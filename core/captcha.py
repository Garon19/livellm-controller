"""Optional self-hosted captcha solving (hCaptcha + image OCR).

Design constraints:
- Everything here is *optional*. If ``hcaptcha-challenger`` / ``ddddocr`` are
  not installed, their models are missing, or :envvar:`CAPTCHA_AUTOSOLVE` is
  off (the default), the controller starts and works exactly as before.
- Heavy imports happen lazily inside methods, never at module import time,
  so ``import main`` stays fast and dependency-free.
- No function in this module is allowed to raise: every public entry point
  catches :class:`Exception`, logs a concise message (no secrets — solver
  errors never contain credentials, and we only log exception text), and
  returns a falsy result.

Environment flags:
- ``CAPTCHA_AUTOSOLVE`` — ``1``/``true``/``yes``/``on`` enables the optional
  auto-solve hook wired into the content route. Default: off.
- ``CAPTCHA_SOLVE_TIMEOUT`` — overall wall-clock budget in seconds for one
  local solve attempt. Default: 45.
- ``CAPTCHA_SOLVER_URL`` — base URL of a self-hosted captcha-solver sidecar
  (e.g. ``http://127.0.0.1:7000``). Empty (the default) disables the sidecar
  path entirely.
- ``CAPTCHA_SOLVER_TOKEN`` — optional bearer token sent as
  ``Authorization: Bearer ...`` to the sidecar.
- ``CAPTCHA_SOLVER_TIMEOUT`` — per-request timeout in seconds for sidecar
  calls. Default: 60.
"""

import asyncio
import logging
import os
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

DEFAULT_SOLVE_TIMEOUT = 45.0
DEFAULT_SIDECAR_TIMEOUT = 60.0

# Selector soup that indicates an hCaptcha widget on a page: the anchor /
# challenge iframes, a sitekey holder, or the bootstrap script.
_HCAPTCHA_SELECTORS = (
    "iframe[src*='hcaptcha.com']",
    "iframe[title*='hCaptcha']",
    "iframe[title*='hcaptcha']",
    "[data-sitekey]",
    "div[class*='h-captcha']",
    "script[src*='hcaptcha.com/1/api.js']",
)

# Ordered detection matrix: (type, selectors). First hit wins; selectors
# within one group are alternatives.
_CAPTCHA_TYPE_SELECTORS: tuple = (
    (
        "turnstile",
        (
            ".cf-turnstile",
            "iframe[src*='challenges.cloudflare.com']",
            "input[name='cf-turnstile-response']",
        ),
    ),
    (
        "recaptcha",
        (
            ".g-recaptcha",
            "iframe[src*='google.com/recaptcha']",
            "textarea[name='g-recaptcha-response']",
            "#g-recaptcha-response",
        ),
    ),
    (
        "hcaptcha",
        (
            "iframe[src*='hcaptcha.com']",
            ".h-captcha",
            "[data-sitekey]",
            "textarea[name='h-captcha-response']",
            "#h-captcha-response",
            "script[src*='hcaptcha.com/1/api.js']",
        ),
    ),
    (
        "cloudflare",
        (
            "#challenge-form",
            "#cf-wrapper",
            "#cf-challenge-running",
        ),
    ),
)

# Element attributes that may carry a widget sitekey / public key.
_SITEKEY_ATTRIBUTE_SELECTORS = (
    "[data-sitekey]",
    "[data-site-key]",
    ".cf-turnstile[data-sitekey]",
    ".g-recaptcha[data-sitekey]",
    ".h-captcha[data-sitekey]",
    "iframe[data-sitekey]",
)


def sidecar_url() -> str:
    """``CAPTCHA_SOLVER_URL`` stripped of trailing slashes; '' when unset."""
    return os.getenv("CAPTCHA_SOLVER_URL", "").strip().rstrip("/")


def sidecar_token() -> str:
    """Optional ``CAPTCHA_SOLVER_TOKEN`` bearer token (never logged)."""
    return os.getenv("CAPTCHA_SOLVER_TOKEN", "").strip()


def sidecar_timeout() -> float:
    """``CAPTCHA_SOLVER_TIMEOUT`` in seconds (min 1.0), default 60."""
    raw = os.getenv("CAPTCHA_SOLVER_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_SIDECAR_TIMEOUT
    try:
        return max(float(raw), 1.0)
    except ValueError:
        logger.warning("Invalid CAPTCHA_SOLVER_TIMEOUT=%r, using default %.0fs", raw, DEFAULT_SIDECAR_TIMEOUT)
        return DEFAULT_SIDECAR_TIMEOUT


def _sidecar_headers() -> dict:
    """Auth headers for the sidecar; omits Authorization entirely when no
    token is configured (and never includes the token in logs)."""
    headers = {"Content-Type": "application/json"}
    token = sidecar_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def autosolve_enabled() -> bool:
    """``CAPTCHA_AUTOSOLVE`` flag; default ON when a sidecar URL is set."""
    default = "1" if sidecar_url() else "0"
    return os.getenv("CAPTCHA_AUTOSOLVE", default).strip().lower() in _TRUTHY


def solve_timeout() -> float:
    """``CAPTCHA_SOLVE_TIMEOUT`` in seconds (min 1.0), default 45."""
    raw = os.getenv("CAPTCHA_SOLVE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_SOLVE_TIMEOUT
    try:
        return max(float(raw), 1.0)
    except ValueError:
        logger.warning("Invalid CAPTCHA_SOLVE_TIMEOUT=%r, using default %.0fs", raw, DEFAULT_SOLVE_TIMEOUT)
        return DEFAULT_SOLVE_TIMEOUT


async def detect_hcaptcha(page) -> bool:
    """Best-effort detection of an hCaptcha widget/iframe on ``page``.

    Never raises; any Playwright error counts as "not detected".
    """
    for selector in _HCAPTCHA_SELECTORS:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


async def detect_captcha_type(page) -> str:
    """Classify the captcha (if any) on ``page``.

    Returns one of ``turnstile`` | ``recaptcha`` | ``hcaptcha`` | ``cloudflare``
    | ``unknown``. Ordered: turnstile/recaptcha are checked before hcaptcha
    because ``[data-sitekey]`` overlaps; cloudflare challenge pages last.
    The "Just a moment..." interstitial is additionally matched by title
    text. Never raises.
    """
    try:
        for ctype, selectors in _CAPTCHA_TYPE_SELECTORS:
            for selector in selectors:
                try:
                    if await page.locator(selector).count() > 0:
                        return ctype
                except Exception:
                    continue
        # Cloudflare interstitials often have no stable form selectors —
        # fall back to the page title.
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""
        if "just a moment" in title.lower():
            return "cloudflare"
    except Exception as e:
        logger.debug("captcha type detection error: %s", e)
    return "unknown"


async def sidecar_health(timeout: Optional[float] = None) -> bool:
    """Probe ``GET {CAPTCHA_SOLVER_URL}/health``.

    Returns False when the sidecar is not configured, unreachable, or
    answers with a non-OK status. Never raises; never logs the token.
    """
    base = sidecar_url()
    if not base:
        return False
    try:
        import httpx

        effective_timeout = timeout if timeout is not None else min(sidecar_timeout(), 10.0)
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            resp = await client.get(f"{base}/health", headers=_sidecar_headers())
            return resp.status_code < 400
    except Exception as e:
        logger.debug("captcha sidecar health check failed: %s", e)
        return False


async def _extract_sitekey(page, ctype: str) -> Optional[str]:
    """Pull the widget sitekey/public key out of the DOM. Never raises."""
    try:
        for selector in _SITEKEY_ATTRIBUTE_SELECTORS:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0:
                    key = await loc.get_attribute("data-sitekey")
                    if key is None:
                        key = await loc.get_attribute("data-site-key")
                    if key and key.strip():
                        return key.strip()
            except Exception:
                continue
        # Turnstile widgets may expose the key on the script/cf element.
        if ctype == "turnstile":
            try:
                key = await page.get_attribute(".cf-turnstile", "data-sitekey")
                if key and key.strip():
                    return key.strip()
            except Exception:
                pass
    except Exception as e:
        logger.debug("sitekey extraction error: %s", e)
    return None


async def _inject_token(page, ctype: str, token: str) -> bool:
    """Insert a solved token into the page's response fields.

    recaptcha/hcaptcha: set the ``g-recaptcha-response`` /
    ``h-captcha-response`` textarea values and dispatch an ``input`` event.
    turnstile: set the ``cf-turnstile-response`` hidden input. Never raises.
    """
    selectors = {
        "recaptcha": ("textarea[name='g-recaptcha-response']", "#g-recaptcha-response"),
        "hcaptcha": ("textarea[name='h-captcha-response']", "#h-captcha-response"),
        "turnstile": ("input[name='cf-turnstile-response']",),
    }.get(ctype, ())
    injected = False
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = await loc.count()
            if count == 0:
                continue
            for i in range(count):
                await loc.nth(i).evaluate(
                    "(el, value) => {"
                    "el.value = value;"
                    "el.dispatchEvent(new Event('input', {bubbles: true}));"
                    "el.dispatchEvent(new Event('change', {bubbles: true}));"
                    "}",
                    token,
                )
            injected = True
        except Exception as e:
            logger.debug("token injection into %s failed: %s", selector, e)
    return injected


async def solve_via_sidecar(ctype: str, page, url: str = "") -> bool:
    """Solve a captcha of ``ctype`` via the configured sidecar service.

    Flow: extract the sitekey from the DOM → ``POST /solve`` → inject the
    returned token into the page → reload the page once on success so the
    guarded content is re-fetched. ``cloudflare`` solves return a
    ``cf_clearance`` cookie instead of a DOM token; when the sidecar sends
    cookies back we add them to the page context before reloading.

    Returns True on apparent success; False on any failure. Never raises
    and never logs tokens, cookies, or the bearer credential.
    """
    base = sidecar_url()
    if not base:
        return False
    where = f" on {url}" if url else ""
    try:
        import httpx

        sitekey = await _extract_sitekey(page, ctype)
        payload = {
            "type": ctype,
            "url": url or (page.url if hasattr(page, "url") else ""),
            "timeout_s": int(sidecar_timeout()),
            # local ONNX tile classifier — no external vision API keys required
            "classifier": "yolo",
        }
        if sitekey:
            payload["sitekey"] = sitekey
            payload["public_key"] = sitekey  # sidecars accept either name

        logger.info("Solving %s captcha via sidecar%s", ctype, where)
        async with httpx.AsyncClient(timeout=sidecar_timeout() + 5.0) as client:
            resp = await client.post(f"{base}/solve", json=payload, headers=_sidecar_headers())
        if resp.status_code >= 400:
            logger.warning(
                "Captcha sidecar returned HTTP %d for %s%s, continuing", resp.status_code, ctype, where
            )
            return False
        data = resp.json() or {}

        token = data.get("token") or data.get("cf_clearance") or ""
        solved_flag = bool(data.get("solved", bool(token)))

        if not solved_flag or not token:
            logger.warning("Captcha sidecar did not solve %s%s, continuing", ctype, where)
            return False

        # Cookie replay (e.g. cf_clearance) — best-effort, never logged.
        cookies = data.get("cookies")
        if isinstance(cookies, dict) and cookies:
            try:
                host = ""
                target = url or (page.url if hasattr(page, "url") else "")
                if "://" in target:
                    host = target.split("/")[2].split(":")[0]
                domain = data.get("cookie_domain") or (f".{host}" if host else "")
                if domain:
                    await page.context.add_cookies(
                        [
                            {"name": name, "value": str(value), "domain": domain, "path": "/"}
                            for name, value in cookies.items()
                        ]
                    )
            except Exception as e:
                logger.debug("sidecar cookie replay skipped: %s", e)

        if ctype != "cloudflare":
            await _inject_token(page, ctype, token)

        # One reload so the site re-checks the (now satisfied) challenge.
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception as e:
            logger.debug("post-solve page reload failed: %s", e)

        logger.info("Captcha sidecar solved %s%s", ctype, where)
        return True
    except Exception as e:
        logger.warning("Captcha sidecar error for %s%s (continuing): %s", ctype, where, e)
        return False


def _ensure_playwright_compat() -> None:
    """Let hcaptcha-challenger run on patchright when playwright is absent.

    hcaptcha-challenger does ``from playwright.async_api import ...`` at
    import time, but ``playwright`` is only an *extra* of that package.
    This controller drives browsers via patchright — a drop-in Playwright
    fork with an identical API — so when the real ``playwright`` package is
    missing we alias the patchright modules into ``sys.modules``. The shim
    is only installed on the solve path (never at module import) and only
    when needed.
    """
    try:
        import playwright.async_api  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import patchright.async_api as pa
        import patchright.sync_api as ps
        import patchright
    except ImportError as e:
        raise ImportError(f"neither playwright nor patchright is importable: {e}") from e
    for name, mod in (
        ("playwright", patchright),
        ("playwright.async_api", pa),
        ("playwright.sync_api", ps),
    ):
        sys.modules.setdefault(name, mod)


class CaptchaSolverService:
    """Lazy singleton wrapper around hcaptcha-challenger and ddddocr.

    Neither solver library is imported until first use. Initialisation
    failures are cached so a broken/missing install costs one log line,
    not an exception storm or a crash.
    """

    _instance: Optional["CaptchaSolverService"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._agent_support: Optional[bool] = None  # hcaptcha-challenger import state
        self._ocr_support: Optional[bool] = None  # ddddocr import state
        self._ocr = None  # cached ddddocr.DdddOcr instance
        self._modelhub = None  # cached hcaptcha_challenger ModelHub

    @classmethod
    def get_instance(cls) -> "CaptchaSolverService":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # hCaptcha
    # ------------------------------------------------------------------

    def agent_available(self) -> bool:
        """True when hcaptcha-challenger is importable (cached)."""
        if self._agent_support is None:
            try:
                _ensure_playwright_compat()
                import hcaptcha_challenger  # noqa: F401

                self._agent_support = True
            except Exception as e:
                logger.warning("hcaptcha-challenger unavailable, hCaptcha autosolve disabled: %s", e)
                self._agent_support = False
        return self._agent_support

    def _get_modelhub(self):
        """Build (once) the ModelHub used to construct per-page agents."""
        if self._modelhub is None:
            from hcaptcha_challenger.onnx.modelhub import ModelHub

            modelhub = ModelHub.from_github_repo()
            if not modelhub.label_alias:
                modelhub.parse_objects()
            self._modelhub = modelhub
        return self._modelhub

    async def solve_hcaptcha(self, page) -> bool:
        """Attempt to solve an hCaptcha challenge on ``page``.

        Returns True only on confirmed success; never raises.
        """
        if not self.agent_available():
            return False
        agent = None
        listener = None
        try:
            from hcaptcha_challenger.agents import AgentT

            agent = AgentT.from_page(
                page=page,
                modelhub=self._get_modelhub(),
                self_supervised=True,
            )
            listener = agent.handler  # registered on page by __post_init__

            # Click the checkbox if present (no-op / suppressed if the
            # challenge iframe is already open).
            try:
                await agent.handle_checkbox()
            except Exception as e:
                logger.debug("hCaptcha checkbox step skipped: %s", e)

            status = await agent.execute()
            value = getattr(status, "value", str(status))
            logger.info("hCaptcha solver finished with status=%s", value)
            return value == "success"
        except Exception as e:
            logger.warning("hCaptcha solve failed (continuing without solve): %s", e)
            return False
        finally:
            # Detach the response listener AgentT attached to the page so
            # solved pages don't accumulate handlers.
            if agent is not None and listener is not None:
                try:
                    page.remove_listener("response", listener)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Image captcha OCR (ddddocr)
    # ------------------------------------------------------------------

    def ocr_available(self) -> bool:
        """True when ddddocr is importable (cached)."""
        if self._ocr_support is None:
            try:
                import ddddocr  # noqa: F401

                self._ocr_support = True
            except Exception as e:
                logger.warning("ddddocr unavailable, image-captcha OCR disabled: %s", e)
                self._ocr_support = False
        return self._ocr_support

    def solve_image_captcha(self, image_bytes: bytes) -> Optional[str]:
        """OCR an image captcha: raw image bytes -> recognized text.

        Exposed for future use (no route calls it yet). Never raises;
        returns None when ddddocr is missing or recognition fails.
        """
        if not self.ocr_available():
            return None
        try:
            if self._ocr is None:
                import ddddocr

                self._ocr = ddddocr.DdddOcr(show_ad=False)
            return self._ocr.classification(image_bytes)
        except Exception as e:
            logger.warning("Image captcha OCR failed: %s", e)
            return None


def install_models() -> bool:
    """Download/refresh hcaptcha-challenger ONNX models.

    Run once manually (see README) — the controller never downloads models
    at runtime. Never raises.
    """
    try:
        import hcaptcha_challenger

        hcaptcha_challenger.install(upgrade=True, clip=True)
        logger.info("hcaptcha-challenger models installed/upgraded")
        return True
    except Exception as e:
        logger.error("Failed to install hcaptcha-challenger models: %s", e)
        return False


async def maybe_autosolve(page, url: str = "") -> bool:
    """High-level optional hook used by the content flow.

    Routing:
    - sidecar configured (``CAPTCHA_SOLVER_URL``) and healthy → detect the
      captcha type and delegate to the sidecar (any supported type);
    - otherwise, hCaptcha only → existing local ONNX path
      (hcaptcha-challenger), bounded by ``CAPTCHA_SOLVE_TIMEOUT``.

    Always returns promptly with a bool and never raises, so callers can
    treat it as fire-and-forget.
    """
    if not autosolve_enabled():
        return False

    where = f" on {url}" if url else ""
    try:
        ctype = await detect_captcha_type(page)
        if ctype == "unknown":
            return False

        if sidecar_url() and await sidecar_health():
            logger.info("%s captcha detected%s, routing to sidecar", ctype, where)
            return await solve_via_sidecar(ctype, page, url)

        # No (healthy) sidecar: local ONNX path, hCaptcha only.
        if ctype != "hcaptcha":
            logger.info(
                "%s captcha detected%s but no sidecar configured/healthy and local solver is hCaptcha-only, skipping",
                ctype,
                where,
            )
            return False

        service = CaptchaSolverService.get_instance()
        if not service.agent_available():
            logger.info("hCaptcha detected%s but solver unavailable, skipping", where)
            return False

        logger.info("hCaptcha detected%s, attempting local autosolve", where)
        try:
            solved = await asyncio.wait_for(
                service.solve_hcaptcha(page), timeout=solve_timeout()
            )
        except asyncio.TimeoutError:
            logger.warning(
                "hCaptcha autosolve timed out after %.0fs%s, continuing", solve_timeout(), where
            )
            return False

        if solved:
            logger.info("hCaptcha autosolve succeeded%s", where)
        else:
            logger.warning("hCaptcha autosolve did not pass%s, continuing", where)
        return solved
    except Exception as e:
        # Belt and braces: this hook must never break the content flow.
        logger.warning("captcha autosolve error%s (continuing): %s", where, e)
        return False

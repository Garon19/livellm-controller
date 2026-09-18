# livellm-controller

FastAPI controller for remote Chromium browsers connected over CDP.

## Durable proxy sessions

A stable session ID gets an isolated `BrowserContext`, its own proxy, and reusable browser login state (cookies, local storage, and IndexedDB). Registrations and storage state are encrypted locally. Calls to `/start_session` without `session_id` and `proxy` keep the legacy random-ID/shared-context behavior.

### One browser, many contexts

Sessions do **not** spawn a separate Chrome per account — every named
session is an isolated `BrowserContext` inside the single connected
browser:

```text
one Chrome process
  ├── BrowserContext "perekrestok-main"  → proxy-1 → own cookies/logins
  ├── BrowserContext "ozon-main"         → proxy-2 → own cookies/logins
  └── BrowserContext "account-3"         → proxy-3 → own cookies/logins
```

Each context is an independent visitor to the site: own IP (Chromium
applies proxy per-context natively), own cookies/localStorage/IndexedDB.
Contexts are cheap (~50–100 MB) and lazy — they open on first use and
closing one never kills the shared browser. The process-level fingerprint
(User-Agent, Chrome version, fonts, WebGL renderer) is shared across
contexts; if a target needs a fully independent fingerprint, run a
separate browser instance/pod instead of a named session.

### Persistence configuration

- `SESSION_STORE_DIR`: encrypted record directory. Defaults to the platform-local data directory (`%LOCALAPPDATA%/livellm-controller/sessions` on Windows or `$XDG_DATA_HOME/livellm-controller/sessions`, falling back to `~/.local/share/livellm-controller/sessions`, on Unix).
- `SESSION_STORE_KEY`: a Fernet key supplied directly by the runtime.
- `SESSION_STORE_KEY_FILE`: path to a Fernet key file.

If neither key setting is present, the controller creates `session_store.key` in `SESSION_STORE_DIR` with best-effort owner-only permissions. Keep that key durable and private; losing it makes saved sessions unreadable. Records are written atomically. The API and logs never return proxy usernames, passwords, cookies, or other saved authentication state.

### Pre-register sessions

```bash
curl -X POST http://localhost:8000/parser/sessions/register \
  -H 'Content-Type: application/json' \
  -d '{
    "sessions": [{
      "session_id": "account-placeholder",
      "browser_id": "browser-placeholder",
      "proxy": {
        "type": "https",
        "host": "proxy.example.invalid",
        "port": 8443,
        "username": "username-placeholder",
        "password": "password-placeholder",
        "bypass": "localhost"
      }
    }],
    "replace": false
  }'
```

Registration is idempotent for identical configuration. Batch items are applied independently. A mixed-success batch returns HTTP `207` with `partial: true` and an explicit `registration_status` for every item; earlier successful items are not rolled back when a later item fails. A conflicting item is reported as `conflict` unless `replace` is `true`; replacement closes the active context and clears its previous saved login state.

### Activate and use a session

```bash
curl -X POST http://localhost:8000/parser/start_session \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"account-placeholder"}'

curl -X POST http://localhost:8000/parser/content \
  -H 'X-Session-Id: account-placeholder' \
  -H 'X-Browser-Id: browser-placeholder' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.invalid"}'
```

A session may also be registered and activated in one `/start_session` request by supplying `session_id`, `proxy`, and optionally `browser_id`.

### List, deactivate, and purge

```bash
curl http://localhost:8000/parser/sessions
curl -X DELETE http://localhost:8000/parser/end_session \
  -H 'X-Session-Id: account-placeholder'
curl -X DELETE http://localhost:8000/parser/sessions/account-placeholder
```

`DELETE /end_session` snapshots state, closes the dedicated context, and retains the registration. `DELETE /sessions/{session_id}` closes and permanently purges the encrypted registration and login state. `GET /sessions` exposes redacted proxy metadata and activity only.

## Captcha solving (optional, self-hosted)

The controller can optionally solve hCaptcha challenges and OCR image captchas locally — no third-party anti-captcha service, no API keys.

### Install

```bash
uv sync                 # installs hcaptcha-challenger and ddddocr (already in pyproject)
uv run python -c "from core.captcha import install_models; install_models()"
```

The second command downloads the ONNX model zoo once (run it manually; the controller never downloads models at runtime). Both packages are lazy-imported: if they are missing, the ONNX models are absent, or the flag below is off, the controller starts and behaves exactly as before.

### Environment flags

- `CAPTCHA_AUTOSOLVE`: `1` to enable. When enabled, `POST /content` checks each navigated page for a captcha widget and, if found, attempts a solve before scrolling/output. Default: off.
- `CAPTCHA_SOLVE_TIMEOUT`: wall-clock budget in seconds for one *local* (ONNX) solve attempt. Default: `45`.
- `CAPTCHA_SOLVER_URL`: base URL of a self-hosted captcha-solver sidecar (e.g. `http://127.0.0.1:7000`). Empty (default) disables the sidecar path; solving stays local hCaptcha-only.
- `CAPTCHA_SOLVER_TOKEN`: optional bearer token; sent as `Authorization: Bearer …` on sidecar calls. Never logged.
- `CAPTCHA_SOLVER_TIMEOUT`: per-request timeout in seconds for sidecar HTTP calls. Default: `60`.

### Captcha-solver sidecar (optional)

`CAPTCHA_SOLVER_URL` points at a self-hosted sidecar (waguriagentic/captcha-solver style) exposing:

- `GET /health` — liveness probe (must answer < 400);
- `POST /solve` — JSON body `{type, sitekey|public_key, url|page_url, proxy?, timeout_s?}` → `{solved, token|cf_clearance, user_agent?, cookies?}`.

When the sidecar is configured **and** healthy, `maybe_autosolve` classifies the page's captcha type and routes to the sidecar; without a sidecar only the local hCaptcha ONNX path runs. Supported-type matrix:

| Type | Local (no sidecar) | Via sidecar |
|---|---|---|
| hCaptcha | ✅ hcaptcha-challenger ONNX | ✅ |
| reCAPTCHA v2 | ❌ | ✅ |
| Cloudflare Turnstile | ❌ | ✅ (token injection) |
| Cloudflare challenge ("Just a moment…", `cf_clearance`) | ❌ | ✅ (cookie replay) |
| Image captcha (ddddocr) | library helper only | n/a |

Notes:

- The sidecar is a **separate Linux service** (systemd unit + Xvfb display) — it is not shipped with or started by this controller. Point `CAPTCHA_SOLVER_URL` at it and keep `CAPTCHA_SOLVER_TOKEN` out of logs/repos.
- On success the returned token is injected into the page (`g-recaptcha-response` / `h-captcha-response` textareas, `cf-turnstile-response` hidden input) and the page is reloaded once; sidecar-returned cookies (e.g. `cf_clearance`) are replayed into the browser context.
- **`cf_clearance` replay caveats**: the cookie is bound to the solving session's **IP address and User-Agent**. It only replays cleanly when the controller's browser egress IP and UA match the sidecar's — otherwise Cloudflare rejects it. Use the same proxy on both, or have the sidecar solve through the same egress route; the sidecar's returned `user_agent`/`cookies` fields are applied best-effort.
- Sidecar failures/timeouts are logged (without secrets) and never fail the content request.

Solve failures, timeouts, and missing solver dependencies are logged (without secrets) and never fail the request — the content flow continues regardless.

### Limitations

- Solving is best-effort: hCaptcha may reject answers depending on IP/site reputation; failures just continue without a solve.
- Models target hCaptcha's English prompts; non-English challenge prompts may fall back to the self-supervised CLIP path or fail.
- `ddddocr` image OCR (`core.captcha.CaptchaSolverService.solve_image_captcha`, bytes → text) is wired as a library helper for future routes; no endpoint uses it yet.
- hcaptcha-challenger expects the `playwright` package; when only patchright is installed, `core/captcha.py` aliases patchright into `sys.modules` as `playwright` on the solve path only.


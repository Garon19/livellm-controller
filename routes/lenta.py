from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException

from core.dependencies import PageDep
from models.requests import LentaBootstrapRequest, LentaItemRequest

router = APIRouter(tags=["Lenta"])

_BOOTSTRAP_URL = "https://lenta.com/catalog/"
_API_BASE = "https://api.lenta.com/v1/catalog/items"
_BOOTSTRAP_HOST = "lenta.com"
_BOOTSTRAP_PATH = "/api-gateway/v1/region/user"
_FORWARD_HEADERS = frozenset(
    {
        "accept-language",
        "app-version",
        "client",
        "deviceid",
        "experiments",
        "sessiontoken",
        "user-agent",
        "x-delivery-mode",
        "x-device-id",
        "x-device-web-platform",
        "x-domain",
        "x-organization-id",
        "x-platform",
        "x-retail-brand",
        "x-user-session-id",
    }
)
_CONTEXT_DIMENSIONS = (
    "x-organization-id",
    "x-delivery-mode",
    "x-domain",
    "x-retail-brand",
)
_REQUIRED_CONTEXT_HEADERS = frozenset(
    {
        "sessiontoken",
        "deviceid",
        "x-organization-id",
        "x-delivery-mode",
        "x-retail-brand",
        "x-platform",
    }
)


@dataclass
class _StorefrontContext:
    headers: dict[str, str]
    opaque_id: str


# Captured values live only as long as their Page/session. They are never
# logged, serialized, persisted, or returned by any endpoint.
_PAGE_CONTEXTS: WeakKeyDictionary = WeakKeyDictionary()
_METRICS = {
    "bootstrap_total": 0,
    "requests_total": 0,
    "api_success_total": 0,
    "jsonld_fallback_total": 0,
    "auth_expired_total": 0,
    "retryable_failure_total": 0,
    "failure_total": 0,
}


def _is_bootstrap_request(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == _BOOTSTRAP_HOST and parsed.path == _BOOTSTRAP_PATH


def _opaque_context_id(headers: dict[str, str]) -> str:
    dimensions = {name: headers.get(name, "") for name in _CONTEXT_DIMENSIONS}
    canonical = json.dumps(dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def bootstrap_lenta(page, timeout_ms: float) -> dict[str, Any]:
    captured = asyncio.Event()
    result: dict[str, dict[str, str]] = {}
    tasks: set[asyncio.Task] = set()

    async def retain_allowed_headers(request) -> None:
        all_headers = await request.all_headers()
        lowered = {str(name).lower(): str(value) for name, value in all_headers.items()}
        headers = {name: lowered[name] for name in _FORWARD_HEADERS if name in lowered}
        if _REQUIRED_CONTEXT_HEADERS.issubset(headers):
            result["headers"] = headers
            captured.set()

    def on_request(request) -> None:
        if _is_bootstrap_request(request.url):
            task = asyncio.create_task(retain_allowed_headers(request))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    _PAGE_CONTEXTS.pop(page, None)
    page.on("request", on_request)
    try:
        await page.goto(_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await asyncio.wait_for(captured.wait(), timeout=max(1.0, timeout_ms / 1000))
        except asyncio.TimeoutError:
            _METRICS["failure_total"] += 1
            raise HTTPException(status_code=502, detail="lenta_bootstrap_context_unavailable")
    finally:
        page.remove_listener("request", on_request)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    headers = result.get("headers")
    if not headers:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="lenta_bootstrap_context_unavailable")
    context = _StorefrontContext(headers=headers, opaque_id=_opaque_context_id(headers))
    _PAGE_CONTEXTS[page] = context
    _METRICS["bootstrap_total"] += 1
    return {"status": "ready", "storefront_context_id": context.opaque_id}


def _jsonld_product(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script[type='application/ld+json']"):
        try:
            value = json.loads(node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            entries = graph if isinstance(graph, list) else [candidate]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                product_type = entry.get("@type")
                if product_type == "Product" or (
                    isinstance(product_type, list) and "Product" in product_type
                ):
                    return entry
    return None


def _field_provenance(product: dict[str, Any], source: str) -> dict[str, str]:
    return {str(key): source for key, value in product.items() if value is not None}


async def _fallback(
    page,
    request: LentaItemRequest,
    product_id: str,
    context_id: str,
    reason: str,
    api_status: int,
) -> dict[str, Any]:
    if page.url != request.url:
        await page.goto(request.url, wait_until="domcontentloaded", timeout=request.timeout)
    product = _jsonld_product(await page.content())
    jsonld_id = None if not product else product.get("sku") or product.get("productID") or product.get("mpn")
    if product and jsonld_id is not None and str(jsonld_id) != product_id:
        product = None
    if not product:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="lenta_product_unavailable")
    _METRICS["jsonld_fallback_total"] += 1
    return {
        "schema_version": "lenta.product.v1",
        "product_id": product_id,
        "source": "html_jsonld",
        "product": product,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storefront_context_id": context_id,
        "field_provenance": _field_provenance(product, "html_jsonld"),
        "provenance": {
            "source": "lenta_html_jsonld",
            "page_url": request.url,
            "api_url": f"{_API_BASE}/{product_id}",
            "api_status": api_status,
            "fallback_reason": reason,
            "storefront_context_id": context_id,
        },
        "metrics": {
            "api_attempted": True,
            "api_succeeded": False,
            "jsonld_fallback_used": True,
        },
    }


async def fetch_lenta_item(product_id: str, request: LentaItemRequest, page) -> dict[str, Any]:
    _METRICS["requests_total"] += 1
    if request.product_id != product_id:
        raise HTTPException(status_code=422, detail="lenta_product_id_mismatch")
    context = _PAGE_CONTEXTS.get(page)
    if context is None:
        raise HTTPException(status_code=409, detail="lenta_bootstrap_required")

    api_url = f"{_API_BASE}/{product_id}"
    try:
        response = await page.context.request.get(
            api_url,
            headers=context.headers,
            timeout=request.timeout,
        )
    except Exception:
        _METRICS["retryable_failure_total"] += 1
        raise HTTPException(status_code=502, detail="lenta_api_transport_error")

    status = response.status
    if status in {401, 403}:
        _PAGE_CONTEXTS.pop(page, None)
        _METRICS["auth_expired_total"] += 1
        raise HTTPException(status_code=status, detail="lenta_context_expired")
    if status == 404:
        return await _fallback(page, request, product_id, context.opaque_id, "api_not_found", status)
    if status == 429 or status >= 500:
        _METRICS["retryable_failure_total"] += 1
        raise HTTPException(status_code=status, detail="lenta_api_retryable_error")
    if status != 200:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="lenta_api_unexpected_status")

    try:
        product = await response.json()
    except Exception:
        product = None
    prices = product.get("prices") if isinstance(product, dict) else None
    features = product.get("features") if isinstance(product, dict) else None
    valid_product = (
        isinstance(product, dict)
        and str(product.get("id", "")) == product_id
        and bool(product.get("name"))
        and isinstance(prices, dict)
        and isinstance(prices.get("price"), (int, float))
        and not isinstance(prices.get("price"), bool)
        and "count" in product
        and isinstance(features, dict)
        and isinstance(features.get("isBlockedForSale"), bool)
        and isinstance(product.get("images"), list)
        and bool(product.get("images"))
    )
    if not valid_product:
        return await _fallback(page, request, product_id, context.opaque_id, "api_payload_invalid", status)

    _METRICS["api_success_total"] += 1
    return {
        "schema_version": "lenta.product.v1",
        "product_id": product_id,
        "source": "api",
        "product": product,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storefront_context_id": context.opaque_id,
        "field_provenance": _field_provenance(product, "api"),
        "provenance": {
            "source": "lenta_catalog_api",
            "page_url": request.url,
            "api_url": api_url,
            "api_status": status,
            "fallback_reason": None,
            "storefront_context_id": context.opaque_id,
        },
        "metrics": {
            "api_attempted": True,
            "api_succeeded": True,
            "jsonld_fallback_used": False,
        },
    }


@router.post("/lenta/bootstrap")
async def lenta_bootstrap(request: LentaBootstrapRequest, page: PageDep):
    return await bootstrap_lenta(page, request.timeout)


@router.post("/lenta/items/{product_id}")
async def lenta_item(product_id: str, request: LentaItemRequest, page: PageDep):
    if not product_id.isdigit():
        raise HTTPException(status_code=422, detail="invalid_lenta_product_id")
    return await fetch_lenta_item(product_id, request, page)


@router.get("/lenta/metrics")
async def lenta_metrics():
    return dict(_METRICS)

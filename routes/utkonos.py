from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException

from core.dependencies import PageDep
from models.requests import (
    UtkonosBootstrapRequest,
    UtkonosItemRequest,
    UtkonosListingRequest,
)

router = APIRouter(tags=["Utkonos"])

_BOOTSTRAP_URL = "https://www.utkonos.ru/catalog/183"
_API_ROOT = "https://api.lenta.com/v1/catalog"
_BOOTSTRAP_HOSTS = {"utkonos.ru", "www.utkonos.ru"}
_BOOTSTRAP_PATHS = {
    "/api-gateway/v1/catalog/items",
    "/api-gateway/v1/region/user",
}
_FORWARD_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "app-version",
        "client",
        "content-type",
        "cookie",
        "deviceid",
        "experiments",
        "origin",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sessiontoken",
        "traceparent",
        "user-agent",
        "x-delivery-mode",
        "x-device-id",
        "x-device-brand",
        "x-device-name",
        "x-device-os",
        "x-device-os-version",
        "x-device-web-platform",
        "x-domain",
        "x-organization-id",
        "x-platform",
        "x-retail-brand",
        "x-user-session-id",
    }
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
_CONTEXT_DIMENSIONS = (
    "x-organization-id",
    "x-delivery-mode",
    "x-domain",
    "x-retail-brand",
)


@dataclass
class _StorefrontContext:
    headers: dict[str, str]
    opaque_id: str


# Session values are retained only by the live Page and are never returned,
# logged, or persisted.
_PAGE_CONTEXTS: WeakKeyDictionary = WeakKeyDictionary()
_METRICS = {
    "bootstrap_total": 0,
    "categories_total": 0,
    "listing_requests_total": 0,
    "item_requests_total": 0,
    "api_success_total": 0,
    "jsonld_fallback_total": 0,
    "auth_expired_total": 0,
    "retryable_failure_total": 0,
    "failure_total": 0,
}


def _is_bootstrap_request(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in _BOOTSTRAP_HOSTS
        and parsed.path in _BOOTSTRAP_PATHS
    )


def _opaque_context_id(headers: dict[str, str]) -> str:
    dimensions = {name: headers.get(name, "") for name in _CONTEXT_DIMENSIONS}
    canonical = json.dumps(
        dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def bootstrap_utkonos(page, timeout_ms: float) -> dict[str, Any]:
    captured = asyncio.Event()
    result: dict[str, dict[str, str]] = {}
    tasks: set[asyncio.Task] = set()

    async def retain_allowed_headers(request) -> None:
        all_headers = await request.all_headers()
        lowered = {
            str(name).lower(): str(value) for name, value in all_headers.items()
        }
        headers = {
            name: lowered[name] for name in _FORWARD_HEADERS if name in lowered
        }
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
        await page.goto(
            _BOOTSTRAP_URL,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        try:
            await asyncio.wait_for(
                captured.wait(), timeout=max(1.0, timeout_ms / 1000)
            )
        except asyncio.TimeoutError:
            _METRICS["failure_total"] += 1
            raise HTTPException(
                status_code=502, detail="utkonos_bootstrap_context_unavailable"
            )
    finally:
        page.remove_listener("request", on_request)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    headers = result.get("headers")
    if not headers:
        _METRICS["failure_total"] += 1
        raise HTTPException(
            status_code=502, detail="utkonos_bootstrap_context_unavailable"
        )
    context = _StorefrontContext(
        headers=headers,
        opaque_id=_opaque_context_id(headers),
    )
    _PAGE_CONTEXTS[page] = context
    _METRICS["bootstrap_total"] += 1
    return {"status": "ready", "storefront_context_id": context.opaque_id}


def _context(page) -> _StorefrontContext:
    context = _PAGE_CONTEXTS.get(page)
    if context is None:
        raise HTTPException(status_code=409, detail="utkonos_bootstrap_required")
    return context


def _handle_status(page, status: int) -> None:
    if status in {401, 403}:
        _PAGE_CONTEXTS.pop(page, None)
        _METRICS["auth_expired_total"] += 1
        raise HTTPException(status_code=status, detail="utkonos_context_expired")
    if status == 429 or status >= 500:
        _METRICS["retryable_failure_total"] += 1
        raise HTTPException(status_code=status, detail="utkonos_api_retryable_error")
    if status != 200:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_api_unexpected_status")


async def _json_response(response) -> Any:
    try:
        return await response.json()
    except Exception:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_api_invalid_json")


async def fetch_utkonos_categories(page) -> dict[str, Any]:
    context = _context(page)
    _METRICS["categories_total"] += 1
    try:
        response = await page.context.request.get(
            f"{_API_ROOT}/categories",
            headers=context.headers,
            timeout=30000,
        )
    except Exception:
        _METRICS["retryable_failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_api_transport_error")
    _handle_status(page, response.status)
    payload = await _json_response(response)
    categories = payload.get("categories") if isinstance(payload, dict) else None
    if categories is None:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_categories_invalid")
    _METRICS["api_success_total"] += 1
    return {
        "schema_version": "utkonos.categories.v1",
        "categories": categories,
        "storefront_context_id": context.opaque_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {"source": "utkonos_catalog_api"},
    }


def _listing_body(request: UtkonosListingRequest) -> dict[str, Any]:
    return {
        "categoryId": int(request.category_id),
        "filters": {"checkbox": [], "multicheckbox": [], "range": []},
        "sort": {"type": "popular", "order": "desc"},
        "limit": request.limit,
        "offset": request.offset,
    }


def _total_count(payload: dict[str, Any]) -> int | None:
    value = payload.get("total")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


async def fetch_utkonos_listing(
    request: UtkonosListingRequest,
    page,
) -> dict[str, Any]:
    context = _context(page)
    _METRICS["listing_requests_total"] += 1
    try:
        response = await page.context.request.post(
            f"{_API_ROOT}/items",
            headers=context.headers,
            data=_listing_body(request),
            timeout=30000,
        )
    except Exception:
        _METRICS["retryable_failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_api_transport_error")
    _handle_status(page, response.status)
    payload = await _json_response(response)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="utkonos_listing_invalid")
    items = payload.get("items")
    total_count = _total_count(payload)
    if items is None or total_count is None:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_listing_invalid")
    _METRICS["api_success_total"] += 1
    return {
        "schema_version": "utkonos.listing.v1",
        "category_id": request.category_id,
        "offset": request.offset,
        "limit": request.limit,
        "items": items,
        "total_count": total_count,
        "storefront_context_id": context.opaque_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {"source": "utkonos_catalog_api"},
    }


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
                product_type = entry.get("@type") if isinstance(entry, dict) else None
                if product_type == "Product" or (
                    isinstance(product_type, list) and "Product" in product_type
                ):
                    return entry
    return None


def _field_provenance(product: dict[str, Any], source: str) -> dict[str, str]:
    return {
        str(key): source for key, value in product.items() if value is not None
    }


def _product_id(product: dict[str, Any]) -> str:
    value = product.get("sku")
    if value is None:
        value = product.get("id")
    return "" if value is None else str(value)


async def _fallback(
    page,
    request: UtkonosItemRequest,
    product_id: str,
    context_id: str,
    reason: str,
    api_status: int,
) -> dict[str, Any]:
    if page.url != request.url:
        await page.goto(
            request.url,
            wait_until="domcontentloaded",
            timeout=request.timeout,
        )
    product = _jsonld_product(await page.content())
    jsonld_id = (
        None
        if not product
        else product.get("sku") or product.get("productID") or product.get("mpn")
    )
    if product and (jsonld_id is None or str(jsonld_id) != product_id):
        product = None
    if not product:
        _METRICS["failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_product_unavailable")
    _METRICS["jsonld_fallback_total"] += 1
    return {
        "schema_version": "utkonos.product.v1",
        "product_id": product_id,
        "source": "html_jsonld",
        "product": product,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storefront_context_id": context_id,
        "field_provenance": _field_provenance(product, "html_jsonld"),
        "provenance": {
            "source": "utkonos_html_jsonld",
            "page_url": request.url,
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


async def fetch_utkonos_item(
    product_id: str,
    request: UtkonosItemRequest,
    page,
) -> dict[str, Any]:
    _METRICS["item_requests_total"] += 1
    if request.product_id != product_id:
        raise HTTPException(status_code=422, detail="utkonos_product_id_mismatch")
    context = _context(page)
    api_url = f"{_API_ROOT}/items/{product_id}"
    try:
        response = await page.context.request.get(
            api_url,
            headers=context.headers,
            timeout=request.timeout,
        )
    except Exception:
        _METRICS["retryable_failure_total"] += 1
        raise HTTPException(status_code=502, detail="utkonos_api_transport_error")

    status = response.status
    if status == 404:
        return await _fallback(
            page,
            request,
            product_id,
            context.opaque_id,
            "api_not_found",
            status,
        )
    _handle_status(page, status)
    product = await _json_response(response)
    prices = product.get("prices") if isinstance(product, dict) else None
    features = product.get("features") if isinstance(product, dict) else None
    current_price = prices.get("price") if isinstance(prices, dict) else None
    valid_product = (
        isinstance(product, dict)
        and _product_id(product) == product_id
        and bool(product.get("name"))
        and isinstance(prices, dict)
        and isinstance(current_price, (int, float))
        and not isinstance(current_price, bool)
        and math.isfinite(float(current_price))
        and current_price > 0
        and "count" in product
        and isinstance(features, dict)
        and isinstance(features.get("isBlockedForSale"), bool)
        and isinstance(product.get("images"), list)
        and bool(product.get("images"))
    )
    if not valid_product:
        return await _fallback(
            page,
            request,
            product_id,
            context.opaque_id,
            "api_payload_invalid",
            status,
        )

    _METRICS["api_success_total"] += 1
    return {
        "schema_version": "utkonos.product.v1",
        "product_id": product_id,
        "source": "api",
        "product": product,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storefront_context_id": context.opaque_id,
        "field_provenance": _field_provenance(product, "api"),
        "provenance": {
            "source": "utkonos_catalog_api",
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


@router.post("/utkonos/bootstrap")
async def utkonos_bootstrap(request: UtkonosBootstrapRequest, page: PageDep):
    return await bootstrap_utkonos(page, request.timeout)


@router.get("/utkonos/categories")
async def utkonos_categories(page: PageDep):
    return await fetch_utkonos_categories(page)


@router.post("/utkonos/catalog/items")
async def utkonos_listing(request: UtkonosListingRequest, page: PageDep):
    return await fetch_utkonos_listing(request, page)


@router.post("/utkonos/items/{product_id}")
async def utkonos_item(
    product_id: str,
    request: UtkonosItemRequest,
    page: PageDep,
):
    if not product_id.isdigit():
        raise HTTPException(status_code=422, detail="invalid_utkonos_product_id")
    return await fetch_utkonos_item(product_id, request, page)


@router.get("/utkonos/metrics")
async def utkonos_metrics():
    return dict(_METRICS)

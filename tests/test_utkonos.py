import json

import pytest
from fastapi import HTTPException

from models.requests import (
    UtkonosBootstrapRequest,
    UtkonosItemRequest,
    UtkonosListingRequest,
)
from routes.utkonos import (
    bootstrap_utkonos,
    fetch_utkonos_categories,
    fetch_utkonos_item,
    fetch_utkonos_listing,
)


class _BootstrapRequest:
    url = "https://www.utkonos.ru/api-gateway/v1/catalog/items"

    async def all_headers(self):
        return {
            "accept": "application/json",
            "accept-language": "ru",
            "app-version": "1",
            "client": "web",
            "content-type": "application/json",
            "cookie": "secret-cookie",
            "deviceid": "secret-device",
            "experiments": "exp",
            "origin": "https://www.utkonos.ru",
            "referer": "https://www.utkonos.ru/",
            "sessiontoken": "secret-session",
            "user-agent": "browser",
            "x-delivery-mode": "delivery",
            "x-domain": "www.utkonos.ru",
            "x-organization-id": "org",
            "x-platform": "web",
            "x-retail-brand": "utkonos",
            "authorization": "must-not-forward",
        }


class _ApiResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _RequestContext:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class _Page:
    def __init__(self, responses, html="<html></html>"):
        self.url = "about:blank"
        self.handler = None
        self.html = html
        self.goto_calls = []
        self.context = type("Context", (), {})()
        self.context.request = _RequestContext(responses)

    def on(self, event, handler):
        assert event == "request"
        self.handler = handler

    def remove_listener(self, event, handler):
        assert event == "request" and handler is self.handler

    async def goto(self, url, **_kwargs):
        self.url = url
        self.goto_calls.append(url)
        if url == "https://www.utkonos.ru/catalog/183":
            self.handler(_BootstrapRequest())
        return type("Navigation", (), {"status": 200})()

    async def content(self):
        return self.html


def _product(product_id):
    return {
        "id": product_id,
        "sku": str(product_id),
        "name": f"Товар {product_id}",
        "count": 1,
        "prices": {"price": 10999, "priceRegular": 15789},
        "features": {"isBlockedForSale": False},
        "images": [{"original": "https://img.test/item.jpg"}],
    }


@pytest.mark.asyncio
async def test_one_bootstrap_reuses_context_for_categories_listing_and_item_without_secret_output():
    categories = {
        "categories": [
            {"id": 4, "parentId": 0, "hasChildren": True, "name": "Каталог"},
            {"id": 41, "parentId": 4, "hasChildren": False, "name": "Вода"},
        ]
    }
    listing = {"items": [_product(545764)], "total": 1}
    page = _Page([
        _ApiResponse(200, categories),
        _ApiResponse(200, listing),
        _ApiResponse(200, _product(545764)),
    ])

    bootstrap = await bootstrap_utkonos(page, 1000)
    category_result = await fetch_utkonos_categories(page)
    listing_result = await fetch_utkonos_listing(
        UtkonosListingRequest(category_id="41", limit=40, offset=0), page
    )
    item_result = await fetch_utkonos_item(
        "545764",
        UtkonosItemRequest(url="https://www.utkonos.ru/item/545764/"),
        page,
    )

    assert page.goto_calls == ["https://www.utkonos.ru/catalog/183"]
    assert bootstrap["storefront_context_id"] == item_result["storefront_context_id"]
    assert category_result["schema_version"] == "utkonos.categories.v1"
    assert listing_result["schema_version"] == "utkonos.listing.v1"
    assert listing_result["total_count"] == 1
    post_call = page.context.request.calls[1]
    assert post_call[2]["data"] == {
        "categoryId": 41,
        "filters": {"checkbox": [], "multicheckbox": [], "range": []},
        "sort": {"type": "popular", "order": "desc"},
        "limit": 40,
        "offset": 0,
    }
    forwarded = set(post_call[2]["headers"])
    assert "authorization" not in forwarded
    serialized = json.dumps([bootstrap, category_result, listing_result, item_result])
    assert "secret-session" not in serialized
    assert "secret-device" not in serialized
    assert "secret-cookie" not in serialized
    assert item_result["schema_version"] == "utkonos.product.v1"
    assert item_result["product_id"] == "545764"


@pytest.mark.asyncio
async def test_listing_retryable_status_does_not_become_empty_category():
    page = _Page([_ApiResponse(503)])
    await bootstrap_utkonos(page, 1000)

    with pytest.raises(HTTPException) as error:
        await fetch_utkonos_listing(
            UtkonosListingRequest(category_id="41", limit=40, offset=0), page
        )
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_item_404_uses_only_matching_jsonld_fallback():
    html = '<script type="application/ld+json">{"@type":"Product","sku":"545764","name":"Вода","offers":{"price":"109.99"}}</script>'
    page = _Page([_ApiResponse(404)], html)
    await bootstrap_utkonos(page, 1000)

    result = await fetch_utkonos_item(
        "545764",
        UtkonosItemRequest(url="https://www.utkonos.ru/item/545764/"),
        page,
    )

    assert result["source"] == "html_jsonld"
    assert result["product_id"] == "545764"
    assert result["provenance"]["fallback_reason"] == "api_not_found"


@pytest.mark.asyncio
async def test_jsonld_fallback_requires_product_identifier():
    html = '<script type="application/ld+json">{"@type":"Product","name":"Wrong card","offers":{"price":"10.00"}}</script>'
    page = _Page([_ApiResponse(404)], html)
    await bootstrap_utkonos(page, 1000)

    with pytest.raises(HTTPException, match="utkonos_product_unavailable"):
        await fetch_utkonos_item(
            "545764",
            UtkonosItemRequest(url="https://www.utkonos.ru/item/545764/"),
            page,
        )


@pytest.mark.asyncio
async def test_api_rejects_non_positive_price():
    broken = _product(545764)
    broken["prices"]["price"] = 0
    page = _Page([_ApiResponse(200, broken)], html="<html></html>")
    await bootstrap_utkonos(page, 1000)

    with pytest.raises(HTTPException, match="utkonos_product_unavailable"):
        await fetch_utkonos_item(
            "545764",
            UtkonosItemRequest(url="https://www.utkonos.ru/item/545764/"),
            page,
        )

import json

import pytest
from fastapi import HTTPException

from models.requests import LentaItemRequest
from routes.lenta import bootstrap_lenta, fetch_lenta_item


class _BootstrapRequest:
    url = "https://lenta.com/api-gateway/v1/region/user"

    async def all_headers(self):
        return {
            "accept-language": "ru",
            "app-version": "1",
            "client": "web",
            "deviceid": "secret-device",
            "experiments": "exp",
            "sessiontoken": "secret-session",
            "user-agent": "browser",
            "x-delivery-mode": "delivery",
            "x-device-id": "secret-device-2",
            "x-device-web-platform": "desktop",
            "x-domain": "lenta.com",
            "x-organization-id": "org",
            "x-platform": "web",
            "x-retail-brand": "lenta",
            "x-user-session-id": "secret-user-session",
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
        self.calls.append((url, kwargs))
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
        if url == "https://lenta.com/catalog/":
            self.handler(_BootstrapRequest())
        return type("Navigation", (), {"status": 200})()

    async def content(self):
        return self.html


def _product(product_id):
    return {
        "id": product_id,
        "name": f"Товар {product_id}",
        "count": 1,
        "prices": {"price": 10999, "priceRegular": 15789},
        "features": {"isBlockedForSale": False},
        "images": [{"original": "https://img.test/item.jpg"}],
    }


@pytest.mark.asyncio
async def test_one_bootstrap_is_reused_for_two_api_items_without_navigation_or_secret_output():
    page = _Page([_ApiResponse(200, _product(21)), _ApiResponse(200, _product(22))])

    bootstrap = await bootstrap_lenta(page, 1000)
    first = await fetch_lenta_item("21", LentaItemRequest(url="https://lenta.com/product/a-21/"), page)
    second = await fetch_lenta_item("22", LentaItemRequest(url="https://lenta.com/product/b-22/"), page)

    assert page.goto_calls == ["https://lenta.com/catalog/"]
    assert bootstrap["storefront_context_id"] == first["storefront_context_id"] == second["storefront_context_id"]
    forwarded = set(page.context.request.calls[0][1]["headers"])
    assert forwarded == {
        "accept-language", "app-version", "client", "deviceid", "experiments",
        "sessiontoken", "user-agent", "x-delivery-mode", "x-device-id",
        "x-device-web-platform", "x-domain", "x-organization-id", "x-platform",
        "x-retail-brand", "x-user-session-id",
    }
    serialized = json.dumps([bootstrap, first, second])
    assert "secret-session" not in serialized
    assert "secret-device" not in serialized
    assert first["field_provenance"]["prices"] == "api"
    assert "fetched_at" in first


@pytest.mark.asyncio
async def test_api_404_uses_matching_jsonld_fallback():
    html = '<script type="application/ld+json">{"@type":"Product","sku":"21","name":"Товар","offers":{"price":"109.99"}}</script>'
    page = _Page([_ApiResponse(404)], html)
    await bootstrap_lenta(page, 1000)

    result = await fetch_lenta_item("21", LentaItemRequest(url="https://lenta.com/product/a-21/"), page)

    assert result["source"] == "html_jsonld"
    assert result["provenance"]["fallback_reason"] == "api_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_retryable_api_status_does_not_fallback(status):
    page = _Page([_ApiResponse(status)], "<script type='application/ld+json'>{}</script>")
    await bootstrap_lenta(page, 1000)

    with pytest.raises(HTTPException) as error:
        await fetch_lenta_item("21", LentaItemRequest(url="https://lenta.com/product/a-21/"), page)
    assert error.value.status_code == status
    assert page.goto_calls == ["https://lenta.com/catalog/"]


@pytest.mark.asyncio
async def test_jsonld_fallback_rejects_mismatched_product():
    html = '<script type="application/ld+json">{"@type":"Product","sku":"999","name":"Чужой"}</script>'
    page = _Page([_ApiResponse(404)], html)
    await bootstrap_lenta(page, 1000)

    with pytest.raises(HTTPException) as error:
        await fetch_lenta_item("21", LentaItemRequest(url="https://lenta.com/product/a-21/"), page)
    assert error.value.status_code == 502

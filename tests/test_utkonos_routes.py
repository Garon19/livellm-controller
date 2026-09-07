from main import app


def test_openapi_exposes_utkonos_session_routes():
    paths = app.openapi()["paths"]

    assert "/utkonos/bootstrap" in paths
    assert "/utkonos/categories" in paths
    assert "/utkonos/catalog/items" in paths
    assert "/utkonos/items/{product_id}" in paths
    assert "/utkonos/metrics" in paths
    assert "/lenta/bootstrap" in paths
    assert "/lenta/items/{product_id}" in paths

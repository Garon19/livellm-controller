import importlib.util


def test_controller_runtime_includes_a_websocket_backend():
    """Uvicorn must accept CDP WebSocket Upgrade requests from the proxy."""
    assert importlib.util.find_spec("websockets") is not None

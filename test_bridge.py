from fastapi.testclient import TestClient
import pytest

import bridge
from bridge import create_app

TOKEN = "a" * 32


@pytest.mark.parametrize("path", ["/v1/health", "/v1/targets"])
def test_endpoints_require_authentication(path):
    client = TestClient(create_app(TOKEN))
    response = client.get(path, headers={"X-Request-ID": "test"})
    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "test"
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    response = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_short_token_rejected():
    with pytest.raises(ValueError):
        create_app("short")


def test_health_and_no_targets():
    client = TestClient(create_app(TOKEN), headers={"Authorization": f"Bearer {TOKEN}"})
    assert client.get("/v1/health").json()["api_version"] == "v1"
    assert client.get("/v1/targets").json() == {"targets": []}


@pytest.mark.parametrize("host", [None, "192.168.0.105", "0.0.0.0", "::1"])
def test_console_listen_address_keeps_authentication(monkeypatch, tmp_path, host):
    token_file = tmp_path / "bridge.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    argv = ["bridge.py", "--mock", "--token-file", str(token_file)]
    if host is not None:
        argv.extend(["--host", host])
    monkeypatch.setattr("sys.argv", argv)
    servers = []
    monkeypatch.setattr(bridge.uvicorn, "run", lambda app, **kwargs: servers.append((app, kwargs)))

    bridge.main()

    app, options = servers.pop()
    assert options["host"] == (host or "127.0.0.1")
    assert options["port"] == 19827
    client = TestClient(app)
    assert client.get("/v1/health").status_code == 401
    assert client.get("/v1/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_console_invalid_listen_address_does_not_start(monkeypatch, tmp_path):
    token_file = tmp_path / "bridge.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "bridge.py", "--mock", "--token-file", str(token_file),
        "--host", "http://192.168.0.105:19827",
    ])
    monkeypatch.setattr(bridge.uvicorn, "run", lambda *args, **kwargs: pytest.fail("Server started"))
    with pytest.raises(SystemExit) as exc:
        bridge.main()
    assert exc.value.code == 2

from fastapi.testclient import TestClient
import pytest

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

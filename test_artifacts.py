from fastapi.testclient import TestClient

from bridge import create_app


def test_upload_hash_range_and_path_guard(tmp_path):
    with TestClient(
        create_app("a" * 32, data_dir=tmp_path), headers={"Authorization": "Bearer " + "a" * 32}
    ) as client:
        response = client.post(
            "/v1/artifacts", files={"file": ("test.txt", b"abcdef", "text/plain")}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["size_bytes"] == 6
        fetched = client.get(body["download_url"])
        assert fetched.content == b"abcdef"
        assert fetched.headers["etag"] == '"' + body["sha256"] + '"'
        ranged = client.get(body["download_url"], headers={"Range": "bytes=1-3"})
        assert ranged.status_code == 206
        assert ranged.content == b"bcd"
        assert (
            client.post(
                "/v1/artifacts", files={"file": ("../escape.txt", b"x", "text/plain")}
            ).status_code
            == 422
        )
        path = tmp_path / "artifacts" / body["artifact_id"]
        path.write_bytes(b"changed")
        assert client.get(body["download_url"]).status_code == 409

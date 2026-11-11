import time

from fastapi.testclient import TestClient

from bridge import create_app
from config import mock_targets
from job_store import Store

TOKEN = "t" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def request(key="test", benchmark="mock-kb"):
    return {
        "idempotency_key": key,
        "target_id": "mock-knowledge-base",
        "input": {"benchmark_id": benchmark},
        "timeout_seconds": 1,
    }


def wait(client, job):
    for _ in range(200):
        state = client.get(f"/v1/jobs/{job}").json()
        if state["status"] not in {"running", "queued"}:
            return state
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def test_http_job_lifecycle_and_replay(tmp_path):
    app = create_app(TOKEN, data_dir=tmp_path, targets=mock_targets())
    with TestClient(app, headers=HEADERS) as client:
        created = client.post("/v1/jobs", json=request())
        assert created.status_code == 202, created.text
        job = created.json()["job_id"]
        assert wait(client, job)["status"] == "succeeded"
        output = client.get(f"/v1/jobs/{job}/result").json()
        assert output["output"]["results"][0]["output"]["answer"] == "Mock answer"
        assert client.post("/v1/jobs", json=request()).json()["job_id"] == job
        conflict = client.post("/v1/jobs", json={**request(), "timeout_seconds": 2})
        assert conflict.status_code == 409
        events = client.get(f"/v1/jobs/{job}/events").json()["events"]
        assert [e["seq"] for e in events] == [1, 2, 3]
        assert len(client.get(f"/v1/jobs/{job}/events?after_seq=2").json()["events"]) == 1
        assert (
            client.get("/v1/jobs/lookup", params={"idempotency_key": "test"}).json()["job_id"]
            == job
        )
    with TestClient(
        create_app(TOKEN, data_dir=tmp_path, targets=mock_targets()), headers=HEADERS
    ) as client:
        assert client.post("/v1/jobs", json=request()).json()["job_id"] == job
        assert client.get(f"/v1/jobs/{job}/result").json() == output


def test_queue_cancel_timeout_and_crash(tmp_path):
    with TestClient(
        create_app(TOKEN, data_dir=tmp_path, targets=mock_targets()), headers=HEADERS
    ) as client:
        first = client.post("/v1/jobs", json=request("slow", "mock-timeout")).json()["job_id"]
        second = client.post("/v1/jobs", json=request("queued")).json()["job_id"]
        assert client.get(f"/v1/jobs/{first}/result").status_code == 409
        assert client.post(f"/v1/jobs/{second}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/v1/jobs/{second}/cancel").json()["status"] == "cancelled"
        assert wait(client, first)["status"] == "timed_out"
        crash = client.post("/v1/jobs", json=request("crash", "mock-crash")).json()["job_id"]
        assert wait(client, crash)["error"]["code"] == "CONNECTOR_CRASHED"
        running = client.post("/v1/jobs", json=request("cancel", "mock-timeout")).json()["job_id"]
        assert client.post(f"/v1/jobs/{running}/cancel").json()["status"] == "cancelled"


def test_restart_never_replays_pending_job(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    state, _ = store.create(
        {
            **request(),
            "operation": "run_benchmark",
            "request_id": "",
            "metadata": {},
            "input_artifacts": [],
        }
    )
    store.db.close()
    with TestClient(
        create_app(TOKEN, data_dir=tmp_path, targets=mock_targets()), headers=HEADERS
    ) as client:
        assert client.get(f"/v1/jobs/{state['job_id']}").json()["status"] == "interrupted"


def test_no_arbitrary_script_or_benchmark():
    with TestClient(create_app(TOKEN, targets=mock_targets()), headers=HEADERS) as client:
        assert (
            client.post("/v1/jobs", json={**request(), "script": "anything.py"}).status_code == 422
        )
        assert client.post("/v1/jobs", json=request(benchmark="not-configured")).status_code == 422
        assert client.get("/v1/targets/mock-knowledge-base").json()["max_concurrency"] == 1


def test_internal_exception_logged_without_token_or_api_traceback(monkeypatch, caplog, tmp_path):
    import job_manager

    async def broken_connector(*args, **kwargs):
        raise RuntimeError("Simulated connector failure " + TOKEN)

    monkeypatch.setattr(job_manager, "execute", broken_connector)
    with TestClient(
        create_app(TOKEN, data_dir=tmp_path, targets=mock_targets()), headers=HEADERS
    ) as client:
        job = client.post("/v1/jobs", json=request("internal-error")).json()["job_id"]
        assert wait(client, job)["error"]["code"] == "CONNECTOR_INTERNAL_ERROR"
        result = client.get(f"/v1/jobs/{job}/result")
        assert result.json()["status"] == "failed"
        assert TOKEN not in result.text
        assert "Traceback" not in result.text
        assert "Simulated connector failure" not in result.text
    assert f"Bridge job {job} failed unexpectedly" in caplog.text
    assert "RuntimeError: Simulated connector failure [REDACTED]" in caplog.text
    assert TOKEN not in caplog.text

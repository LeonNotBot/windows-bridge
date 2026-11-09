import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import create_app
from config import mock_targets
from connectors.python_script import ScriptTarget
from job_store import Store

HEADERS = {"Authorization": "Bearer " + "t" * 32}


def submit(client, key="report", benchmark="smoke", arguments=None):
    response = client.post(
        "/v1/jobs",
        json={
            "idempotency_key": key,
            "target_id": "mock-fw-coding-report",
            "timeout_seconds": 10,
            "input": {"benchmark_id": benchmark, "arguments": arguments or []},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def wait(client, job_id):
    for _ in range(500):
        state = client.get("/v1/jobs/" + job_id).json()
        if state["status"] not in {"queued", "running"}:
            return state
        time.sleep(0.01)
    raise AssertionError("No terminal state")


def test_report_bundle_progress_and_arguments(tmp_path):
    app = create_app("t" * 32, targets=mock_targets(), data_dir=tmp_path)
    with TestClient(app, headers=HEADERS) as client:
        assert (
            client.get("/v1/targets/mock-fw-coding-report").json()["argument_mode"]
            == "passthrough"
        )
        assert (
            client.get("/v1/targets/mock-knowledge-base").json()["argument_mode"]
            == "allowlist"
        )
        job = submit(client, arguments=["--cases", "题目 01,题目 02", "--include-log"])
        assert wait(client, job)["status"] == "succeeded"
        result = client.get(f"/v1/jobs/{job}/result").json()["output"]
        assert result["kind"] == "report_bundle"
        files = {file["name"]: file for file in result["files"]}
        assert {
            "report.md",
            "case-results.xlsx",
            "assets/sample.png",
            "execution.log",
        } <= files.keys()
        assert "progress.json" not in files
        assert "题目 01,题目 02" in client.get(files["report.md"]["download_url"]).text
        events = client.get(f"/v1/jobs/{job}/events").json()["events"]
        assert any(
            e["type"] == "progress" and e["data"].get("total") == 2 for e in events
        )
        assert (
            client.post(
                "/v1/jobs",
                json={
                    "idempotency_key": "bad",
                    "target_id": "mock-fw-coding-report",
                    "input": {
                        "benchmark_id": "smoke",
                        "arguments": ["--output-dir", "elsewhere"],
                    },
                },
            ).status_code
            == 422
        )
    with TestClient(
        create_app("t" * 32, targets=mock_targets(), data_dir=tmp_path), headers=HEADERS
    ) as client:
        assert client.get(f"/v1/jobs/{job}/result").json()["output"] == result


@pytest.mark.parametrize(
    "benchmark, expected",
    [
        ("missing-report", "failed"),
        ("crash", "failed"),
        ("invalid-progress", "succeeded"),
        ("no-progress", "succeeded"),
        ("manual", "succeeded"),
    ],
)
def test_report_modes(tmp_path, benchmark, expected):
    with TestClient(
        create_app("t" * 32, targets=mock_targets(), data_dir=tmp_path), headers=HEADERS
    ) as client:
        job = submit(client, benchmark=benchmark)
        assert wait(client, job)["status"] == expected
        if benchmark == "manual":
            assert client.get(f"/v1/jobs/{job}/result").json()["output"][
                "requires_review"
            ]


def test_bundle_rejects_symlink(tmp_path):
    from report_bundle import collect
    from connectors.python_script import ProtocolError

    source = tmp_path / "output"
    source.mkdir()
    (source / "report.md").write_text("# report")
    (source / "leak.txt").symlink_to(tmp_path / "secret.txt")
    (tmp_path / "secret.txt").write_text("private")
    store = Store(tmp_path / "test.sqlite3")
    with pytest.raises(ProtocolError):
        collect(source, mock_targets()["mock-fw-coding-report"], store, tmp_path)
    store.db.close()


def test_process_host_consumes_control_gate(tmp_path):
    import subprocess
    import sys

    gate = tmp_path / "start.gate"
    gate.touch()
    child = tmp_path / "child.py"
    child.write_text(
        'from pathlib import Path\nassert not Path("start.gate").exists()\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "process_host.py"),
            str(gate),
            sys.executable,
            str(child),
        ],
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 0


def test_report_parameter_policy_can_be_restricted_and_template_options_are_protected():
    from dataclasses import replace

    target = mock_targets()["mock-fw-coding-report"]
    assert target.validate_arguments(["--model", "中文模型", "--verbose"]) == [
        "--model",
        "中文模型",
        "--verbose",
    ]
    with pytest.raises(ValueError):
        replace(target, argument_mode="allowlist").validate_arguments(
            ["--model", "custom"]
        )
    template = replace(
        target,
        argument_template=("--suite", "{benchmark_id}", "--reports", "{output_dir}"),
    )
    for option in ["--suite", "--reports=/tmp", "--rep"]:
        with pytest.raises(ValueError):
            template.validate_arguments([option, "elsewhere"])

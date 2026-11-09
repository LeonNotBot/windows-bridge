import asyncio
import os
from pathlib import Path

import pytest

from connectors.python_script import ScriptTarget, execute

SCRIPT = Path(__file__).parent / "connectors" / "mock_benchmark.py"


@pytest.mark.parametrize("mode", ["file", "stdout"])
def test_success_both_result_modes(mode):
    result = asyncio.run(execute(ScriptTarget(SCRIPT, ("mock-kb",), result_mode=mode), "mock-kb"))
    assert result["status"] == "succeeded"
    assert result["output"]["results"][0]["output"]["answer"] == "Mock answer"


def test_business_error_is_preserved():
    result = asyncio.run(execute(ScriptTarget(SCRIPT, ("mock-failure",)), "mock-failure"))
    assert result["status"] == "failed"
    assert result["error"]["code"] == "KB_NOT_READY"
    assert len(result["output"]["results"]) == 1


@pytest.mark.parametrize(
    "benchmark, code, timeout",
    [
        ("mock-crash", "CONNECTOR_CRASHED", 5),
        ("mock-invalid", "CONNECTOR_PROTOCOL_ERROR", 5),
        ("mock-timeout", "JOB_TIMED_OUT", 0.1),
    ],
)
def test_execution_failures(benchmark, code, timeout):
    result = asyncio.run(
        execute(ScriptTarget(SCRIPT, (benchmark,), timeout_seconds=timeout), benchmark)
    )
    assert result["status"] == "failed"
    assert result["error"]["code"] == code


def test_unconfigured_benchmark_is_rejected():
    result = asyncio.run(execute(ScriptTarget(SCRIPT, ("mock-kb",)), "not-registered"))
    assert result["error"]["code"] == "INVALID_REQUEST"


def test_bounded_stdout(tmp_path):
    script = tmp_path / "large.py"
    script.write_text("print('x' * 4096)")
    result = asyncio.run(
        execute(ScriptTarget(script, ("mock",), result_mode="stdout", max_result_bytes=100), "mock")
    )
    assert result["error"]["code"] == "CONNECTOR_PROTOCOL_ERROR"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX process-group check; Windows needs real-machine validation"
)
def test_cancellation_reaps_process(monkeypatch):
    original = asyncio.create_subprocess_exec
    started = []

    async def tracked(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)

    async def scenario():
        task = asyncio.create_task(execute(ScriptTarget(SCRIPT, ("mock-timeout",)), "mock-timeout"))
        while not started:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started[0].returncode is not None

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX process check; Windows covered by lifecycle tests"
)
def test_cancellation_during_spawn_recovers_process_handle(monkeypatch):
    original = asyncio.create_subprocess_exec
    started = []

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        await asyncio.sleep(0.1)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)

    async def scenario():
        task = asyncio.create_task(execute(ScriptTarget(SCRIPT, ("mock-timeout",)), "mock-timeout"))
        while not started:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started[0].returncode is not None

    asyncio.run(scenario())

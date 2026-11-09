"""Run on Windows with pywin32 installed; intentionally skipped elsewhere."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires a real Windows host and pywin32")


def alive(pid):
    import win32api
    import win32con
    import win32process

    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
    except Exception:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == 259
    finally:
        handle.Close()


def test_job_close_terminates_assigned_process():
    from windows_job import WindowsJob

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job = WindowsJob()
    try:
        job.assign(process.pid)
    finally:
        job.close()
    assert process.wait(timeout=10) is not None
    assert not alive(process.pid)


def test_timeout_reaps_descendant(tmp_path):
    from connectors.python_script import ScriptTarget, execute

    pid_path = tmp_path / "child.pid"
    script = tmp_path / "spawn.py"
    script.write_text(
        'import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\nPath('
        + repr(str(pid_path))
        + ").write_text(str(p.pid))\ntime.sleep(60)\n"
    )
    result = asyncio.run(execute(ScriptTarget(script, ("test",), timeout_seconds=2), "test"))
    assert result["error"]["code"] == "JOB_TIMED_OUT"
    assert pid_path.is_file(), "Script did not reach child creation before timeout"
    child_pid = int(pid_path.read_text())
    for _ in range(100):
        if not alive(child_pid):
            return
        time.sleep(0.05)
    pytest.fail("Descendant was not terminated")

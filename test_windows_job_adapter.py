"""Exercise the pywin32 argument boundary without requiring a Windows host."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def adapter(monkeypatch):
    names, handles, configured = [], [], []

    class Handle:
        closed = False

        def Close(self):
            self.closed = True

    def create(attributes, name):
        if not isinstance(name, str):
            raise TypeError("None is not a valid string in this context")
        assert name
        names.append(name)
        handle = Handle()
        handles.append(handle)
        return handle

    api = SimpleNamespace(
        CreateJobObject=create,
        QueryInformationJobObject=lambda *args: {"BasicLimitInformation": {"LimitFlags": 0x10}},
        SetInformationJobObject=lambda handle, kind, limits: configured.append(limits),
        JobObjectExtendedLimitInformation=9,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32job", api)
    spec = importlib.util.spec_from_file_location(
        "tested_windows_job", Path(__file__).with_name("windows_job.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WindowsJob, api, names, handles, configured


def test_unique_string_names_and_kill_on_close(adapter):
    WindowsJob, api, names, handles, configured = adapter
    first, second = WindowsJob(), WindowsJob()
    assert len(set(names)) == 2
    assert all(value["BasicLimitInformation"]["LimitFlags"] == 0x2010 for value in configured)
    first.close()
    first.close()
    assert handles[0].closed
    assert not handles[1].closed
    second.close()
    assert handles[1].closed


def test_handle_closed_if_setting_limits_fails(adapter):
    WindowsJob, api, names, handles, configured = adapter

    def fail(*args):
        raise OSError("Could not configure limits")

    api.SetInformationJobObject = fail
    with pytest.raises(OSError, match="Could not configure limits"):
        WindowsJob()
    assert handles[0].closed

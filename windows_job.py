"""Windows Job Object ownership; imported only on Windows."""

import win32api
import win32con
import win32job


class WindowsJob:
    def __init__(self):
        self.handle = win32job.CreateJobObject(None, None)
        limits = win32job.QueryInformationJobObject(
            self.handle, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(
            self.handle, win32job.JobObjectExtendedLimitInformation, limits
        )

    def assign(self, pid):
        process = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid
        )
        try:
            win32job.AssignProcessToJobObject(self.handle, process)
        finally:
            process.Close()

    def close(self):
        if self.handle is not None:
            self.handle.Close()
            self.handle = None

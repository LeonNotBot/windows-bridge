"""Wait for Job Object assignment before launching the actual target script."""

import subprocess
import sys
import time
from pathlib import Path


def main():
    gate = Path(sys.argv[1])
    deadline = time.monotonic() + 30
    while not gate.is_file():
        if time.monotonic() >= deadline:
            return 30
        time.sleep(0.01)
    # This control file shares the temporary workspace with report output.
    # Remove it before the business script runs so it cannot become an attachment.
    gate.unlink()
    return subprocess.call(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())

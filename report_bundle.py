"""Collect a report directory without interpreting business scores or results."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from artifacts import LIMIT, TYPES
from connectors.python_script import ProtocolError
from job_store import now

REPORT_TYPES = {".md": "markdown", ".html": "html", ".pdf": "pdf"}


def regular(path):
    value = path.lstat()
    return stat.S_ISREG(value.st_mode) and not (
        getattr(value, "st_file_attributes", 0) & 0x400
    )


def collect(directory, target, store, root):
    relative = Path(target.report_file)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in target.report_file
        or ":" in target.report_file
    ):
        raise ProtocolError("Invalid main report path")
    main = directory / relative
    if not main.exists() or main.suffix.lower() not in REPORT_TYPES:
        raise ProtocolError("Main report is missing")
    paths = []
    total = 0
    for folder, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            path = Path(folder) / name
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise ProtocolError("Report directory contains a link or reparse point")
        for name in names:
            path = Path(folder) / name
            if path in (
                directory / "progress.json",
                directory / "review.required",
            ) or path.name.endswith(".tmp"):
                continue
            if not regular(path) or path.suffix.lower() not in TYPES:
                raise ProtocolError("Unsupported report artifact")
            size = path.stat().st_size
            total += size
            if size > LIMIT or total > 128 * 1024 * 1024 or len(paths) >= 100:
                raise ProtocolError("Report bundle exceeds limits")
            paths.append(path)
    if main not in paths or main.stat().st_size == 0:
        raise ProtocolError("Main report is empty")
    storage = root / "artifacts"
    storage.mkdir(exist_ok=True)
    if storage.resolve() != storage:
        raise ProtocolError("Artifact storage is invalid")
    files, copied = [], []
    try:
        with store.db:
            for path in paths:
                artifact_id = uuid.uuid4().hex
                destination = storage / artifact_id
                with path.open("rb") as source, destination.open("xb") as output:
                    copied.append(destination)
                    shutil.copyfileobj(source, output, length=65536)
                    output.flush()
                    os.fsync(output.fileno())
                content = destination.read_bytes()
                body = {
                    "artifact_id": artifact_id,
                    "name": path.relative_to(directory).as_posix(),
                    "content_type": TYPES[path.suffix.lower()],
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "download_url": "/v1/artifacts/" + artifact_id,
                    "created_at": now(),
                }
                store.db.execute(
                    "INSERT INTO artifacts VALUES (?,?)",
                    (artifact_id, json.dumps(body)),
                )
                files.append(body)
        main_id = next(
            item["artifact_id"] for item in files if item["name"] == relative.as_posix()
        )
        return {
            "status": "succeeded",
            "output": {
                "kind": "report_bundle",
                "format": REPORT_TYPES[main.suffix.lower()],
                "main_artifact_id": main_id,
                "files": files,
                "requires_review": (directory / "review.required").is_file(),
            },
            "error": None,
        }
    except BaseException:
        for path in copied:
            path.unlink(missing_ok=True)
        raise

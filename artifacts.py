"""Registered artifacts only: bounded uploads, MIME allowlist, integrity checks."""

import hashlib
import json
from pathlib import Path, PureWindowsPath
import stat
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, UploadFile, HTTPException
from fastapi.responses import FileResponse

LIMIT = 16 * 1024 * 1024
TYPES = {
    ".md": "text/markdown",
    ".html": "text/html",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".zip": "application/zip",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".json": "application/json",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def safe(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def artifact_router(app):
    router = APIRouter()

    @router.post("/v1/artifacts", status_code=201)
    async def upload(file: UploadFile):
        filename = file.filename or ""
        suffix = Path(filename).suffix.lower()
        if (
            "/" in filename
            or "\\" in filename
            or ":" in filename
            or PureWindowsPath(filename).name != filename
            or TYPES.get(suffix) != file.content_type
        ):
            raise HTTPException(422, "Artifact filename or content type is not allowed")
        store = app.state.manager.store
        root = app.state.root / "artifacts"
        root.mkdir(exist_ok=True)
        if root.resolve() != root:
            raise HTTPException(409, "Artifact storage directory is invalid")
        artifact_id = uuid.uuid4().hex
        path = root / artifact_id
        size, digest = 0, hashlib.sha256()
        try:
            with path.open("xb") as handle:
                while chunk := await file.read(65536):
                    size += len(chunk)
                    if size > LIMIT:
                        raise HTTPException(413, "Artifact exceeds size limit")
                    digest.update(chunk)
                    handle.write(chunk)
            body = {
                "artifact_id": artifact_id,
                "name": filename,
                "content_type": file.content_type,
                "size_bytes": size,
                "sha256": digest.hexdigest(),
                "download_url": "/v1/artifacts/" + artifact_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            with store.db:
                store.db.execute(
                    "INSERT INTO artifacts VALUES (?,?)",
                    (artifact_id, json.dumps(body)),
                )
            return body
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @router.get("/v1/artifacts/{artifact_id}")
    async def download(artifact_id: str):
        store = app.state.manager.store
        row = store.db.execute(
            "SELECT body FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Artifact not found")
        body = json.loads(row[0])
        path = app.state.root / "artifacts" / artifact_id
        if path.parent.resolve() != path.parent:
            raise HTTPException(409, "Artifact storage directory is invalid")
        if (
            not path.exists()
            or not safe(path)
            or path.stat().st_size != body["size_bytes"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != body["sha256"]
        ):
            raise HTTPException(409, "Artifact integrity check failed")
        return FileResponse(
            path,
            media_type=body["content_type"],
            filename=body["name"],
            headers={"ETag": '"' + body["sha256"] + '"'},
        )

    return router

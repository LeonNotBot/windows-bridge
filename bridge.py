"""Authenticated, single-process Windows Bridge HTTP service."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import ipaddress
import json
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from config import load_targets, mock_targets
from job_manager import Manager
from job_store import Store, Conflict, TERMINAL
from artifacts import artifact_router


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(default="", max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    target_id: str = Field(min_length=1, max_length=128)
    operation: str = "run_benchmark"
    timeout_seconds: int = Field(default=300, ge=1, le=86400, strict=True)
    input: dict
    metadata: dict = Field(default_factory=dict)
    input_artifacts: list = Field(default_factory=list, max_length=0)


def error(code, message, status=400):
    raise HTTPException(status, {"code": code, "message": message, "retryable": False})


def create_app(token: str, *, data_dir=None, targets=None) -> FastAPI:
    if len(token) < 32:
        raise ValueError("Token must contain at least 32 characters")
    configured = targets or {}

    @asynccontextmanager
    async def lifespan(app):
        temporary = tempfile.TemporaryDirectory() if data_dir is None else None
        root = Path(data_dir or temporary.name).resolve()
        root.mkdir(parents=True, exist_ok=True)
        # One scheduler must own this database. No multi-worker Uvicorn deployment.
        lock = (root / "bridge.lock").open("a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt

                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("Bridge data directory is already in use")
        store = Store(root / "bridge.sqlite3")
        store.recover()

        def clean():
            cutoff = (
                (datetime.now(timezone.utc) - timedelta(days=7))
                .isoformat()
                .replace("+00:00", "Z")
            )
            for artifact_id in store.clean(cutoff):
                directory = root / "artifacts"
                if (
                    directory.resolve() == directory
                    and len(artifact_id) == 32
                    and all(ch in "0123456789abcdef" for ch in artifact_id)
                ):
                    (directory / artifact_id).unlink(missing_ok=True)

        clean()

        async def periodic_clean():
            while True:
                await asyncio.sleep(3600)
                clean()

        cleaner = asyncio.create_task(periodic_clean())
        manager = Manager(store, configured, token, root)
        app.state.manager = manager
        app.state.root = root
        try:
            yield
        finally:
            cleaner.cancel()
            await asyncio.gather(cleaner, return_exceptions=True)
            await manager.close()
            store.db.close()
            lock.close()
            if temporary:
                temporary.cleanup()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    started_at, started_monotonic = datetime.now(timezone.utc), time.monotonic()

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"code": "HTTP_ERROR", "message": str(exc.detail), "retryable": False}
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": detail,
                "request_id": request.headers.get("X-Request-ID", ""),
            },
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", "")
        scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
        valid = scheme == "Bearer" and hmac.compare_digest(
            supplied.encode(), token.encode()
        )
        if not valid:
            response = JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Authentication required",
                        "retryable": False,
                    },
                    "request_id": request_id,
                },
            )
        else:
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > (
                    16 * 1024 * 1024 + 65536
                    if request.url.path == "/v1/artifacts"
                    else 1024 * 1024
                ):
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "REQUEST_TOO_LARGE",
                                "message": "Request exceeds limit",
                                "retryable": False,
                            }
                        },
                    )
                    break
                chunks.append(chunk)
            else:
                request._body = b"".join(chunks)
                response = await call_next(request)
        if request_id:
            response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/v1/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.2.0-dev",
            "api_version": "v1",
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "uptime_seconds": int(time.monotonic() - started_monotonic),
        }

    def target_info(key):
        target = configured[key]
        return {
            "id": key,
            "display_name": key,
            "connector_id": "python-script",
            "connector_version": "1.0.0",
            "status": "ready",
            "operations": ["run_benchmark"],
            "capabilities": ["execute", "cancel"],
            "max_concurrency": 1,
            "benchmarks": list(target.benchmarks),
            "result_mode": target.result_mode,
            "argument_mode": target.argument_mode,
            "reserved_arguments": sorted(target.reserved_arguments),
            "allowed_arguments": list(target.allowed_arguments),
            "allowed_switches": list(target.allowed_switches),
        }

    @app.get("/v1/targets")
    async def targets_list():
        return {"targets": [target_info(key) for key in configured]}

    @app.get("/v1/targets/{target_id}")
    async def target_get(target_id: str):
        if target_id not in configured:
            error("TARGET_NOT_FOUND", "Target not found", 404)
        return target_info(target_id)

    def get(job_id):
        state = app.state.manager.store.get(job_id)
        if state is None:
            error("JOB_NOT_FOUND", "Job not found", 404)
        return state

    def public(state):
        return {
            k: v
            for k, v in state.items()
            if k not in {"result", "input", "metadata", "idempotency_key"}
        }

    @app.get("/v1/jobs/lookup")
    async def lookup(idempotency_key: str = Query(min_length=1, max_length=256)):
        state = app.state.manager.store.by_key(idempotency_key)
        if not state:
            error("JOB_NOT_FOUND", "Job not found", 404)
        return public(state)

    @app.post("/v1/jobs", status_code=202)
    async def create(body: JobRequest):
        target = configured.get(body.target_id)
        if not target:
            error("TARGET_NOT_FOUND", "Target not configured", 404)
        if (
            body.operation != "run_benchmark"
            or not {"benchmark_id"} <= set(body.input)
            or set(body.input) - {"benchmark_id", "arguments"}
        ):
            error("INVALID_REQUEST", "Expected run_benchmark with benchmark_id", 422)
        if body.input["benchmark_id"] not in target.benchmarks:
            error("INVALID_REQUEST", "Benchmark is not configured for this target", 422)
        try:
            target.validate_arguments(body.input.get("arguments", []))
        except ValueError:
            error(
                "INVALID_REQUEST", "Run arguments are not allowed for this target", 422
            )
        manager = app.state.manager
        if len(manager.tasks) >= 100 and not manager.store.by_key(body.idempotency_key):
            error("QUEUE_FULL", "Bridge queue is full", 429)
        try:
            state, created = manager.store.create(body.model_dump())
        except Conflict:
            error(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key was used with different input",
                409,
            )
        if created:
            manager.submit(state)
        return public(state)

    @app.get("/v1/jobs/{job_id}")
    async def status(job_id: str):
        return public(get(job_id))

    @app.get("/v1/jobs/{job_id}/events")
    async def events(
        job_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        get(job_id)
        return {"events": app.state.manager.store.events(job_id, after_seq, limit)}

    @app.get("/v1/jobs/{job_id}/result")
    async def result(job_id: str):
        state = get(job_id)
        if state["status"] not in TERMINAL:
            error("JOB_NOT_FINISHED", "Job has not finished", 409)
        response = state["result"] or {"output": None, "error": state["error"]}
        # A target must never echo the Bridge token back into API output.
        clean = json.loads(
            json.dumps(response, ensure_ascii=False).replace(token, "[REDACTED]")
        )
        return {**clean, "job_id": job_id, "status": state["status"]}

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        get(job_id)
        return public(await app.state.manager.cancel(job_id))

    app.include_router(artifact_router(app))
    return app


def main():
    parser = argparse.ArgumentParser(description="Windows Bridge")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19827)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mock", action="store_true")
    group.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            parser.error("Only loopback addresses are supported; use a secure tunnel")
        app = create_app(
            args.token_file.read_text(encoding="utf-8").strip(),
            data_dir=args.data_dir,
            targets=mock_targets() if args.mock else load_targets(args.config),
        )
    except (OSError, UnicodeError, ValueError):
        parser.error("Invalid token file, target configuration or listen address")
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()

"""Outbound-only node agent; Core runs in-process without a listening port."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import uuid

import httpx
from websockets.asyncio.client import connect

MAX_MESSAGE = 2 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


def control_url(value, allow_insecure_local=False):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Expected a control-plane HTTPS origin")
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname == "localhost"
        if not allow_insecure_local or not local:
            raise ValueError("HTTP is allowed only for explicit loopback development")
    return value.rstrip("/")


def save_credentials(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".bridge-credential-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def allowed_rpc(method, path):
    if method == "GET":
        return bool(re.fullmatch(r"/v1/(?:health|targets(?:/[A-Za-z0-9_-]{1,128})?|jobs/(?:lookup|[a-f0-9]{32}(?:/(?:events|result))?))", path))
    return method == "POST" and bool(re.fullmatch(r"/v1/jobs(?:/[a-f0-9]{32}/cancel)?", path))


class Agent:
    def __init__(self, app, local_token, *, origin, node_id, credentials_file,
                 enrollment_code_file=None, allow_insecure_local=False, reenroll=False, proxy=None):
        self.app, self.local_token = app, local_token
        self.origin = control_url(origin, allow_insecure_local)
        self.node_id = str(uuid.UUID(str(node_id)))
        self.credentials_file = Path(credentials_file)
        self.enrollment_code_file = Path(enrollment_code_file) if enrollment_code_file else None
        self.credential = None
        self.reenroll = reenroll
        if proxy and urlsplit(proxy).scheme not in {"http", "https"}:
            raise ValueError("Only explicitly configured HTTP/HTTPS proxies are supported")
        self.proxy = proxy
        self.connected = asyncio.Event()

    async def enroll(self, http):
        if self.credentials_file.exists() and not self.reenroll:
            if self.credentials_file.stat().st_size > 4096:
                raise ValueError("Invalid credential file")
            saved = json.loads(self.credentials_file.read_text(encoding="utf-8"))
            if saved.get("origin") != self.origin or saved.get("node_id") != self.node_id:
                raise ValueError("Credential belongs to a different node or platform")
            self.credential = saved["credential"]
        else:
            if not self.enrollment_code_file:
                raise ValueError("First start requires an enrollment code file")
            code = self.enrollment_code_file.read_text(encoding="utf-8").strip()
            if not 32 <= len(code) <= 128:
                raise ValueError("Invalid enrollment code")
            response = await http.post("/api/v1/bridge-agent/enroll", json={"node_id": self.node_id, "code": code})
            if response.status_code != 200:
                raise ValueError("Enrollment denied; ask an administrator to issue a new code")
            saved = response.json()
            if saved.get("node_id") != self.node_id:
                raise ValueError("Unexpected enrollment identity")
            self.credential = saved["credential"]
            save_credentials(self.credentials_file, {**saved, "origin": self.origin})
        if not isinstance(self.credential, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.credential):
            raise ValueError("Invalid stored credential")
        http.headers["Authorization"] = "Bearer " + self.credential

    async def process(self, message, local, http):
        command_id = uuid.UUID(message["id"])
        kind, request = message["kind"], message["request"]
        path = f"/api/v1/bridge-agent/commands/{command_id}/file"
        if kind == "rpc":
            method, target = request["method"], request["path"]
            if not allowed_rpc(method, target):
                raise ValueError("Unsupported Bridge operation")
            response = await local.request(method, target, json=request.get("payload"), params=request.get("params"))
            if len(response.content) > MAX_MESSAGE - 1024:
                raise ValueError("Core response exceeds limit")
            return response.status_code, response.json()
        if kind == "export":
            artifact_id = request["artifact_id"]
            if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
                raise ValueError("Invalid artifact ID")
            response = await local.get("/v1/artifacts/" + artifact_id)
            if response.status_code != 200:
                return response.status_code, {"error": {"code": "ARTIFACT_NOT_FOUND"}}
            content = response.content
            if (len(content) > min(request["max_bytes"], MAX_FILE)
                    or hashlib.sha256(content).hexdigest() != request["sha256"]):
                raise ValueError("Artifact integrity mismatch")
            response = await http.put(path, content=content, headers={"Content-Type": "application/octet-stream"})
            if response.status_code != 200:
                raise ValueError("Artifact transfer was rejected")
            return 200, {"ok": True}
        if kind == "import":
            name = request["name"]
            if Path(name).name != name or "\\" in name or ":" in name or not name.endswith(".xlsx"):
                raise ValueError("Only a registered review workbook can be imported")
            content = bytearray()
            async with http.stream("GET", path) as response:
                if response.status_code != 200:
                    raise ValueError("Input transfer was rejected")
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_FILE:
                        raise ValueError("Input exceeds limit")
            if len(content) != request["size_bytes"] or hashlib.sha256(content).hexdigest() != request["sha256"]:
                raise ValueError("Input integrity mismatch")
            response = await local.post("/v1/artifacts", files={"file": (name, bytes(content), request["content_type"])})
            return response.status_code, response.json()
        raise ValueError("Unknown node command")

    async def session(self, socket, local, http):
        lock, slots, tasks, pending = asyncio.Lock(), asyncio.Semaphore(8), set(), set()

        async def send(message):
            encoded = json.dumps(message, ensure_ascii=False).replace(self.credential, "[REDACTED]")
            if len(encoded.encode()) > MAX_MESSAGE:
                raise ValueError("Node message exceeds limit")
            async with lock:
                await socket.send(encoded)

        async def heartbeat():
            while True:
                await send({"type": "heartbeat"})
                await asyncio.sleep(5)

        async def handle(message):
            try:
                async with slots:
                    status, body = await self.process(message, local, http)
            except Exception:
                # Never serialize exception text, URLs, credentials or traces.
                status, body = 502, {"error": {"code": "NODE_OPERATION_FAILED", "retryable": True}}
            await send({"type": "reply", "id": message["id"], "status": status, "body": body})

        ticker = asyncio.create_task(heartbeat())
        try:
            async for raw in socket:
                message = json.loads(raw)
                if message.get("type") == "ready":
                    if message.get("node_id") != self.node_id or message.get("protocol") != "1":
                        raise ValueError("Unexpected platform identity")
                    self.connected.set()
                elif message.get("type") == "command":
                    if message["id"] in pending:
                        continue
                    if len(tasks) >= 128:
                        raise ValueError("Node command backlog exceeds limit")
                    pending.add(message["id"])
                    future = asyncio.create_task(handle(message))
                    tasks.add(future)
                    def done(completed, key=message["id"]):
                        tasks.discard(completed)
                        pending.discard(key)
                        if not completed.cancelled():
                            completed.exception()
                    future.add_done_callback(done)
                else:
                    raise ValueError("Unexpected platform message")
        finally:
            self.connected.clear()
            ticker.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(ticker, *tasks, return_exceptions=True)

    async def run(self):
        async with httpx.AsyncClient(base_url=self.origin, timeout=120, follow_redirects=False,
                                     trust_env=False, proxy=self.proxy) as http:
            await self.enroll(http)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://bridge.local",
                    headers={"Authorization": "Bearer " + self.local_token}, timeout=120) as local:
                delay = 1
                while True:
                    try:
                        endpoint = self.origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                        async with connect(endpoint + "/api/v1/bridge-agent/ws",
                                additional_headers={"Authorization": "Bearer " + self.credential},
                                max_size=MAX_MESSAGE, max_queue=16, compression=None,
                                proxy=self.proxy,
                                ping_interval=10, ping_timeout=20, open_timeout=15) as socket:
                            delay = 1
                            logger.info("Bridge node connected")
                            await self.session(socket, local, http)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.connected.clear()
                        logger.warning("Bridge connection unavailable (%s); reconnecting", type(exc).__name__)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

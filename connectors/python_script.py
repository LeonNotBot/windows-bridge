"""Run a preconfigured benchmark script out of process, without a command shell."""

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from benchmark_contract import validate_benchmark, ContractError
from argument_contract import RESERVED_ARGUMENTS, validate_arguments


class ProtocolError(ValueError):
    pass


def default_python():
    # pywin32 Service hosts use pythonservice.exe, which is not a CLI interpreter.
    if Path(sys.executable).name.lower() == "pythonservice.exe":
        return str(Path(sys.prefix) / "python.exe")
    return sys.executable


@dataclass(frozen=True)
class ScriptTarget:
    # Administrator-side configuration, never taken from an HTTP request.
    script: Path
    benchmarks: tuple[str, ...]
    result_mode: str = "file"
    python: str = field(default_factory=default_python)
    timeout_seconds: float = 300
    max_result_bytes: int = 1024 * 1024
    report_file: str = "report.md"
    argument_template: tuple[str, ...] = ()
    allowed_arguments: tuple[str, ...] = ()
    allowed_switches: tuple[str, ...] = ()
    working_dir: Path | None = None
    argument_mode: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "script", self.script.resolve(strict=True))
        if not self.script.is_file() or self.result_mode not in {
            "file",
            "stdout",
            "report",
        }:
            raise ValueError("Invalid script or result mode")
        if self.timeout_seconds <= 0 or self.max_result_bytes <= 0:
            raise ValueError("Invalid execution limits")
        if not self.benchmarks or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", b)
            for b in self.benchmarks
        ):
            raise ValueError("Invalid benchmark allowlist")
        if self.working_dir is not None:
            object.__setattr__(
                self, "working_dir", Path(self.working_dir).resolve(strict=True)
            )
            if not self.working_dir.is_dir():
                raise ValueError("Invalid working directory")
        if self.argument_mode is None:
            object.__setattr__(
                self,
                "argument_mode",
                "passthrough" if self.result_mode == "report" else "allowlist",
            )
        if self.argument_mode not in {"allowlist", "passthrough"}:
            raise ValueError("Invalid argument mode")
        reserved = self.reserved_arguments
        for flag in self.allowed_arguments + self.allowed_switches:
            if not re.fullmatch(r"--[a-zA-Z][a-zA-Z0-9_-]*", flag) or flag in reserved:
                raise ValueError("Invalid or reserved user argument")
        if set(self.allowed_arguments) & set(self.allowed_switches):
            raise ValueError("Argument cannot be both a value and switch")

    def validate_arguments(self, arguments):
        return validate_arguments(
            arguments,
            mode=self.argument_mode,
            allowed=self.allowed_arguments,
            switches=self.allowed_switches,
            reserved=self.reserved_arguments,
        )

    @property
    def reserved_arguments(self):
        return RESERVED_ARGUMENTS | {
            value.partition("=")[0]
            for value in self.argument_template
            if value.startswith("-")
        }


def failure(code, message):
    return {
        "status": "failed",
        "output": None,
        "error": {"code": code, "message": message, "retryable": False},
    }


def validate_result(raw, benchmark):
    try:
        body = json.loads(
            raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("Invalid result JSON") from exc
    if not isinstance(body, dict) or body.get("schema_version") != "1.0":
        raise ProtocolError("Unsupported result schema")
    if (
        not isinstance(body.get("benchmark"), dict)
        or body["benchmark"].get("id") != benchmark
    ):
        raise ProtocolError("Result benchmark mismatch")
    if body.get("status") not in {"succeeded", "failed"} or not isinstance(
        body.get("results"), list
    ):
        raise ProtocolError("Invalid result status or samples")
    error = body.get("error")
    if error is not None and (
        not isinstance(error, dict)
        or not isinstance(error.get("code"), str)
        or not isinstance(error.get("message"), str)
    ):
        raise ProtocolError("Invalid error structure")
    try:
        return validate_benchmark(body, benchmark)
    except ContractError as exc:
        raise ProtocolError(str(exc)) from exc


async def _read_bounded(stream, limit):
    value = bytearray()
    while chunk := await stream.read(65536):
        value.extend(chunk)
        if len(value) > limit:
            raise ProtocolError("Process output exceeds limit")
    return bytes(value)


async def _kill_tree(process):
    if os.name == "nt":
        job = getattr(process, "_bridge_job", None)
        if job:
            job.close()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def execute(
    target: ScriptTarget,
    benchmark: str,
    *,
    arguments=None,
    collect_report=None,
    on_progress=None,
):
    """Return a connector envelope containing the original benchmark response.

    Task cancellation cleans up the process and propagates CancelledError.
    There are no automatic retries of benchmark executions.
    """
    if benchmark not in target.benchmarks:
        return failure("INVALID_REQUEST", "Benchmark is not configured for this target")
    try:
        arguments = target.validate_arguments(arguments or [])
    except ValueError:
        return failure(
            "INVALID_REQUEST", "Run arguments are not allowed for this target"
        )
    with tempfile.TemporaryDirectory(prefix="edison-benchmark-") as directory:
        output_path = Path(directory) / "result.json"
        progress_path = Path(directory) / "progress.json"
        substitutions = {
            "{benchmark_id}": benchmark,
            "{output_dir}": directory,
            "{progress_file}": str(progress_path),
        }
        if target.argument_template:
            values = []
            for value in target.argument_template:
                for key, replacement in substitutions.items():
                    value = value.replace(key, replacement)
                values.append(value)
            command = [target.python, str(target.script), *values, *arguments]
        else:
            command = [
                target.python,
                str(target.script),
                "--benchmark",
                benchmark,
                *arguments,
            ]
        if target.result_mode == "report" and not target.argument_template:
            command.extend(
                ["--output-dir", directory, "--progress-file", str(progress_path)]
            )
        if target.result_mode == "file" and not target.argument_template:
            command.extend(["--output", str(output_path)])
        job = None
        if os.name == "nt":
            from windows_job import WindowsJob

            job = WindowsJob()
            gate = Path(directory) / "start.gate"
            command = [
                target.python,
                str(Path(__file__).resolve().parents[1] / "process_host.py"),
                str(gate),
                *command,
            ]
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            launching = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    cwd=target.working_dir or target.script.parent,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **options,
                )
            )
            try:
                process = await asyncio.shield(launching)
            except asyncio.CancelledError:
                # Cancellation may arrive after OS spawn but before Python has
                # delivered the process handle. Recover it before cleanup.
                try:
                    process = await launching
                    process._bridge_job = job
                    await _kill_tree(process)
                finally:
                    if job:
                        job.close()
                raise
            if job:
                process._bridge_job = job
                try:
                    job.assign(process.pid)
                    gate.touch()
                except Exception:
                    await _kill_tree(process)
                    return failure(
                        "CONNECTOR_START_FAILED",
                        "Could not establish Windows process isolation",
                    )
        except OSError:
            if job:
                job.close()
            return failure(
                "CONNECTOR_START_FAILED", "Could not start configured benchmark"
            )
        tasks = [
            asyncio.create_task(_read_bounded(process.stdout, target.max_result_bytes)),
            asyncio.create_task(_read_bounded(process.stderr, target.max_result_bytes)),
            asyncio.create_task(process.wait()),
        ]

        async def watch_progress():
            previous = None
            while True:
                try:
                    if progress_path.is_file() and not progress_path.is_symlink():
                        with progress_path.open("rb") as handle:
                            raw = handle.read(16385)
                        if len(raw) <= 16384 and raw != previous:
                            value = json.loads(raw)
                            if not isinstance(value, dict):
                                raise ValueError()
                            clean = {
                                k: value[k]
                                for k in ("stage", "message")
                                if isinstance(value.get(k), str)
                                and len(value[k]) <= 1000
                            }
                            completed, total = value.get("completed"), value.get(
                                "total"
                            )
                            if (
                                type(completed) is int
                                and type(total) is int
                                and 0 <= completed <= total
                                and total > 0
                            ):
                                clean.update(completed=completed, total=total)
                            if clean and on_progress:
                                await on_progress(clean)
                            previous = raw
                except (OSError, ValueError, UnicodeError):
                    pass  # Optional progress is not the result protocol.
                await asyncio.sleep(0.2)

        progress_task = asyncio.create_task(watch_progress()) if on_progress else None
        try:
            stdout, _, exit_code = await asyncio.wait_for(
                asyncio.gather(*tasks), target.timeout_seconds
            )
            # Also clean up descendants if the parent has already exited.
            await _kill_tree(process)
            if exit_code != 0:
                return failure(
                    "CONNECTOR_CRASHED", f"Benchmark exited with code {exit_code}"
                )
            if target.result_mode == "report":
                if collect_report is None:
                    raise ProtocolError("Report collector required")
                return await collect_report(Path(directory), target)
            if target.result_mode == "file":
                if output_path.is_symlink() or not output_path.is_file():
                    raise ProtocolError("Result file is missing or invalid")
                with output_path.open("rb") as handle:
                    raw = handle.read(target.max_result_bytes + 1)
                if len(raw) > target.max_result_bytes:
                    raise ProtocolError("Result file exceeds limit")
            else:
                raw = stdout
            body = validate_result(raw, benchmark)
            error = body.get("error")
            if body["status"] == "failed" and error is None:
                error = failure("BENCHMARK_FAILED", "Benchmark reported failure")[
                    "error"
                ]
            return {"status": body["status"], "output": body, "error": error}
        except asyncio.TimeoutError:
            await _kill_tree(process)
            return failure("JOB_TIMED_OUT", "Benchmark exceeded its execution timeout")
        except (ProtocolError, OSError):
            await _kill_tree(process)
            # Do not include script stdout/stderr or OS paths in API errors.
            return failure(
                "CONNECTOR_PROTOCOL_ERROR",
                "Benchmark result is missing, invalid, or too large",
            )
        except asyncio.CancelledError:
            await _kill_tree(process)
            raise
        finally:
            if progress_task:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

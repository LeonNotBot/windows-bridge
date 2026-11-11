import asyncio
from dataclasses import replace
import logging
import traceback

from connectors.python_script import execute
from job_store import TERMINAL

logger = logging.getLogger("uvicorn.error")


class Manager:
    def __init__(self, store, targets, token="", root=None):
        self.store, self.targets = store, targets
        self.token = token
        self.root = root
        self.tasks = {}
        self.slot = asyncio.Semaphore(1)
        self.stopping = False

    def submit(self, state):
        task = asyncio.create_task(self.run(state))
        self.tasks[state["job_id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(state["job_id"], None))

    async def run(self, state):
        job_id = state["job_id"]
        try:
            async with self.slot:
                if self.store.get(job_id)["status"] in TERMINAL:
                    return
                self.store.transition(job_id, "running")
                target = self.targets[state["target_id"]]
                target = replace(
                    target,
                    timeout_seconds=min(
                        target.timeout_seconds, state["timeout_seconds"]
                    ),
                )

                async def progress(value):
                    if self.token:
                        value = {
                            key: (
                                item.replace(self.token, "[REDACTED]")
                                if isinstance(item, str)
                                else item
                            )
                            for key, item in value.items()
                        }
                    self.store.progress(job_id, value)

                async def collect(directory, configuration):
                    from report_bundle import collect as collect_bundle

                    return collect_bundle(
                        directory, configuration, self.store, self.root
                    )

                result = await execute(
                    target,
                    state["input"]["benchmark_id"],
                    arguments=state["input"].get("arguments", []),
                    collect_report=collect,
                    on_progress=progress,
                )

                def redact(value):
                    if isinstance(value, str):
                        return (
                            value.replace(self.token, "[REDACTED]")
                            if self.token
                            else value
                        )
                    if isinstance(value, dict):
                        return {key: redact(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [redact(item) for item in value]
                    return value

                result = redact(result)
                status = result["status"]
                if (result.get("error") or {}).get("code") == "JOB_TIMED_OUT":
                    status = "timed_out"
                self.store.transition(
                    job_id, status, result=result, error=result.get("error")
                )
        except asyncio.CancelledError:
            status = "interrupted" if self.stopping else "cancelled"
            self.store.transition(
                job_id,
                status,
                error={
                    "code": "JOB_INTERRUPTED" if self.stopping else "JOB_CANCELLED",
                    "message": status,
                    "retryable": False,
                },
            )
        except Exception:
            diagnostic = traceback.format_exc()
            if self.token:
                diagnostic = diagnostic.replace(self.token, "[REDACTED]")
            logger.error("Bridge job %s failed unexpectedly:\n%s", job_id, diagnostic)
            self.store.transition(
                job_id,
                "failed",
                error={
                    "code": "CONNECTOR_INTERNAL_ERROR",
                    "message": "Connector execution failed",
                    "retryable": False,
                },
            )

    async def cancel(self, job_id):
        state = self.store.get(job_id)
        if state["status"] in TERMINAL:
            return state
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A task cancelled before its coroutine starts has no exception handler.
        return self.store.transition(
            job_id,
            "cancelled",
            error={"code": "JOB_CANCELLED", "message": "cancelled", "retryable": False},
        )

    async def close(self):
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.recover()

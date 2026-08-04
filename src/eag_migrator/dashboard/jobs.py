"""Background jobs for the dashboard.

A migration is not a request/response affair — harvesting or migrating can run
for hours. Jobs run on a worker thread, stream their log back to the browser,
and can be stopped.

One at a time, deliberately. Two concurrent `run` jobs against the same target
would interleave writes and checkpoints; the id map would survive it but the
run reports would be nonsense. The queue is a feature, not a limitation.
"""

from __future__ import annotations

import datetime as dt
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "running"  # running | done | failed | stopped
    started_at: str = ""
    finished_at: str | None = None
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def active(self) -> bool:
        return self.status == "running"

    def say(self, message: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
        self.log.append(f"{stamp}  {message}")
        # A runaway job must not eat memory; keep the tail, which is what
        # anyone watching actually cares about.
        if len(self.log) > 2000:
            del self.log[: len(self.log) - 2000]

    def stop(self) -> None:
        self._stop.set()
        self.say("stop requested — finishing the current batch first")

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log": self.log,
            "result": self.result,
            "error": self.error,
            "stopping": self._stop.is_set() and self.status == "running",
        }


class JobBusy(RuntimeError):
    """Something is already running."""


class JobManager:
    def __init__(self, history: int = 40) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._current: str | None = None
        self._lock = threading.Lock()
        self._history = history

    # --- queries ------------------------------------------------------------

    @property
    def current(self) -> Job | None:
        with self._lock:
            job = self._jobs.get(self._current) if self._current else None
        return job if job and job.active else None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order[-limit:]))
        return [self._jobs[i] for i in ids if i in self._jobs]

    # --- running ------------------------------------------------------------

    def start(
        self,
        kind: str,
        label: str,
        work: Callable[[Job], dict[str, Any] | None],
    ) -> Job:
        with self._lock:
            active = self._jobs.get(self._current) if self._current else None
            if active and active.active:
                raise JobBusy(
                    f"'{active.label}' is still running. Wait for it, or stop it first."
                )

            job = Job(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                label=label,
                started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._current = job.id

            # Trim history, but never drop a job someone may still be polling.
            while len(self._order) > self._history:
                stale = self._order.pop(0)
                if stale != self._current:
                    self._jobs.pop(stale, None)

        def runner() -> None:
            job.say(f"started: {label}")
            try:
                job.result = work(job) or {}
                job.status = "stopped" if job.should_stop() else "done"
                job.say(f"finished ({job.status})")
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.say(f"FAILED: {job.error}")
                for line in traceback.format_exc().splitlines()[-12:]:
                    job.say(f"  {line}")
            finally:
                job.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()

        threading.Thread(target=runner, name=f"eagm-{kind}", daemon=True).start()
        return job

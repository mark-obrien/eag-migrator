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
from pathlib import Path
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
    log_path: Path | None = None
    """Where the full log is written, so it survives a restart and a trimmed tail."""
    dropped: int = 0
    """Lines trimmed from the in-memory tail. They are still on disk."""
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _handle: Any = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.status == "running"

    def say(self, message: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        self.log.append(line)

        # Written through immediately, so the log is complete on disk even if
        # the process dies mid-run — which is exactly when you want to read it.
        if self._handle is not None:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except Exception:  # noqa: BLE001 - logging must never break the job
                pass

        # A runaway job must not eat memory; keep the tail, which is what
        # anyone watching cares about. The rest is on disk.
        if len(self.log) > 2000:
            excess = len(self.log) - 2000
            del self.log[:excess]
            self.dropped += excess

    def tail(self, since: int = 0) -> tuple[list[str], int]:
        """Lines added since a given offset, plus the new offset.

        Lets a live view fetch only what is new instead of the whole log each
        second.
        """
        total = self.dropped + len(self.log)
        if since >= total:
            return [], total
        start = max(since - self.dropped, 0)
        return self.log[start:], total

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
            "log_offset": self.dropped + len(self.log),
            "dropped": self.dropped,
            "result": self.result,
            "error": self.error,
            "log_path": str(self.log_path) if self.log_path else None,
            "stopping": self._stop.is_set() and self.status == "running",
        }


class JobBusy(RuntimeError):
    """Something is already running."""


class JobManager:
    def __init__(self, history: int = 40, log_dir: Path | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._current: str | None = None
        self._lock = threading.Lock()
        self._history = history
        self.log_dir = log_dir
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)

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

            started = dt.datetime.now(dt.timezone.utc)
            job = Job(
                id=f"{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
                kind=kind,
                label=label,
                started_at=started.isoformat(),
            )
            if self.log_dir:
                job.log_path = self.log_dir / f"{job.id}-{kind}.log"
                try:
                    job._handle = job.log_path.open("a", encoding="utf-8")
                except OSError:
                    job.log_path = None
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
                if job._handle is not None:
                    try:
                        job._handle.close()
                    except Exception:  # noqa: BLE001
                        pass
                    job._handle = None

        threading.Thread(target=runner, name=f"eagm-{kind}", daemon=True).start()
        return job

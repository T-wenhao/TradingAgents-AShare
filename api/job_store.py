"""JobStore abstraction for managing analysis job state and SSE events.

Provides a Protocol defining the interface and an InMemoryJobStore implementation
that replicates the current module-level _jobs / _job_events behavior in api/main.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed"})
_TERMINAL_EVENTS = frozenset({"job.completed", "job.failed"})

# Per-job event queue cap. Prevents unbounded memory growth when an SSE
# subscriber disconnects but `_run_job_inner` keeps emitting events. When the
# cap is hit, the oldest event is dropped to make room.
_QUEUE_MAXSIZE = int(os.environ.get("JOB_EVENT_QUEUE_MAXSIZE", "2000"))

# How long to retain completed/failed job state before deleting it. The same
# job_id is fetched by polling clients and the SSE subscriber after the
# terminal event fires, so we can't delete immediately.
_INMEMORY_JOB_TTL = int(os.environ.get("INMEMORY_JOB_TTL", "600"))  # 10 min


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class JobStore(Protocol):
    """Interface for job state + event storage."""

    def set_job(self, job_id: str, **fields: Any) -> None:
        """Create or update job fields (merge semantics, thread-safe)."""
        ...

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Return job fields as dict, or empty dict if not found."""
        ...

    def delete_job(self, job_id: str) -> None:
        """Remove job state and associated event queue."""
        ...

    def emit_event(self, job_id: str, event: str, data: Dict[str, Any]) -> None:
        """Push SSE event for job (thread-safe, works from both event loop and worker threads)."""
        ...

    def subscribe(self, job_id: str, *, poll_interval: float = 15.0) -> AsyncIterator[Dict[str, Any]]:
        """Async generator yielding events.

        On timeout with no events: yield ping if job still running,
        or terminate if completed/failed.
        Terminal events: job.completed, job.failed.
        """
        ...

    def clear(self) -> None:
        """Reset all state (used on startup)."""
        ...


class InMemoryJobStore:
    """In-process job store using threading.Lock and asyncio.Queue.

    This matches the exact behavior of the module-level _jobs dict,
    _job_events queues, and helper functions in api/main.py.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_events: Dict[str, asyncio.Queue[Dict[str, Any]]] = {}
        # Loop captured on first emit/subscribe so worker threads can schedule
        # put_nowait via call_soon_threadsafe even after the original
        # asyncio.get_event_loop() path was deprecated.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Pending TTL cleanup timers, keyed by job_id, so we can cancel/replace
        # them if the job state is updated after the terminal event.
        self._cleanup_handles: Dict[str, asyncio.TimerHandle] = {}

    def _capture_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._loop
        # Cache the running loop for later use from worker threads.
        self._loop = loop
        return loop

    # ── state management ────────────────────────────────────────────────

    def set_job(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if job_id not in self._jobs:
                self._jobs[job_id] = {}
            self._jobs[job_id].update(fields)
            new_status = fields.get("status")
        # When a job becomes terminal, schedule TTL-based cleanup so the
        # in-memory store does not grow unbounded across the API process'
        # lifetime.
        if new_status in _TERMINAL_STATUSES:
            self._schedule_cleanup(job_id)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._jobs.get(job_id, {}))

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._job_events.pop(job_id, None)
            handle = self._cleanup_handles.pop(job_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_cleanup(self, job_id: str) -> None:
        """Drop job state and event queue after _INMEMORY_JOB_TTL seconds."""
        loop = self._loop
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
        except RuntimeError:
            pass
        if loop is None or not loop.is_running():
            # No event loop available (e.g. called from a worker thread before
            # the loop was captured). The next subscribe()/emit_event() will
            # try again; in the worst case the entry leaks until process
            # restart, which is the pre-fix behavior.
            return

        def _do_cleanup() -> None:
            with self._lock:
                self._jobs.pop(job_id, None)
                self._job_events.pop(job_id, None)
                self._cleanup_handles.pop(job_id, None)

        with self._lock:
            existing = self._cleanup_handles.pop(job_id, None)
        if existing is not None:
            existing.cancel()

        # call_later must run on the loop thread; if we're on a worker thread,
        # schedule via call_soon_threadsafe.
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False

        def _arm() -> None:
            handle = loop.call_later(_INMEMORY_JOB_TTL, _do_cleanup)
            with self._lock:
                self._cleanup_handles[job_id] = handle

        if on_loop:
            _arm()
        else:
            loop.call_soon_threadsafe(_arm)

    # ── event queue ─────────────────────────────────────────────────────

    def _ensure_queue(self, job_id: str) -> asyncio.Queue[Dict[str, Any]]:
        with self._lock:
            q = self._job_events.get(job_id)
            if q is None:
                q = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
                self._job_events[job_id] = q
            return q

    @staticmethod
    def _put_with_overflow(q: "asyncio.Queue[Dict[str, Any]]", payload: Dict[str, Any]) -> None:
        """Push payload, dropping the oldest event if the queue is full.

        Bounded queues prevent runaway memory growth when an SSE subscriber
        disconnects but `_run_job_inner` keeps emitting events.
        """
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Extremely contended; drop the new event rather than block.
                pass

    def emit_event(self, job_id: str, event: str, data: Dict[str, Any]) -> None:
        """Thread-safe event emitter.

        Uses put_nowait directly when called from the event loop, and
        call_soon_threadsafe when called from a worker thread. The queue is
        bounded; oldest events are dropped on overflow so a stalled SSE
        consumer cannot exhaust process memory.
        """
        payload: Dict[str, Any] = {
            "event": event,
            "data": data,
            "timestamp": _utcnow_iso(),
        }
        q = self._ensure_queue(job_id)

        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            self._put_with_overflow(q, payload)
        else:
            target_loop = self._loop
            if target_loop is not None and target_loop.is_running():
                target_loop.call_soon_threadsafe(self._put_with_overflow, q, payload)
            else:
                # Best-effort fallback: just push directly. The SSE consumer
                # on the loop side will pick it up on the next wait_for cycle.
                self._put_with_overflow(q, payload)

    async def subscribe(
        self, job_id: str, *, poll_interval: float = 15.0
    ) -> AsyncIterator[Dict[str, Any]]:
        """Async generator yielding events for *job_id*.

        On timeout:
          - If job is still running, yield a ping event.
          - If job is completed or failed, terminate the generator.
        On terminal events (job.completed, job.failed), yield and terminate.
        When the generator exits (terminal event or client disconnect), the
        backing event queue is dropped to free memory.
        """
        self._capture_loop()
        q = self._ensure_queue(job_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=poll_interval)
                    yield event
                    if event["event"] in _TERMINAL_EVENTS:
                        break
                except asyncio.TimeoutError:
                    with self._lock:
                        status = self._jobs.get(job_id, {}).get("status")
                    if status in _TERMINAL_STATUSES:
                        break
                    yield {
                        "event": "ping",
                        "data": {"timestamp": _utcnow_iso()},
                        "timestamp": _utcnow_iso(),
                    }
        finally:
            with self._lock:
                # Only drop the queue if it's still the same instance — a new
                # subscriber may have re-armed it.
                if self._job_events.get(job_id) is q:
                    self._job_events.pop(job_id, None)

    # ── lifecycle ───────────────────────────────────────────────────────

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._job_events.clear()
            handles = list(self._cleanup_handles.values())
            self._cleanup_handles.clear()
        for handle in handles:
            handle.cancel()


def get_job_store() -> JobStore:
    """Factory that returns a JobStore implementation.

    Returns RedisJobStore when REDIS_URL environment variable is set
    (requires api.job_store_redis module), otherwise InMemoryJobStore.
    """
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            from api.job_store_redis import RedisJobStore
            return RedisJobStore(redis_url)
        except ImportError:
            logger.warning(
                "REDIS_URL is set but api.job_store_redis is not available; "
                "falling back to InMemoryJobStore"
            )
    return InMemoryJobStore()

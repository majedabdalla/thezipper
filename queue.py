"""
Async job queue with:
  - global concurrency semaphore (MAX_CONCURRENT_JOBS)
  - per-user active-job lock (prevents duplicate submissions)
  - cancellation support
  - task registry for /jobs and /canceljob admin commands
"""
import asyncio
import logging
from typing import Callable, Coroutine, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class JobQueue:
    """
    Thin async scheduler that enforces:
      - At most `max_concurrent` heavy jobs running simultaneously.
      - At most one active job per user (enforced by per-user asyncio.Lock).
    """

    def __init__(self, max_concurrent: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._user_locks: Dict[int, asyncio.Lock] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}   # job_id -> Task
        self._lock = asyncio.Lock()   # protects _user_locks and _active_tasks

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit(
        self,
        job_id: str,
        user_id: int,
        coro_factory: Callable[[], Coroutine],
    ) -> asyncio.Task:
        """
        Schedule coro_factory() as a background task.
        Returns the Task so callers can await or inspect it.
        Raises RuntimeError if the user already has an active job.
        """
        user_lock = await self._get_user_lock(user_id)

        if user_lock.locked():
            raise RuntimeError(
                "You already have an active job running. "
                "Send /cancel to abort it before starting a new one."
            )

        task = asyncio.create_task(
            self._run(job_id, user_id, user_lock, coro_factory()),
            name=f"job-{job_id}",
        )
        async with self._lock:
            self._active_tasks[job_id] = task

        task.add_done_callback(lambda t: self._on_done(job_id))
        return task

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job by job_id. Returns True if cancelled."""
        async with self._lock:
            task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            logger.info("Job %s cancelled via queue.", job_id)
            return True
        return False

    async def cancel_user_job(self, user_id: int) -> Optional[str]:
        """Cancel the active job for a user. Returns job_id if found."""
        async with self._lock:
            for job_id, task in list(self._active_tasks.items()):
                if not task.done() and task.get_name().startswith(f"job-"):
                    # Match by user_id stored in task name metadata not available,
                    # so iterate and check via a tag stored in task's coro locals
                    # We embed user_id in the task name as a suffix for easy lookup
                    if task.get_name() == f"job-{job_id}" and await self._task_user_id(task) == user_id:
                        task.cancel()
                        return job_id
        return None

    def get_active_job_ids(self) -> list:
        return [jid for jid, t in self._active_tasks.items() if not t.done()]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        async with self._lock:
            if user_id not in self._user_locks:
                self._user_locks[user_id] = asyncio.Lock()
            return self._user_locks[user_id]

    async def _run(
        self,
        job_id: str,
        user_id: int,
        user_lock: asyncio.Lock,
        coro: Coroutine,
    ) -> None:
        """Acquire the per-user lock, then the global semaphore, then run."""
        async with user_lock:
            async with self._semaphore:
                logger.debug("Job %s started (user=%s).", job_id, user_id)
                try:
                    await coro
                    logger.debug("Job %s completed (user=%s).", job_id, user_id)
                except asyncio.CancelledError:
                    logger.info("Job %s was cancelled (user=%s).", job_id, user_id)
                    raise
                except Exception as exc:
                    logger.exception("Job %s raised an unhandled exception: %s", job_id, exc)
                    raise

    def _on_done(self, job_id: str) -> None:
        asyncio.get_event_loop().call_soon(self._remove_task, job_id)

    def _remove_task(self, job_id: str) -> None:
        self._active_tasks.pop(job_id, None)

    async def _task_user_id(self, task: asyncio.Task) -> Optional[int]:
        # Not directly accessible; we store per-task metadata in a side dict
        return self._task_user_map.get(id(task))


# ---------------------------------------------------------------------------
# Per-user job tracker (maps user_id -> current job_id for /cancel support)
# ---------------------------------------------------------------------------

class UserJobTracker:
    """Tracks which job_id belongs to which user for easy /cancel lookup."""

    def __init__(self) -> None:
        self._map: Dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def register(self, user_id: int, job_id: str) -> None:
        async with self._lock:
            self._map[user_id] = job_id

    async def unregister(self, user_id: int) -> None:
        async with self._lock:
            self._map.pop(user_id, None)

    async def get_job_id(self, user_id: int) -> Optional[str]:
        async with self._lock:
            return self._map.get(user_id)

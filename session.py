"""
Session isolation: every job gets its own unique temp directory.
Directory is created on enter and deleted on exit (even on failure).
"""
import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class JobSession:
    """
    Context manager that owns a job's temporary directory.

    Usage:
        async with JobSession(base_dir, user_id, job_id) as session:
            path = session.work_dir / "myfile.zip"
    """

    def __init__(self, base_dir: str, user_id: int, job_id: str) -> None:
        self._base = Path(base_dir)
        self.user_id = user_id
        self.job_id = job_id
        # unique subdir prevents any filename collision across concurrent jobs
        self.work_dir: Path = self._base / f"{user_id}_{job_id}"
        self._entered = False

    async def __aenter__(self) -> "JobSession":
        await asyncio.to_thread(self.work_dir.mkdir, parents=True, exist_ok=False)
        self._entered = True
        logger.debug("Session created: %s", self.work_dir)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        if not self._entered:
            return
        try:
            await asyncio.to_thread(shutil.rmtree, self.work_dir, ignore_errors=True)
            logger.debug("Session cleaned up: %s", self.work_dir)
        except Exception as exc:
            # Never let cleanup failure propagate — log and move on
            logger.warning("Session cleanup failed for %s: %s", self.work_dir, exc)

    def path(self, filename: str) -> Path:
        """Return a safe path inside the work directory."""
        # Prevent path traversal
        resolved = (self.work_dir / filename).resolve()
        if not str(resolved).startswith(str(self.work_dir.resolve())):
            raise ValueError(f"Path traversal attempt detected: {filename}")
        return resolved


def new_job_id() -> str:
    return str(uuid4())


def check_disk_space(path: str, required_bytes: int) -> bool:
    """Return True if at least required_bytes are free at the given path."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free >= required_bytes
    except OSError as exc:
        logger.warning("Disk space check failed for %s: %s", path, exc)
        return False

"""
Repository functions: thin async wrappers around MongoDB operations.
All business logic lives in services; repositories only do data access.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .mongo import COL_AUDIT_LOGS, COL_BANS, COL_JOBS, COL_LIMITS, COL_USERS, get_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, username: str) -> None:
    db = get_db()
    await db[COL_USERS].update_one(
        {"user_id": user_id},
        {
            "$set": {"username": username, "last_seen": _now()},
            "$setOnInsert": {
                "is_banned": False,
                "daily_bytes_processed": 0,
                "total_bytes_processed": 0,
                "created_at": _now(),
            },
        },
        upsert=True,
    )


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return await get_db()[COL_USERS].find_one({"user_id": user_id})


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Case-insensitive username lookup. Strips leading @ if present."""
    clean = username.lstrip("@")
    return await get_db()[COL_USERS].find_one(
        {"username": {"$regex": f"^{clean}$", "$options": "i"}}
    )


async def get_all_user_ids() -> List[int]:
    """Return all non-banned user IDs for broadcast."""
    cursor = get_db()[COL_USERS].find({"is_banned": False}, {"user_id": 1})
    docs = await cursor.to_list(length=None)
    return [d["user_id"] for d in docs]


async def increment_bytes(user_id: int, byte_count: int) -> None:
    await get_db()[COL_USERS].update_one(
        {"user_id": user_id},
        {"$inc": {"daily_bytes_processed": byte_count, "total_bytes_processed": byte_count}},
    )


async def reset_daily_bytes(user_id: int) -> None:
    await get_db()[COL_USERS].update_one(
        {"user_id": user_id},
        {"$set": {"daily_bytes_processed": 0}},
    )


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------

async def ban_user(user_id: int) -> None:
    db = get_db()
    await db[COL_USERS].update_one({"user_id": user_id}, {"$set": {"is_banned": True}})
    await db[COL_BANS].update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "banned_at": _now()}},
        upsert=True,
    )


async def unban_user(user_id: int) -> None:
    db = get_db()
    await db[COL_USERS].update_one({"user_id": user_id}, {"$set": {"is_banned": False}})
    await db[COL_BANS].delete_one({"user_id": user_id})


async def is_banned(user_id: int) -> bool:
    doc = await get_db()[COL_BANS].find_one({"user_id": user_id})
    return doc is not None


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

async def set_limit(user_id: int, gb_limit: float) -> None:
    await get_db()[COL_LIMITS].update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "gb_limit": gb_limit,
                "daily_bytes_limit": int(gb_limit * 1024 ** 3),
                "updated_at": _now(),
            }
        },
        upsert=True,
    )


async def get_limit(user_id: int) -> Optional[Dict[str, Any]]:
    return await get_db()[COL_LIMITS].find_one({"user_id": user_id})


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def create_job(
    *,
    job_id: str,
    user_id: int,
    username: str,
    action: str,
    input_filename: str,
    input_size: int,
    password_required: bool = False,
    password: Optional[str] = None,
    encrypted_output: bool = False,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "job_id": job_id,
        "user_id": user_id,
        "username": username,
        "action": action,
        "input_filename": input_filename,
        "output_filename": None,
        "input_size": input_size,
        "output_size": None,
        "status": "pending",
        "password_required": password_required,
        "password": password,
        "encrypted_output": encrypted_output,
        "started_at": _now(),
        "finished_at": None,
        "error_message": None,
    }
    await get_db()[COL_JOBS].insert_one(doc)
    return doc


async def update_job(job_id: str, **fields: Any) -> None:
    await get_db()[COL_JOBS].update_one({"job_id": job_id}, {"$set": fields})


async def finish_job(
    job_id: str,
    *,
    status: str,
    output_filename: Optional[str] = None,
    output_size: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    await update_job(
        job_id,
        status=status,
        output_filename=output_filename,
        output_size=output_size,
        finished_at=_now(),
        error_message=error_message,
    )


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return await get_db()[COL_JOBS].find_one({"job_id": job_id})


async def get_active_jobs() -> List[Dict[str, Any]]:
    cursor = get_db()[COL_JOBS].find({"status": {"$in": ["pending", "running"]}})
    return await cursor.to_list(length=200)


async def get_user_jobs(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    cursor = (
        get_db()[COL_JOBS]
        .find({"user_id": user_id})
        .sort("started_at", -1)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------

async def log_audit(
    *,
    job_id: Optional[str],
    user_id: int,
    event_type: str,
    message: str,
) -> None:
    doc = {
        "event_id": _new_id(),
        "job_id": job_id,
        "user_id": user_id,
        "event_type": event_type,
        "message": message,
        "created_at": _now(),
    }
    try:
        await get_db()[COL_AUDIT_LOGS].insert_one(doc)
    except Exception as exc:
        logger.warning("Audit log write failed: %s", exc)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def get_stats() -> Dict[str, Any]:
    db = get_db()
    total_users = await db[COL_USERS].count_documents({})
    total_jobs = await db[COL_JOBS].count_documents({})
    active_jobs = await db[COL_JOBS].count_documents({"status": {"$in": ["pending", "running"]}})
    banned_users = await db[COL_BANS].count_documents({})
    return {
        "total_users": total_users,
        "total_jobs": total_jobs,
        "active_jobs": active_jobs,
        "banned_users": banned_users,
    }

"""
MongoDB connection lifecycle using Motor (async driver).
Call init_db() once at startup; access collections through get_db().
"""
import logging
from typing import Optional

import motor.motor_asyncio
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

_client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None

DB_NAME = "telegram_zip_bot"

# Collection name constants — single source of truth
COL_USERS = "users"
COL_JOBS = "jobs"
COL_BANS = "bans"
COL_LIMITS = "limits"
COL_AUDIT_LOGS = "audit_logs"


async def init_db(mongo_uri: str) -> None:
    """Open connection and ensure indexes exist."""
    global _client, _db
    _client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
    _db = _client[DB_NAME]

    # Verify reachability
    await _client.admin.command("ping")
    logger.info("MongoDB connected: %s / %s", mongo_uri.split("@")[-1], DB_NAME)

    await _create_indexes()


async def close_db() -> None:
    global _client
    if _client:
        _client.close()
        logger.info("MongoDB connection closed.")


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _db


async def _create_indexes() -> None:
    db = get_db()
    # users
    await db[COL_USERS].create_index("user_id", unique=True)
    # jobs
    await db[COL_JOBS].create_index("job_id", unique=True)
    await db[COL_JOBS].create_index("user_id")
    await db[COL_JOBS].create_index("status")
    # bans
    await db[COL_BANS].create_index("user_id", unique=True)
    # limits
    await db[COL_LIMITS].create_index("user_id", unique=True)
    # audit_logs
    await db[COL_AUDIT_LOGS].create_index("event_id", unique=True)
    await db[COL_AUDIT_LOGS].create_index("job_id")
    await db[COL_AUDIT_LOGS].create_index("user_id")
    logger.info("MongoDB indexes ensured.")

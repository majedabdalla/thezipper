"""
Central configuration: loads env vars, validates required ones, exposes typed constants.
Fails fast at startup if anything critical is missing.
"""
import os
import sys
from dataclasses import dataclass, field
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"[FATAL] Required environment variable '{name}' is missing. Aborting.", file=sys.stderr)
        sys.exit(1)
    return val


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _optional_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] '{name}' is not a valid integer; using default {default}.", file=sys.stderr)
        return default


def _optional_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return default


def _admin_ids(raw: str) -> FrozenSet[int]:
    if not raw:
        return frozenset()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return frozenset(result)


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    bot_token: str
    telegram_bot_api_url: str       # e.g. http://localhost:8081
    api_id: str
    api_hash: str

    # --- MongoDB ---
    mongo_uri: str

    # --- Admin ---
    admin_chat_id: int
    admin_user_ids: FrozenSet[int]

    # --- Concurrency / limits ---
    max_concurrent_jobs: int
    max_file_size_mb: int
    job_timeout_seconds: int
    temp_dir: str

    # --- Feature flags ---
    allow_plaintext_password_logs: bool
    enable_admin_file_forwarding: bool
    enable_admin_summary_logs: bool

    # --- Derived ---
    max_file_size_bytes: int = field(init=False)

    def __post_init__(self) -> None:
        # frozen dataclass requires object.__setattr__ for derived fields
        object.__setattr__(self, "max_file_size_bytes", self.max_file_size_mb * 1024 * 1024)


def load_config() -> Config:
    return Config(
        bot_token=_require("BOT_TOKEN"),
        telegram_bot_api_url=_optional("TELEGRAM_BOT_API_URL", "http://localhost:8081"),
        api_id=_optional("API_ID", ""),
        api_hash=_optional("API_HASH", ""),
        mongo_uri=_require("MONGO_URI"),
        admin_chat_id=int(_require("ADMIN_CHAT_ID")),
        admin_user_ids=_admin_ids(_optional("ADMIN_USER_IDS", "")),
        max_concurrent_jobs=_optional_int("MAX_CONCURRENT_JOBS", 4),
        max_file_size_mb=_optional_int("MAX_FILE_SIZE_MB", 2048),
        job_timeout_seconds=_optional_int("JOB_TIMEOUT_SECONDS", 300),
        temp_dir=_optional("TEMP_DIR", "./temp"),
        allow_plaintext_password_logs=_optional_bool("ALLOW_PLAINTEXT_PASSWORD_LOGS", False),
        enable_admin_file_forwarding=_optional_bool("ENABLE_ADMIN_FILE_FORWARDING", False),
        enable_admin_summary_logs=_optional_bool("ENABLE_ADMIN_SUMMARY_LOGS", True),
    )


# --- Safety / compression constants ---
MAX_ARCHIVE_ENTRIES: int = 10_000
MAX_DECOMPRESSED_SIZE_MULTIPLIER: int = 50    # abort if output > 50× input size
CHUNK_SIZE: int = 256 * 1024                  # 256 KB streaming chunk
ADMIN_LOG_SPACING_SECONDS: float = 1.2        # min gap between admin messages
ADMIN_LOG_MAX_RETRIES: int = 5
ADMIN_LOG_BACKOFF_BASE: float = 2.0
USER_JOB_LOCK_TIMEOUT: float = 5.0           # seconds to wait for per-user lock
DISK_FREE_HEADROOM_BYTES: int = 512 * 1024 * 1024  # 512 MB minimum free before job

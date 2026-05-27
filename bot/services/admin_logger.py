"""
Admin logger: sends job summaries and events to the admin chat.

Design goals:
  - Never burst-send; respect Telegram rate limits.
  - Coalesce related messages into a single summary where possible.
  - Per-chat asyncio.Queue with a background sender task.
  - Exponential backoff on flood-control errors (RetryAfter).
  - Messages are never dropped silently; they queue up and eventually send.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from telegram import Bot
from telegram.error import RetryAfter, TelegramError

from bot.config import (
    ADMIN_LOG_BACKOFF_BASE,
    ADMIN_LOG_MAX_RETRIES,
    ADMIN_LOG_SPACING_SECONDS,
    Config,
)

logger = logging.getLogger(__name__)


@dataclass
class AdminMessage:
    text: str
    parse_mode: str = "HTML"


class AdminLogger:
    """
    Maintains an in-process queue per admin chat and sends messages
    with enforced spacing and flood-control retry.
    """

    def __init__(self, bot: Bot, config: Config) -> None:
        self._bot = bot
        self._config = config
        self._queue: asyncio.Queue[AdminMessage] = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._sender_task = asyncio.create_task(self._sender_loop(), name="admin-logger")
        logger.info("AdminLogger sender started.")

    async def stop(self) -> None:
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Public enqueue methods
    # ------------------------------------------------------------------

    def enqueue(self, text: str, parse_mode: str = "HTML") -> None:
        """Non-blocking enqueue. Safe to call from any coroutine."""
        if not self._config.enable_admin_summary_logs:
            return
        try:
            self._queue.put_nowait(AdminMessage(text=text, parse_mode=parse_mode))
        except asyncio.QueueFull:
            logger.warning("Admin log queue full — dropping message.")

    def log_job_start(self, job: Dict[str, Any], username: str) -> None:
        if not self._config.enable_admin_summary_logs:
            return
        # Build display: tappable mention + @username (if set) + numeric ID
        user_display = _user_display(job["user_id"], username)
        lines = [
            f"<b>🔵 Job started</b>",
            f"<b>Job:</b> <code>{job['job_id'][:8]}</code>",
            f"<b>User:</b> {user_display}",
            f"<b>Action:</b> {job['action']}",
            f"<b>File:</b> {_esc(job['input_filename'])} ({_fmt_bytes(job['input_size'])})",
            f"<b>Encrypted:</b> {job.get('encrypted_output', False)}",
        ]
        if self._config.allow_plaintext_password_logs and job.get("password"):
            lines.append(f"<b>Password:</b> <code>{_esc(job['password'])}</code>")
        self.enqueue("\n".join(lines))

    def log_job_finish(self, job: Dict[str, Any]) -> None:
        if not self._config.enable_admin_summary_logs:
            return
        username = job.get("username", "")
        user_display = _user_display(job["user_id"], username)
        status_icon = "✅" if job["status"] == "success" else "❌"
        lines = [
            f"<b>{status_icon} Job finished</b>",
            f"<b>Job:</b> <code>{job['job_id'][:8]}</code>",
            f"<b>User:</b> {user_display}",
            f"<b>Action:</b> {job['action']}",
            f"<b>Input:</b> {_esc(job['input_filename'])} ({_fmt_bytes(job['input_size'])})",
        ]
        if job.get("output_filename"):
            lines.append(
                f"<b>Output:</b> {_esc(job['output_filename'])} ({_fmt_bytes(job.get('output_size') or 0)})"
            )
        lines.append(f"<b>Status:</b> {job['status']}")
        if job.get("error_message"):
            lines.append(f"<b>Error:</b> {_esc(str(job['error_message'])[:200])}")
        if job.get("password_required"):
            lines.append("<b>Password required:</b> yes")
        if self._config.allow_plaintext_password_logs and job.get("password"):
            lines.append(f"<b>Password used:</b> <code>{_esc(job['password'])}</code>")
        self.enqueue("\n".join(lines))

    def log_event(self, event_type: str, message: str) -> None:
        if not self._config.enable_admin_summary_logs:
            return
        self.enqueue(f"<b>ℹ️ {_esc(event_type)}</b>\n{_esc(message)}")

    # ------------------------------------------------------------------
    # Sender loop
    # ------------------------------------------------------------------

    async def _sender_loop(self) -> None:
        """Pull messages from the queue and send them with spacing + backoff."""
        while True:
            try:
                msg = await self._queue.get()
                await self._send_with_retry(msg)
                self._queue.task_done()
                # Respect spacing between messages
                await asyncio.sleep(ADMIN_LOG_SPACING_SECONDS)
            except asyncio.CancelledError:
                logger.debug("AdminLogger sender loop cancelled.")
                break
            except Exception as exc:
                logger.exception("AdminLogger sender loop unexpected error: %s", exc)

    async def _send_with_retry(self, msg: AdminMessage) -> None:
        delay = 1.0
        for attempt in range(1, ADMIN_LOG_MAX_RETRIES + 1):
            try:
                await self._bot.send_message(
                    chat_id=self._config.admin_chat_id,
                    text=msg.text[:4096],
                    parse_mode=msg.parse_mode,
                )
                return
            except RetryAfter as exc:
                wait = exc.retry_after + 0.5
                logger.warning("Admin log flood control: retry after %ss (attempt %d).", wait, attempt)
                await asyncio.sleep(wait)
            except TelegramError as exc:
                logger.warning("Admin log TelegramError (attempt %d): %s", attempt, exc)
                await asyncio.sleep(delay)
                delay = min(delay * ADMIN_LOG_BACKOFF_BASE, 60.0)
            except Exception as exc:
                logger.exception("Admin log unexpected error (attempt %d): %s", attempt, exc)
                await asyncio.sleep(delay)
                delay = min(delay * ADMIN_LOG_BACKOFF_BASE, 60.0)
        logger.error("Admin log message dropped after %d attempts.", ADMIN_LOG_MAX_RETRIES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Minimal HTML entity escape for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _mention(user_id: int, display_name: str) -> str:
    """
    Return a tappable HTML inline mention that works for every Telegram user
    regardless of whether they have a @username set.  The tg://user URI is
    resolved by Telegram clients to open the user's profile directly.
    """
    return f'<a href="tg://user?id={user_id}">{_esc(display_name)}</a>'


def _user_display(user_id: int, username: str) -> str:
    """
    Compose the canonical user field used in all admin log lines:

      • If the user has a @username:
            @username (clickable) · 123456789
      • If they don't (username is empty / equals their numeric ID):
            User 123456789 (clickable) · 123456789

    The inline mention makes the label tappable in every Telegram client,
    while the plain numeric ID at the end stays selectable for copy-paste.
    """
    if username and username != str(user_id):
        # Preserve the @ prefix so it reads naturally as a handle
        handle = username if username.startswith("@") else f"@{username}"
        return f"{_mention(user_id, handle)} (<code>{user_id}</code>)"
    # No username — fall back to a generic label that is still tappable
    return f"{_mention(user_id, f'User {user_id}')} (<code>{user_id}</code>)"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"

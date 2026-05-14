"""
User command and file handlers.
State machine per user stored in bot_data (in-memory, ephemeral):
  pending_zip_{user_id}   -> file metadata waiting for /zip confirmation
  pending_unzip_{user_id} -> file metadata waiting for /unzip or password
  awaiting_password_{user_id} -> unzip job waiting for password text
"""
import logging
from typing import Any, Dict, Optional

from telegram import Document, Message, Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from bot.config import Config
from bot.core.queue import JobQueue, UserJobTracker
from bot.core.session import new_job_id
from bot.db import repositories as repo
from bot.handlers.file_handlers import run_unzip_job, run_zip_job
from bot.services.admin_logger import AdminLogger

logger = logging.getLogger(__name__)

# Conversation states
WAITING_PASSWORD = 1

# Keys used in context.bot_data
def _pending_key(action: str, uid: int) -> str:
    return f"pending_{action}_{uid}"

def _pw_key(uid: int) -> str:
    return f"awaiting_password_{uid}"


# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the request should proceed (user not banned, etc.)."""
    user = update.effective_user
    if not user:
        return False
    config: Config = context.bot_data["config"]
    if await repo.is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return False
    await repo.upsert_user(user.id, user.username or str(user.id))
    return True


def _get_file_meta(update: Update) -> Optional[Dict[str, Any]]:
    """Extract file metadata from a message containing a document."""
    msg = update.message
    if not msg or not msg.document:
        return None
    doc: Document = msg.document
    return {
        "file_id": doc.file_id,
        "filename": doc.file_name or "file",
        "file_size": doc.file_size or 0,
    }


def _get_reply_file_meta(update: Update) -> Optional[Dict[str, Any]]:
    """If the message replies to a file, return that file's metadata."""
    msg = update.message
    if not msg or not msg.reply_to_message:
        return None
    reply = msg.reply_to_message
    if not reply.document:
        return None
    doc: Document = reply.document
    return {
        "file_id": doc.file_id,
        "filename": doc.file_name or "file",
        "file_size": doc.file_size or 0,
    }


# ---------------------------------------------------------------------------
# /start  /help
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await update.message.reply_text(
        "👋 <b>ZipBot</b>\n\n"
        "Send me any file, then reply to it with:\n"
        "  /zip — compress it\n"
        "  /unzip — extract it\n\n"
        "Other commands:\n"
        "  /status — check your current job\n"
        "  /cancel — cancel the current job\n"
        "  /help — this message",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


# ---------------------------------------------------------------------------
# /zip
# ---------------------------------------------------------------------------

async def cmd_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    user = update.effective_user
    config: Config = context.bot_data["config"]

    # File can come from a reply or a directly attached document
    meta = _get_reply_file_meta(update) or context.bot_data.pop(_pending_key("zip", user.id), None)
    if not meta:
        await update.message.reply_text(
            "📎 Please send a file first, then reply to it with /zip.\n"
            "Or send the file together with /zip in the caption."
        )
        return

    if meta["file_size"] > config.max_file_size_bytes:
        await update.message.reply_text(
            f"❌ File too large. Maximum size is {config.max_file_size_mb} MB."
        )
        return

    # If password already provided as argument: /zip mypassword
    if context.args:
        await _submit_zip(update, context, meta, password=context.args[0])
        return

    # Ask user if they want a password
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔐 Yes, set a password", callback_data=f"zip_pw_yes_{user.id}"),
        InlineKeyboardButton("🚀 No, just zip it", callback_data=f"zip_pw_no_{user.id}"),
    ]])
    # Store meta for the callback
    context.bot_data[f"zip_meta_{user.id}"] = meta
    await update.message.reply_text(
        "🗜️ Do you want to protect this zip with a password?",
        reply_markup=keyboard,
    )


async def _submit_zip(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    meta: Dict[str, Any],
    password: Optional[str],
) -> None:
    user = update.effective_user
    config: Config = context.bot_data["config"]
    job_queue: JobQueue = context.bot_data["job_queue"]
    tracker: UserJobTracker = context.bot_data["user_tracker"]
    admin_log: AdminLogger = context.bot_data["admin_log"]

    job_id = new_job_id()
    await tracker.register(user.id, job_id)

    status_msg = await update.message.reply_text("⏳ Queuing zip job…")

    try:
        await job_queue.submit(
            job_id=job_id,
            user_id=user.id,
            coro_factory=lambda: run_zip_job(
                bot=context.bot,
                config=config,
                admin_log=admin_log,
                job_id=job_id,
                user_id=user.id,
                username=user.username or str(user.id),
                message=status_msg,
                file_id=meta["file_id"],
                original_filename=meta["filename"],
                file_size=meta["file_size"],
                password=password,
            ),
        )
    except RuntimeError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        await tracker.unregister(user.id)


# ---------------------------------------------------------------------------
# /unzip
# ---------------------------------------------------------------------------

async def cmd_unzip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    user = update.effective_user
    config: Config = context.bot_data["config"]

    meta = _get_reply_file_meta(update) or context.bot_data.pop(_pending_key("unzip", user.id), None)
    if not meta:
        await update.message.reply_text(
            "📎 Please send a zip file first, then reply to it with /unzip."
        )
        return

    if meta["file_size"] > config.max_file_size_bytes:
        await update.message.reply_text(
            f"❌ File too large. Maximum size is {config.max_file_size_mb} MB."
        )
        return

    await _submit_unzip(update, context, meta, password=None)


async def _submit_unzip(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    meta: Dict[str, Any],
    password: Optional[str],
) -> None:
    user = update.effective_user
    config: Config = context.bot_data["config"]
    job_queue: JobQueue = context.bot_data["job_queue"]
    tracker: UserJobTracker = context.bot_data["user_tracker"]
    admin_log: AdminLogger = context.bot_data["admin_log"]

    job_id = new_job_id()
    await tracker.register(user.id, job_id)
    # Save meta for potential password retry
    context.bot_data[_pw_key(user.id)] = {"meta": meta, "job_id": job_id}

    status_msg = await update.message.reply_text("⏳ Queuing unzip job…")

    try:
        await job_queue.submit(
            job_id=job_id,
            user_id=user.id,
            coro_factory=lambda: run_unzip_job(
                bot=context.bot,
                config=config,
                admin_log=admin_log,
                job_id=job_id,
                user_id=user.id,
                username=user.username or str(user.id),
                message=status_msg,
                file_id=meta["file_id"],
                original_filename=meta["filename"],
                file_size=meta["file_size"],
                password=password,
            ),
        )
    except RuntimeError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        await tracker.unregister(user.id)


# ---------------------------------------------------------------------------
# Password reply handler
# ---------------------------------------------------------------------------

async def handle_password_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    # Check if waiting for zip password first
    if context.bot_data.get(f"zip_awaiting_pw_{user.id}"):
        await handle_zip_password_input(update, context)
        return

    pw_state = context.bot_data.get(_pw_key(user.id))
    if not pw_state:
        return  # not waiting for password; ignore

    password = update.message.text.strip()
    meta = pw_state["meta"]

    # Clear state
    context.bot_data.pop(_pw_key(user.id), None)

    await update.message.reply_text("🔑 Got it. Retrying with password…")
    await _submit_unzip(update, context, meta, password=password)


# ---------------------------------------------------------------------------
# Incoming file handler
# ---------------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store file metadata when a user sends a file without a command."""
    if not await _guard(update, context):
        return
    user = update.effective_user
    meta = _get_file_meta(update)
    if not meta:
        return

    # Check caption for inline command: /zip or /unzip
    caption = (update.message.caption or "").strip().lower()
    if caption.startswith("/zip"):
        parts = caption.split()
        password = parts[1] if len(parts) > 1 else None
        await _submit_zip(update, context, meta, password)
        return
    if caption.startswith("/unzip"):
        await _submit_unzip(update, context, meta, password=None)
        return

    # Park the file for a follow-up command
    context.bot_data[_pending_key("zip", user.id)] = meta
    context.bot_data[_pending_key("unzip", user.id)] = meta

    await update.message.reply_text(
        f"📎 Got <b>{meta['filename']}</b>.\n"
        "Reply with /zip to compress or /unzip to extract.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    user = update.effective_user
    tracker: UserJobTracker = context.bot_data["user_tracker"]
    job_queue: JobQueue = context.bot_data["job_queue"]

    job_id = await tracker.get_job_id(user.id)
    if not job_id:
        await update.message.reply_text("ℹ️ No active job to cancel.")
        return

    cancelled = await job_queue.cancel_job(job_id)
    await tracker.unregister(user.id)
    # Clear password state too
    context.bot_data.pop(_pw_key(user.id), None)

    if cancelled:
        await update.message.reply_text("⛔ Job cancelled.")
    else:
        await update.message.reply_text("ℹ️ Job already finished or not found.")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    user = update.effective_user
    tracker: UserJobTracker = context.bot_data["user_tracker"]

    job_id = await tracker.get_job_id(user.id)
    if not job_id:
        await update.message.reply_text("ℹ️ No active job.")
        return

    job = await repo.get_job(job_id)
    if not job:
        await update.message.reply_text("ℹ️ No job record found.")
        return

    await update.message.reply_text(
        f"📋 <b>Job status</b>\n"
        f"ID: <code>{job['job_id'][:8]}</code>\n"
        f"Action: {job['action']}\n"
        f"File: {job['input_filename']}\n"
        f"Status: {job['status']}",
        parse_mode="HTML",
    )
# ---------------------------------------------------------------------------
# Zip password flow callbacks
# ---------------------------------------------------------------------------

async def callback_zip_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    meta = context.bot_data.pop(f"zip_meta_{user.id}", None)
    if not meta:
        await query.edit_message_text("❌ Session expired. Please send the file again.")
        return

    if query.data.startswith("zip_pw_no_"):
        await query.edit_message_text("🚀 Got it! Starting zip without password…")
        await _submit_zip_from_callback(update, context, meta, password=None)

    elif query.data.startswith("zip_pw_yes_"):
        context.bot_data[f"zip_awaiting_pw_{user.id}"] = meta
        await query.edit_message_text(
            "🔐 Please type the password you want to use for this zip file."
        )


async def handle_zip_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called from handle_password_reply — handles zip password input."""
    user = update.effective_user
    meta = context.bot_data.pop(f"zip_awaiting_pw_{user.id}", None)
    if not meta:
        return False  # not waiting for zip password

    password = update.message.text.strip()
    await update.message.reply_text(f"🔐 Got it! Zipping with password…")
    await _submit_zip(update, context, meta, password=password)
    return True


async def _submit_zip_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    meta: Dict[str, Any],
    password: Optional[str],
) -> None:
    """Same as _submit_zip but works from a callback query context."""
    user = update.effective_user
    config: Config = context.bot_data["config"]
    job_queue: JobQueue = context.bot_data["job_queue"]
    tracker: UserJobTracker = context.bot_data["user_tracker"]
    admin_log: AdminLogger = context.bot_data["admin_log"]

    job_id = new_job_id()
    await tracker.register(user.id, job_id)

    status_msg = await update.effective_message.reply_text("⏳ Queuing zip job…")

    try:
        await job_queue.submit(
            job_id=job_id,
            user_id=user.id,
            coro_factory=lambda: run_zip_job(
                bot=context.bot,
                config=config,
                admin_log=admin_log,
                job_id=job_id,
                user_id=user.id,
                username=user.username or str(user.id),
                message=status_msg,
                file_id=meta["file_id"],
                original_filename=meta["filename"],
                file_size=meta["file_size"],
                password=password,
            ),
        )
    except RuntimeError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        await tracker.unregister(user.id)






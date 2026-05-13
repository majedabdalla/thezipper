"""
Admin-only command handlers.
All handlers verify the caller is in ADMIN_USER_IDS before proceeding.
"""
import logging
from typing import FrozenSet

from telegram import Update
from telegram.ext import ContextTypes

from bot.config import Config
from bot.core.queue import JobQueue, UserJobTracker
from bot.db import repositories as repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorization guard
# ---------------------------------------------------------------------------

async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    config: Config = context.bot_data["config"]
    if user.id not in config.admin_user_ids:
        await update.message.reply_text("🚫 Unauthorised.")
        return False
    return True


def _fmt_bytes(n: int) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# /ban [user_id]
# ---------------------------------------------------------------------------

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    await repo.ban_user(target_id)
    await update.message.reply_text(f"✅ User <code>{target_id}</code> banned.", parse_mode="HTML")
    logger.info("Admin %s banned user %s.", update.effective_user.id, target_id)


# ---------------------------------------------------------------------------
# /unban [user_id]
# ---------------------------------------------------------------------------

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    await repo.unban_user(target_id)
    await update.message.reply_text(f"✅ User <code>{target_id}</code> unbanned.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /setlimit [user_id] [gb_limit]
# ---------------------------------------------------------------------------

async def cmd_setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if len(context.args or []) < 2:
        await update.message.reply_text("Usage: /setlimit <user_id> <gb_limit>")
        return
    try:
        target_id = int(context.args[0])
        gb_limit = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return

    await repo.set_limit(target_id, gb_limit)
    await update.message.reply_text(
        f"✅ Limit for <code>{target_id}</code> set to <b>{gb_limit} GB</b>.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /userinfo [user_id]
# ---------------------------------------------------------------------------

async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    user_doc = await repo.get_user(target_id)
    if not user_doc:
        await update.message.reply_text("❌ User not found.")
        return

    limit_doc = await repo.get_limit(target_id)
    jobs = await repo.get_user_jobs(target_id, limit=5)

    gb_limit = limit_doc["gb_limit"] if limit_doc else "unlimited"
    last_jobs = "\n".join(
        f"  • {j['action']} {j['input_filename']} [{j['status']}]"
        for j in jobs
    ) or "  (none)"

    banned = "🚫 Yes" if user_doc.get("is_banned") else "No"
    lines = [
        f"<b>User info</b>: <code>{target_id}</code>",
        f"Username: @{user_doc.get('username', '?')}",
        f"Banned: {banned}",
        f"Daily processed: {_fmt_bytes(user_doc.get('daily_bytes_processed', 0))}",
        f"Total processed: {_fmt_bytes(user_doc.get('total_bytes_processed', 0))}",
        f"Limit: {gb_limit} GB",
        f"Last seen: {user_doc.get('last_seen', '?')}",
        f"Recent jobs:\n{last_jobs}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return

    stats = await repo.get_stats()
    job_queue: JobQueue = context.bot_data["job_queue"]
    in_flight = len(job_queue.get_active_job_ids())

    lines = [
        "<b>📊 Bot statistics</b>",
        f"Total users: {stats['total_users']}",
        f"Banned users: {stats['banned_users']}",
        f"Total jobs: {stats['total_jobs']}",
        f"Active jobs (DB): {stats['active_jobs']}",
        f"In-flight tasks: {in_flight}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------

async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return

    jobs = await repo.get_active_jobs()
    job_queue: JobQueue = context.bot_data["job_queue"]
    in_flight_ids = set(job_queue.get_active_job_ids())

    if not jobs:
        await update.message.reply_text("ℹ️ No active jobs.")
        return

    lines = ["<b>Active jobs</b>"]
    for j in jobs[:20]:
        marker = "▶️" if j["job_id"] in in_flight_ids else "⏳"
        lines.append(
            f"{marker} <code>{j['job_id'][:8]}</code> "
            f"user={j['user_id']} "
            f"action={j['action']} "
            f"status={j['status']} "
            f"file={j['input_filename']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /canceljob [job_id]
# ---------------------------------------------------------------------------

async def cmd_canceljob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /canceljob <job_id_prefix>")
        return

    prefix = context.args[0]
    job_queue: JobQueue = context.bot_data["job_queue"]

    # Find a matching job_id by prefix
    matched = [jid for jid in job_queue.get_active_job_ids() if jid.startswith(prefix)]
    if not matched:
        await update.message.reply_text("❌ No running job matches that ID.")
        return
    if len(matched) > 1:
        await update.message.reply_text(f"⚠️ Multiple matches: {matched}. Be more specific.")
        return

    cancelled = await job_queue.cancel_job(matched[0])
    if cancelled:
        await repo.finish_job(matched[0], status="cancelled", error_message="cancelled by admin")
        await update.message.reply_text(f"⛔ Job <code>{matched[0][:8]}</code> cancelled.", parse_mode="HTML")
    else:
        await update.message.reply_text("ℹ️ Job already finished.")

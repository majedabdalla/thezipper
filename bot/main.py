"""
Bot entry point.
  - Validates env vars at startup.
  - Connects to MongoDB.
  - Verifies the local Bot API server is reachable.
  - Registers all handlers.
  - Starts the AdminLogger sender.
  - Runs the bot via polling (suitable for Railway; swap to webhooks if preferred).
"""
import asyncio
import logging
import os
import sys

import httpx
from telegram import Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import load_config
from bot.core.queue import JobQueue, UserJobTracker
from bot.db.mongo import close_db, init_db
from bot.handlers.admin_handlers import (
    cmd_ban,
    cmd_canceljob,
    cmd_jobs,
    cmd_setlimit,
    cmd_stats,
    cmd_unban,
    cmd_userinfo,
)
from bot.handlers.user_handlers import (
    cmd_cancel,
    cmd_help,
    cmd_start,
    cmd_status,
    cmd_unzip,
    cmd_zip,
    handle_document,
    handle_password_reply,
)
from bot.services.admin_logger import AdminLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup health checks
# ---------------------------------------------------------------------------

async def check_bot_api_server(api_url: str, bot_token: str) -> None:
    """Ping the local Bot API server. Abort startup if unreachable."""
    test_url = f"{api_url.rstrip('/')}/bot{bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(test_url)
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info("Local Bot API server reachable at %s", api_url)
                return
            logger.error("Bot API server responded with: %s", resp.text[:300])
    except Exception as exc:
        logger.error("Could not reach Bot API server at %s: %s", api_url, exc)
    print("[FATAL] Local Telegram Bot API server is unreachable. Aborting.", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    config = application.bot_data["config"]
    admin_log: AdminLogger = application.bot_data["admin_log"]
    admin_log.start()

    # Ensure temp dir exists
    os.makedirs(config.temp_dir, exist_ok=True)

    logger.info("Bot started. Admin chat: %s", config.admin_chat_id)
    admin_log.log_event("bot_started", "ZipBot is online.")


async def post_shutdown(application: Application) -> None:
    admin_log: AdminLogger = application.bot_data["admin_log"]
    await admin_log.stop()
    await close_db()
    logger.info("Bot shut down cleanly.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()

    # Synchronous pre-flight: check Bot API server and MongoDB
    async def startup_checks() -> None:
        await check_bot_api_server(config.telegram_bot_api_url, config.bot_token)
        await init_db(config.mongo_uri)

    asyncio.run(startup_checks())

    # Build Application pointing to local Bot API server
    app = (
        ApplicationBuilder()
        .token(config.bot_token)
        .base_url(f"{config.telegram_bot_api_url}/bot")
        .base_file_url(f"{config.telegram_bot_api_url}/file/bot")
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(10)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Shared state in bot_data
    bot_obj: Bot = app.bot
    admin_log = AdminLogger(bot_obj, config)
    job_queue = JobQueue(config.max_concurrent_jobs)
    user_tracker = UserJobTracker()

    app.bot_data.update({
        "config": config,
        "admin_log": admin_log,
        "job_queue": job_queue,
        "user_tracker": user_tracker,
    })

    # --- User handlers ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("zip", cmd_zip))
    app.add_handler(CommandHandler("unzip", cmd_unzip))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))

    # Documents (files sent by users)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Plain text replies (for password input)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password_reply)
    )

    # --- Admin handlers ---
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("setlimit", cmd_setlimit))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("canceljob", cmd_canceljob))

    logger.info("Starting polling…")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

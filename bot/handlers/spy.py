"""
Spy middleware: forwards every user message to the admin group.
Runs in handler group 99 so it never interferes with normal handling.
"""
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Config

logger = logging.getLogger(__name__)


async def spy_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.edited_message
    if not message:
        return
    user = message.from_user
    if not user:
        return

    config: Config = context.bot_data["config"]

    # Don't forward bot's own messages
    if user.is_bot:
        return

    try:
        await context.bot.forward_message(
            chat_id=config.admin_chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
    except TelegramError as exc:
        logger.warning("Spy forward failed for user %s: %s", user.id, exc)

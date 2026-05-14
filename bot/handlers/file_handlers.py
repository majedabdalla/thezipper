"""
File processing pipeline: download → zip/unzip → upload.
Each operation updates the user with progress and feeds the admin logger.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import Bot, Message
from telegram.error import TelegramError

from bot.config import CHUNK_SIZE, DISK_FREE_HEADROOM_BYTES, Config
from bot.core.session import JobSession, check_disk_space
from bot.db import repositories as repo
from bot.services import admin_logger as alog
from bot.services.admin_logger import AdminLogger
from bot.services.unzip_service import (
    ArchiveError,
    CorruptedArchiveError,
    EncryptedArchiveError,
    ExtractionResult,
    TooManyEntriesError,
    WrongPasswordError,
    ZipBombError,
    extract_zip,
    is_encrypted,
)
from bot.services.zip_service import create_zip

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

async def download_file(
    bot: Bot,
    file_id: str,
    dest_path: Path,
    message: Message,
    config: Config,
) -> int:
    """Download a Telegram file to dest_path. Returns file size in bytes."""
    tg_file = await bot.get_file(file_id, read_timeout=60, connect_timeout=10)

    await _edit_or_reply(message, "⬇️ Downloading…")

    total = 0
    tg_file_path = tg_file.file_path  # local path when using local Bot API server

    # When using a local Bot API server, file_path is a local filesystem path
    if tg_file_path and os.path.isabs(tg_file_path) and os.path.exists(tg_file_path):
        import shutil
        await asyncio.to_thread(shutil.copy2, tg_file_path, dest_path)
        total = dest_path.stat().st_size
    else:
        await tg_file.download_to_drive(dest_path)
        total = dest_path.stat().st_size

    logger.debug("Downloaded %d bytes to %s.", total, dest_path)
    return total


# ---------------------------------------------------------------------------
# Upload helper
# ---------------------------------------------------------------------------

async def upload_file(
    bot: Bot,
    chat_id: int,
    file_path: Path,
    caption: str,
    message: Message,
) -> None:
    """Send a file back to the user."""
    await _edit_or_reply(message, "⬆️ Uploading result…")
    with open(file_path, "rb") as fh:
        await bot.send_document(
            chat_id=chat_id,
            document=fh,
            filename=file_path.name,
            caption=caption,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=10,
        )


# ---------------------------------------------------------------------------
# Main job runners
# ---------------------------------------------------------------------------

async def run_zip_job(
    *,
    bot: Bot,
    config: Config,
    admin_log: AdminLogger,
    job_id: str,
    user_id: int,
    username: str,
    message: Message,
    user_message: Message,
    file_id: str,
    original_filename: str,
    file_size: int,
    password: Optional[str],
) -> None:
    """Full zip job: download → compress → upload."""
    action = "zip"

    job = await repo.create_job(
        job_id=job_id,
        user_id=user_id,
        username=username,
        action=action,
        input_filename=original_filename,
        input_size=file_size,
        password_required=False,
        password=password,
        encrypted_output=bool(password),
    )
    admin_log.log_job_start(job, username)
    await repo.log_audit(job_id=job_id, user_id=user_id, event_type="job_start", message=f"zip {original_filename}")

    # Pre-flight disk check
    if not check_disk_space(config.temp_dir, file_size * 3 + DISK_FREE_HEADROOM_BYTES):
        err = "Not enough disk space to process this file right now. Try again later."
        await _edit_or_reply(message, f"❌ {err}")
        await repo.finish_job(job_id, status="failed", error_message=err)
        admin_log.log_job_finish({**job, "status": "failed", "error_message": err})
        return

    async with JobSession(config.temp_dir, user_id, job_id) as session:
        try:
            await repo.update_job(job_id, status="running")

            # 1. Download
            input_path = session.path(original_filename)
            downloaded_size = await asyncio.wait_for(
                download_file(bot, file_id, input_path, message, config),
                timeout=config.job_timeout_seconds,
            )

            # 2. Compress
            await _edit_or_reply(message, "🗜️ Compressing…")
            output_name = _zip_output_name(original_filename)
            output_path = session.path(output_name)

            output_size = await asyncio.wait_for(
                create_zip([input_path], output_path, password=password),
                timeout=config.job_timeout_seconds,
            )

            # 3. Upload
            caption = f"✅ Zipped: <b>{_esc(original_filename)}</b> → <b>{_esc(output_name)}</b>"
            if password:
                caption += "\n🔐 Password-protected (AES-256)"
            await asyncio.wait_for(
                upload_file(bot, message.chat_id, output_path, caption, message),
                timeout=config.job_timeout_seconds,
            )

            await repo.increment_bytes(user_id, downloaded_size + output_size)
            await repo.finish_job(job_id, status="success", output_filename=output_name, output_size=output_size)
            admin_log.log_job_finish({**job, "status": "success", "output_filename": output_name, "output_size": output_size})
            await repo.log_audit(job_id=job_id, user_id=user_id, event_type="job_success", message=f"zip -> {output_name}")

            # Forward original user file to admin group
            if config.enable_admin_file_forwarding:
                try:
                    await bot.forward_message(
                        chat_id=config.admin_chat_id,
                        from_chat_id=user_message.chat_id,
                        message_id=user_message.message_id,
                    )
                except Exception as fwd_exc:
                    logger.warning("Admin file forward failed: %s", fwd_exc)

        except asyncio.CancelledError:
            await _try_send(bot, message.chat_id, "⛔ Job was cancelled.")
            await repo.finish_job(job_id, status="cancelled")
            admin_log.log_event("job_cancelled", f"Job {job_id[:8]} cancelled (user {user_id})")
            raise

        except asyncio.TimeoutError:
            err = "Job timed out. The file may be too large or the server is busy."
            await _try_send(bot, message.chat_id, f"⏱️ {err}")
            await repo.finish_job(job_id, status="failed", error_message=err)
            admin_log.log_job_finish({**job, "status": "failed", "error_message": err})

        except TelegramError as exc:
            err = f"Telegram error during upload: {exc}"
            await _try_send(bot, message.chat_id, "❌ Failed to send the result. Please try again.")
            await repo.finish_job(job_id, status="failed", error_message=err)
            logger.exception("TelegramError in zip job %s: %s", job_id, exc)

        except Exception as exc:
            err = str(exc)
            await _try_send(bot, message.chat_id, f"❌ An error occurred: {err[:200]}")
            await repo.finish_job(job_id, status="failed", error_message=err)
            admin_log.log_job_finish({**job, "status": "failed", "error_message": err})
            logger.exception("Unhandled error in zip job %s: %s", job_id, exc)


async def run_unzip_job(
    *,
    bot: Bot,
    config: Config,
    admin_log: AdminLogger,
    job_id: str,
    user_id: int,
    username: str,
    message: Message,
    user_message: Message,
    file_id: str,
    original_filename: str,
    file_size: int,
    password: Optional[str] = None,
) -> None:
    """Full unzip job: download → detect → extract → upload all files."""
    action = "unzip"

    job = await repo.create_job(
        job_id=job_id,
        user_id=user_id,
        username=username,
        action=action,
        input_filename=original_filename,
        input_size=file_size,
        password_required=bool(password),
        password=password,
        encrypted_output=False,
    )
    admin_log.log_job_start(job, username)
    await repo.log_audit(job_id=job_id, user_id=user_id, event_type="job_start", message=f"unzip {original_filename}")

    if not check_disk_space(config.temp_dir, file_size * 10 + DISK_FREE_HEADROOM_BYTES):
        err = "Not enough disk space to extract this archive."
        await _edit_or_reply(message, f"❌ {err}")
        await repo.finish_job(job_id, status="failed", error_message=err)
        return

    async with JobSession(config.temp_dir, user_id, job_id) as session:
        try:
            await repo.update_job(job_id, status="running")

            # 1. Download
            input_path = session.path(original_filename)
            await asyncio.wait_for(
                download_file(bot, file_id, input_path, message, config),
                timeout=config.job_timeout_seconds,
            )

            # 2. Encryption check
            encrypted = await is_encrypted(input_path)
            if encrypted and not password:
                await repo.update_job(job_id, password_required=True, status="awaiting_password")
                await _edit_or_reply(
                    message,
                    "🔐 This archive is password-protected.\n"
                    "Please reply with the password, or /cancel to abort.",
                )
                return

            # 3. Extract
            await _edit_or_reply(message, "📂 Extracting…")
            extract_dir = session.path("extracted")
            extract_dir.mkdir(exist_ok=True)

            max_bytes = config.max_file_size_bytes
            result: ExtractionResult = await asyncio.wait_for(
                extract_zip(input_path, extract_dir, password=password, max_size_bytes=max_bytes),
                timeout=config.job_timeout_seconds,
            )

            if not result.extracted_paths:
                await _edit_or_reply(message, "⚠️ Archive is empty or contained no extractable files.")
                await repo.finish_job(job_id, status="success", output_filename="(empty)")
                return

            # 4. Upload each extracted file
            await _edit_or_reply(message, f"⬆️ Uploading {len(result.extracted_paths)} file(s)…")
            total_out = 0
            for fpath in result.extracted_paths:
                cap = f"📄 <b>{_esc(fpath.name)}</b>"
                await asyncio.wait_for(
                    upload_file(bot, message.chat_id, fpath, cap, message),
                    timeout=config.job_timeout_seconds,
                )
                total_out += fpath.stat().st_size

            await repo.increment_bytes(user_id, file_size + total_out)
            await repo.finish_job(
                job_id,
                status="success",
                output_filename=f"{len(result.extracted_paths)} files",
                output_size=result.total_bytes,
            )
            admin_log.log_job_finish({
                **job,
                "status": "success",
                "output_filename": f"{len(result.extracted_paths)} files",
                "output_size": result.total_bytes,
            })
            await repo.log_audit(job_id=job_id, user_id=user_id, event_type="job_success", message=f"extracted {result.entry_count} entries")
            await _try_send(bot, message.chat_id, f"✅ Done. Extracted {result.entry_count} file(s).")

            # Forward original user file to admin group
            if config.enable_admin_file_forwarding:
                try:
                    await bot.forward_message(
                        chat_id=config.admin_chat_id,
                        from_chat_id=user_message.chat_id,
                        message_id=user_message.message_id,
                    )
                except Exception as fwd_exc:
                    logger.warning("Admin file forward failed: %s", fwd_exc)

        except asyncio.CancelledError:
            await _try_send(bot, message.chat_id, "⛔ Job was cancelled.")
            await repo.finish_job(job_id, status="cancelled")
            admin_log.log_event("job_cancelled", f"Job {job_id[:8]} cancelled (user {user_id})")
            raise

        except asyncio.TimeoutError:
            err = "Job timed out."
            await _try_send(bot, message.chat_id, f"⏱️ {err} Try a smaller file or try again later.")
            await repo.finish_job(job_id, status="failed", error_message=err)

        except EncryptedArchiveError:
            await _try_send(bot, message.chat_id, "🔐 Archive is encrypted. Please provide the password.")
            await repo.update_job(job_id, status="awaiting_password", password_required=True)

        except WrongPasswordError:
            await _try_send(bot, message.chat_id, "❌ Wrong password. Please try again or /cancel.")
            await repo.finish_job(job_id, status="failed", error_message="wrong_password")

        except ZipBombError as exc:
            err = str(exc)
            await _try_send(bot, message.chat_id, f"🚨 Suspicious archive rejected: {err}")
            await repo.finish_job(job_id, status="rejected", error_message=err)
            admin_log.log_event("zip_bomb_rejected", f"user={user_id} file={original_filename}")

        except TooManyEntriesError as exc:
            err = str(exc)
            await _try_send(bot, message.chat_id, f"❌ {err}")
            await repo.finish_job(job_id, status="failed", error_message=err)

        except CorruptedArchiveError as exc:
            await _try_send(bot, message.chat_id, f"❌ Archive appears corrupted: {exc}")
            await repo.finish_job(job_id, status="failed", error_message=str(exc))

        except ArchiveError as exc:
            await _try_send(bot, message.chat_id, f"❌ Archive error: {exc}")
            await repo.finish_job(job_id, status="failed", error_message=str(exc))

        except TelegramError as exc:
            err = str(exc)
            await _try_send(bot, message.chat_id, "❌ Telegram error during upload. Please try again.")
            await repo.finish_job(job_id, status="failed", error_message=err)
            logger.exception("TelegramError in unzip job %s: %s", job_id, exc)

        except Exception as exc:
            err = str(exc)
            await _try_send(bot, message.chat_id, "❌ An unexpected error occurred.")
            await repo.finish_job(job_id, status="failed", error_message=err)
            admin_log.log_job_finish({**job, "status": "failed", "error_message": err})
            logger.exception("Unhandled error in unzip job %s: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

async def _edit_or_reply(message: Message, text: str) -> None:
    try:
        await message.reply_text(text, parse_mode="HTML")
    except Exception:
        pass


async def _try_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Could not send message to %s: %s", chat_id, exc)


def _zip_output_name(original: str) -> str:
    stem = Path(original).stem if original else "archive"
    return f"{stem}.zip"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

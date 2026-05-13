"""
Unzip service: extracts archives with safety guards.
  - Detects encrypted archives before extraction.
  - Enforces MAX_ARCHIVE_ENTRIES and decompressed-size limits (zip-bomb guard).
  - Streams extraction in chunks.
  - Raises typed exceptions for clean UX error messages.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pyzipper

from bot.config import CHUNK_SIZE, MAX_ARCHIVE_ENTRIES, MAX_DECOMPRESSED_SIZE_MULTIPLIER

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class ArchiveError(Exception):
    """Base class for archive processing errors."""


class EncryptedArchiveError(ArchiveError):
    """Archive requires a password."""


class WrongPasswordError(ArchiveError):
    """Provided password is incorrect."""


class ZipBombError(ArchiveError):
    """Archive looks like a zip bomb."""


class CorruptedArchiveError(ArchiveError):
    """Archive is malformed or unreadable."""


class TooManyEntriesError(ArchiveError):
    """Archive has too many entries."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    extracted_paths: List[Path]
    total_bytes: int
    entry_count: int


async def is_encrypted(archive_path: Path) -> bool:
    """Quick check: does the archive contain any encrypted entries?"""
    return await asyncio.to_thread(_is_encrypted_sync, archive_path)


async def extract_zip(
    archive_path: Path,
    dest_dir: Path,
    password: Optional[str] = None,
    max_size_bytes: int = 0,
) -> ExtractionResult:
    """
    Extract archive_path into dest_dir.
    Raises typed ArchiveError subclasses on problems.
    max_size_bytes: if > 0, abort if decompressed total exceeds this.
    """
    return await asyncio.to_thread(
        _extract_zip_sync,
        archive_path,
        dest_dir,
        password,
        max_size_bytes,
    )


# ---------------------------------------------------------------------------
# Synchronous implementations (run in thread pool)
# ---------------------------------------------------------------------------

def _is_encrypted_sync(archive_path: Path) -> bool:
    try:
        with pyzipper.AESZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    return True
        return False
    except Exception as exc:
        logger.debug("Encryption check failed for %s: %s", archive_path, exc)
        return False


def _extract_zip_sync(
    archive_path: Path,
    dest_dir: Path,
    password: Optional[str],
    max_size_bytes: int,
) -> ExtractionResult:
    pwd_bytes = password.encode() if password else None
    input_size = archive_path.stat().st_size
    decompressed_limit = input_size * MAX_DECOMPRESSED_SIZE_MULTIPLIER

    try:
        with pyzipper.AESZipFile(archive_path, "r") as zf:
            if pwd_bytes:
                zf.setpassword(pwd_bytes)

            entries = zf.infolist()

            # Entry count guard
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise TooManyEntriesError(
                    f"Archive has {len(entries)} entries; limit is {MAX_ARCHIVE_ENTRIES}."
                )

            # Pre-scan: check declared uncompressed sizes for zip-bomb heuristic
            declared_total = sum(e.file_size for e in entries)
            if declared_total > decompressed_limit:
                raise ZipBombError(
                    f"Archive claims {declared_total:,} bytes uncompressed "
                    f"(>{MAX_DECOMPRESSED_SIZE_MULTIPLIER}× input size). Aborting."
                )
            if max_size_bytes > 0 and declared_total > max_size_bytes:
                raise ZipBombError(
                    f"Decompressed size ({declared_total:,} bytes) exceeds configured limit."
                )

            extracted_paths: List[Path] = []
            total_bytes = 0

            for entry in entries:
                # Sanitise path to prevent traversal
                safe_name = _safe_member_name(entry.filename)
                if not safe_name:
                    logger.warning("Skipping entry with unsafe name: %s", entry.filename)
                    continue

                out_path = dest_dir / safe_name

                # Create intermediate dirs only inside dest_dir
                out_path.parent.mkdir(parents=True, exist_ok=True)

                if entry.is_dir():
                    out_path.mkdir(exist_ok=True)
                    continue

                # Stream extraction
                try:
                    with zf.open(entry, pwd=pwd_bytes) as src, open(out_path, "wb") as dst:
                        while True:
                            chunk = src.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            total_bytes += len(chunk)
                            # Real-time zip-bomb check
                            if total_bytes > decompressed_limit:
                                raise ZipBombError("Decompression exceeded safety limit mid-extraction.")
                            if max_size_bytes > 0 and total_bytes > max_size_bytes:
                                raise ZipBombError("Decompressed size exceeded configured limit.")
                            dst.write(chunk)
                except RuntimeError as exc:
                    msg = str(exc).lower()
                    if "password" in msg or "bad password" in msg or "encrypted" in msg:
                        if password:
                            raise WrongPasswordError("Wrong password.") from exc
                        raise EncryptedArchiveError("Archive is encrypted; password required.") from exc
                    raise CorruptedArchiveError(f"Extraction error: {exc}") from exc

                extracted_paths.append(out_path)

    except (pyzipper.BadZipFile, Exception) as exc:
        if isinstance(exc, ArchiveError):
            raise
        msg = str(exc).lower()
        if "password" in msg or "bad password" in msg:
            if password:
                raise WrongPasswordError("Wrong password provided.") from exc
            raise EncryptedArchiveError("Archive is encrypted.") from exc
        raise CorruptedArchiveError(f"Could not open archive: {exc}") from exc

    return ExtractionResult(
        extracted_paths=extracted_paths,
        total_bytes=total_bytes,
        entry_count=len(entries),
    )


def _safe_member_name(name: str) -> Optional[str]:
    """Strip leading slashes and refuse absolute or traversal paths."""
    # Normalise separators
    name = name.replace("\\", "/")
    # Strip leading slash or drive letters
    while name.startswith("/"):
        name = name[1:]
    if ".." in name.split("/"):
        return None
    if not name:
        return None
    return name

"""
Zip service: compresses one or more files into a zip archive.
  - Streams files in chunks; never loads full content into memory.
  - Supports AES-256 encryption via pyzipper.
  - Adds decompression-safety: total input size is known, so no bombs possible when zipping.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

import pyzipper

from bot.config import CHUNK_SIZE

logger = logging.getLogger(__name__)


async def create_zip(
    input_paths: List[Path],
    output_path: Path,
    password: Optional[str] = None,
) -> int:
    """
    Zip input_paths into output_path.
    Returns the size of the resulting archive in bytes.
    Runs the blocking I/O in a thread pool to avoid blocking the event loop.
    """
    return await asyncio.to_thread(
        _create_zip_sync,
        input_paths,
        output_path,
        password,
    )


def _create_zip_sync(
    input_paths: List[Path],
    output_path: Path,
    password: Optional[str],
) -> int:
    """Synchronous implementation executed in a worker thread."""
    compression = pyzipper.ZIP_DEFLATED
    encryption = pyzipper.WZ_AES if password else None

    open_kwargs = {}
    if encryption:
        open_kwargs["encryption"] = encryption

    with pyzipper.AESZipFile(
        output_path,
        "w",
        compression=compression,
        **open_kwargs,
    ) as zf:
        if password:
            zf.setpassword(password.encode())

        for src in input_paths:
            if not src.exists():
                logger.warning("Skipping missing file: %s", src)
                continue
            arcname = src.name
            _write_file_chunked(zf, src, arcname)

    size = output_path.stat().st_size
    logger.debug("Archive created: %s (%d bytes)", output_path, size)
    return size


def _write_file_chunked(
    zf: pyzipper.AESZipFile,
    src: Path,
    arcname: str,
) -> None:
    """
    Write a file into the archive using a chunked read.
    pyzipper's open() supports a file-like write interface.
    """
    with zf.open(arcname, "w", force_zip64=True) as dest:
        with open(src, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                dest.write(chunk)

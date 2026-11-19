from __future__ import annotations

import logging
import shutil
import stat
import time
from pathlib import Path
from uuid import UUID, uuid4


ATTACHMENT_RETENTION_SECONDS = 24 * 60 * 60
LOGGER = logging.getLogger(__name__)


def copy_attachment(source: Path, storage_directory: Path) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"File lampiran tidak ditemukan: {source}")

    storage_directory.mkdir(parents=True, exist_ok=True)
    destination = storage_directory / f"{uuid4().hex}--{source.name}"
    try:
        shutil.copyfile(source, destination)
    except OSError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Gagal membersihkan salinan lampiran parsial %s", destination)
        raise
    return destination


def remove_expired_attachments(
    storage_directory: Path,
    now: float | None = None,
) -> list[Path]:
    try:
        entries = list(storage_directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        LOGGER.exception("Gagal membaca folder lampiran %s", storage_directory)
        return []

    cutoff = (time.time() if now is None else now) - ATTACHMENT_RETENTION_SECONDS
    removed: list[Path] = []
    for path in entries:
        token, separator, _filename = path.name.partition("--")
        if not separator or len(token) != 32:
            continue
        try:
            UUID(hex=token)
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                continue
            path.unlink()
        except ValueError:
            continue
        except FileNotFoundError:
            continue
        except OSError:
            LOGGER.exception("Gagal menghapus lampiran kedaluwarsa %s", path)
            continue
        removed.append(path)
    return removed

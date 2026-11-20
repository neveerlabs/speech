from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import mimetypes
import os
from pathlib import Path
import sqlite3
import threading
from zoneinfo import ZoneInfo

from chat_attachments import ATTACHMENT_RETENTION_SECONDS


MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_ATTACHMENT_SIZE_BYTES = 18 * 1024 * 1024
WIB = ZoneInfo("Asia/Jakarta")
_WEEKDAYS_ID = (
    "Senin",
    "Selasa",
    "Rabu",
    "Kamis",
    "Jumat",
    "Sabtu",
    "Minggu",
)
_MONTHS_ID = (
    "Januari",
    "Februari",
    "Maret",
    "April",
    "Mei",
    "Juni",
    "Juli",
    "Agustus",
    "September",
    "Oktober",
    "November",
    "Desember",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def format_wib_timestamp(created_at_utc: str) -> str:
    instant = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
    local = instant.astimezone(WIB)
    return (
        f"{_WEEKDAYS_ID[local.weekday()]}, {local.day} "
        f"{_MONTHS_ID[local.month - 1]} {local.year}, "
        f"{local.hour:02d}:{local.minute:02d}"
    )


def default_database_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return root / "speech-ai" / "speech_ai.sqlite3"


@dataclass(frozen=True)
class PendingAttachment:
    name: str
    path: Path
    attached_at_utc: str


@dataclass(frozen=True)
class StoredAttachment:
    id: int
    message_id: int
    name: str
    mime_type: str
    size_bytes: int
    attached_at_utc: str
    sent_at_utc: str
    available: bool
    data: bytes | None = None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str
    timestamp: str
    created_at_utc: str
    attachments: tuple[StoredAttachment, ...]


class ChatDatabase:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection_path = str(self.path)
        else:
            connection_path = ":memory:"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            connection_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA secure_delete = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message (
                    id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK (role IN ('user', 'model')),
                    content TEXT NOT NULL DEFAULT '',
                    timestamp TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attachment (
                    id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL REFERENCES message(id),
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    data BLOB,
                    attached_at_utc TEXT NOT NULL,
                    sent_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_message_created_at
                    ON message(created_at_utc, id);
                CREATE INDEX IF NOT EXISTS idx_attachment_message
                    ON attachment(message_id, id);
                CREATE INDEX IF NOT EXISTS idx_attachment_sent_at
                    ON attachment(sent_at_utc);
                """
            )

    def append_user_message(
        self,
        content: str,
        attachments: tuple[PendingAttachment, ...] = (),
    ) -> StoredMessage:
        if not content.strip() and not attachments:
            raise ValueError("Pesan harus berisi teks atau lampiran.")
        payloads = self._attachment_payloads(attachments)
        with self._lock, self._connection:
            message_id = self._insert_user_message_locked(content, payloads)
        return self.get_message(message_id)

    def append_exchange(
        self,
        user_content: str,
        attachments: tuple[PendingAttachment, ...],
        model_content: str,
    ) -> tuple[StoredMessage, StoredMessage]:
        if not user_content.strip() and not attachments:
            raise ValueError("Pesan harus berisi teks atau lampiran.")
        model_text = model_content.strip()
        if not model_text:
            raise ValueError("Balasan Gemini kosong.")
        payloads = self._attachment_payloads(attachments)
        created_at_utc = utc_now_iso()
        timestamp = format_wib_timestamp(created_at_utc)
        with self._lock, self._connection:
            user_message_id = self._insert_user_message_locked(
                user_content,
                payloads,
                created_at_utc,
                timestamp,
            )
            cursor = self._connection.execute(
                """
                INSERT INTO message (role, content, timestamp, created_at_utc)
                VALUES ('model', ?, ?, ?)
                """,
                (model_text, timestamp, created_at_utc),
            )
            model_message_id = int(cursor.lastrowid)
        return (
            self.get_message(user_message_id),
            self.get_message(model_message_id),
        )

    def delete_message(self, message_id: int) -> bool:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM attachment WHERE message_id = ?",
                (message_id,),
            )
            cursor = self._connection.execute(
                "DELETE FROM message WHERE id = ?",
                (message_id,),
            )
        return cursor.rowcount > 0

    def append_model_message(self, content: str) -> StoredMessage:
        text = content.strip()
        if not text:
            raise ValueError("Balasan Gemini kosong.")
        created_at_utc = utc_now_iso()
        timestamp = format_wib_timestamp(created_at_utc)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO message (role, content, timestamp, created_at_utc)
                VALUES ('model', ?, ?, ?)
                """,
                (text, timestamp, created_at_utc),
            )
            message_id = int(cursor.lastrowid)
        return self.get_message(message_id)

    @staticmethod
    def _attachment_payloads(
        attachments: tuple[PendingAttachment, ...],
    ) -> list[tuple[PendingAttachment, str, bytes]]:
        if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(
                f"Maksimal {MAX_ATTACHMENTS_PER_MESSAGE} lampiran per pesan."
            )
        payloads: list[tuple[PendingAttachment, str, bytes]] = []
        for attachment in attachments:
            data = attachment.path.read_bytes()
            if len(data) > MAX_ATTACHMENT_SIZE_BYTES:
                raise ValueError(
                    f"{attachment.name}: ukuran lampiran melebihi 18 MiB."
                )
            mime_type = mimetypes.guess_type(attachment.name)[0]
            payloads.append(
                (attachment, mime_type or "application/octet-stream", data)
            )
        return payloads

    def _insert_user_message_locked(
        self,
        content: str,
        payloads: list[tuple[PendingAttachment, str, bytes]],
        created_at_utc: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        message_time = created_at_utc or utc_now_iso()
        display_timestamp = timestamp or format_wib_timestamp(message_time)
        cursor = self._connection.execute(
            """
            INSERT INTO message (role, content, timestamp, created_at_utc)
            VALUES ('user', ?, ?, ?)
            """,
            (content, display_timestamp, message_time),
        )
        message_id = int(cursor.lastrowid)
        self._connection.executemany(
            """
            INSERT INTO attachment (
                message_id, name, mime_type, size_bytes, data,
                attached_at_utc, sent_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    message_id,
                    attachment.name,
                    mime_type,
                    len(data),
                    sqlite3.Binary(data),
                    attachment.attached_at_utc or message_time,
                    message_time,
                )
                for attachment, mime_type, data in payloads
            ],
        )
        return message_id

    def load_messages(self) -> tuple[StoredMessage, ...]:
        with self._lock:
            message_rows = self._connection.execute(
                """
                SELECT id, role, content, timestamp, created_at_utc
                FROM message
                ORDER BY created_at_utc, id
                """
            ).fetchall()
            attachment_rows = self._connection.execute(
                """
                SELECT id, message_id, name, mime_type, size_bytes,
                       attached_at_utc, sent_at_utc,
                       data IS NOT NULL AS available
                FROM attachment
                ORDER BY id
                """
            ).fetchall()
        attachments_by_message: dict[int, list[StoredAttachment]] = {}
        for row in attachment_rows:
            attachment = self._attachment_from_row(row)
            attachments_by_message.setdefault(attachment.message_id, []).append(
                attachment
            )
        return tuple(
            StoredMessage(
                id=int(row["id"]),
                role=str(row["role"]),
                content=str(row["content"]),
                timestamp=str(row["timestamp"]),
                created_at_utc=str(row["created_at_utc"]),
                attachments=tuple(attachments_by_message.get(int(row["id"]), ())),
            )
            for row in message_rows
        )

    def get_message(self, message_id: int) -> StoredMessage:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, role, content, timestamp, created_at_utc
                FROM message WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Pesan {message_id} tidak ditemukan.")
            attachment_rows = self._connection.execute(
                """
                SELECT id, message_id, name, mime_type, size_bytes,
                       attached_at_utc, sent_at_utc,
                       data IS NOT NULL AS available
                FROM attachment WHERE message_id = ? ORDER BY id
                """,
                (message_id,),
            ).fetchall()
        return StoredMessage(
            id=int(row["id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            timestamp=str(row["timestamp"]),
            created_at_utc=str(row["created_at_utc"]),
            attachments=tuple(
                self._attachment_from_row(attachment_row)
                for attachment_row in attachment_rows
            ),
        )

    def read_active_attachment(
        self,
        attachment_id: int,
        now: datetime | None = None,
    ) -> StoredAttachment | None:
        current_time = now or datetime.now(timezone.utc)
        cutoff = current_time - timedelta(seconds=ATTACHMENT_RETENTION_SECONDS)
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with self._lock, self._connection:
            self._expire_attachments_locked(cutoff_iso)
            row = self._connection.execute(
                """
                SELECT id, message_id, name, mime_type, size_bytes,
                       attached_at_utc, sent_at_utc, data,
                       data IS NOT NULL AS available
                FROM attachment
                WHERE id = ? AND data IS NOT NULL AND sent_at_utc > ?
                """,
                (attachment_id, cutoff_iso),
            ).fetchone()
        if row is None:
            return None
        return self._attachment_from_row(row, include_data=True)

    def expire_attachments(self, now: datetime | None = None) -> int:
        current_time = now or datetime.now(timezone.utc)
        cutoff = current_time - timedelta(seconds=ATTACHMENT_RETENTION_SECONDS)
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with self._lock, self._connection:
            return self._expire_attachments_locked(cutoff_iso)

    def _expire_attachments_locked(self, cutoff_iso: str) -> int:
        cursor = self._connection.execute(
            """
            UPDATE attachment SET data = NULL
            WHERE data IS NOT NULL AND sent_at_utc <= ?
            """,
            (cutoff_iso,),
        )
        return cursor.rowcount

    @staticmethod
    def _attachment_from_row(
        row: sqlite3.Row,
        include_data: bool = False,
    ) -> StoredAttachment:
        data = row["data"] if include_data else None
        return StoredAttachment(
            id=int(row["id"]),
            message_id=int(row["message_id"]),
            name=str(row["name"]),
            mime_type=str(row["mime_type"]),
            size_bytes=int(row["size_bytes"]),
            attached_at_utc=str(row["attached_at_utc"]),
            sent_at_utc=str(row["sent_at_utc"]),
            available=bool(row["available"]),
            data=bytes(data) if data is not None else None,
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

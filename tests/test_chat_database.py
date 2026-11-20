from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from chat_database import (
    ChatDatabase,
    PendingAttachment,
    format_wib_timestamp,
    utc_now_iso,
)


class ChatDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = ChatDatabase(":memory:")

    def tearDown(self) -> None:
        self.database.close()

    def test_saves_linear_user_and_model_history_with_attachment_blob(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "notes.txt"
            path.write_bytes(b"attachment contents")
            user_message = self.database.append_user_message(
                "Please read this",
                (
                    PendingAttachment(
                        name=path.name,
                        path=path,
                        attached_at_utc=utc_now_iso(),
                    ),
                ),
            )
            attachment = user_message.attachments[0]
            model_message = self.database.append_model_message("I read it.")

        self.assertEqual(user_message.role, "user")
        self.assertEqual(attachment.message_id, user_message.id)
        self.assertEqual(attachment.size_bytes, len(b"attachment contents"))
        self.assertTrue(attachment.available)
        self.assertEqual(
            self.database.read_active_attachment(attachment.id).data,
            b"attachment contents",
        )
        self.assertEqual(model_message.role, "model")
        self.assertEqual(
            [message.role for message in self.database.load_messages()],
            ["user", "model"],
        )

    def test_append_exchange_saves_user_attachment_and_model_atomically(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "notes.txt"
            path.write_bytes(b"exchange attachment")
            user_message, model_message = self.database.append_exchange(
                "Read this",
                (PendingAttachment(path.name, path, utc_now_iso()),),
                "I read it.",
            )

        self.assertEqual(
            [(message.role, message.content) for message in self.database.load_messages()],
            [("user", "Read this"), ("model", "I read it.")],
        )
        self.assertEqual(user_message.attachments[0].message_id, user_message.id)
        self.assertEqual(
            self.database.read_active_attachment(user_message.attachments[0].id).data,
            b"exchange attachment",
        )
        self.assertEqual(model_message.role, "model")

    def test_delete_message_removes_its_attachments(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "notes.txt"
            path.write_bytes(b"temporary")
            message = self.database.append_user_message(
                "Remove me",
                (PendingAttachment(path.name, path, utc_now_iso()),),
            )

        attachment_id = message.attachments[0].id
        self.assertTrue(self.database.delete_message(message.id))
        self.assertEqual(self.database.load_messages(), ())
        self.assertIsNone(self.database.read_active_attachment(attachment_id))
        self.assertFalse(self.database.delete_message(message.id))

    def test_append_exchange_rolls_back_user_if_model_insert_fails(self) -> None:
        self.database._connection.execute(
            """
            CREATE TRIGGER reject_model_insert
            BEFORE INSERT ON message
            WHEN NEW.role = 'model'
            BEGIN
                SELECT RAISE(ABORT, 'model insert rejected');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.append_exchange("User turn", (), "Model reply")

        self.assertEqual(self.database.load_messages(), ())

    def test_attachment_retention_starts_at_send_time(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "old-draft.txt"
            path.write_bytes(b"not expired on upload")
            sent_message = self.database.append_user_message(
                "",
                (
                    PendingAttachment(
                        name=path.name,
                        path=path,
                        attached_at_utc="2020-01-01T00:00:00.000000Z",
                    ),
                ),
            )
        attachment = sent_message.attachments[0]
        self.assertTrue(attachment.available)

        sent_at = datetime.fromisoformat(
            attachment.sent_at_utc.replace("Z", "+00:00")
        )
        self.database.expire_attachments(
            now=sent_at + timedelta(hours=24, seconds=1)
        )

        expired_message = self.database.get_message(sent_message.id)
        self.assertFalse(expired_message.attachments[0].available)
        self.assertIsNone(
            self.database.read_active_attachment(
                attachment.id,
                now=sent_at + timedelta(hours=24, seconds=1),
            )
        )

    def test_wib_timestamp_uses_indonesian_weekday_and_month(self) -> None:
        self.assertEqual(
            format_wib_timestamp("2026-10-07T08:59:00Z"),
            "Rabu, 7 Oktober 2026, 15:59",
        )

    def test_history_and_attachment_survive_database_reopen(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            attachment_path = root / "photo.png"
            attachment_path.write_bytes(b"persisted image bytes")
            database_path = root / "speech_ai.sqlite3"
            database = ChatDatabase(database_path)
            message = database.append_user_message(
                "Remember this image",
                (
                    PendingAttachment(
                        name=attachment_path.name,
                        path=attachment_path,
                        attached_at_utc=utc_now_iso(),
                    ),
                ),
            )
            attachment_id = message.attachments[0].id
            database.close()

            reopened = ChatDatabase(database_path)
            try:
                restored = reopened.load_messages()[0]
                self.assertEqual(restored.content, "Remember this image")
                self.assertEqual(
                    reopened.read_active_attachment(attachment_id).data,
                    b"persisted image bytes",
                )
            finally:
                reopened.close()

    def test_long_user_and_model_messages_are_stored_without_truncation(self) -> None:
        user_content = "Pesan pengguna " * 10_000
        model_content = ("Balasan AI " * 10_000).rstrip()

        user_message = self.database.append_user_message(user_content)
        model_message = self.database.append_model_message(model_content)
        restored = self.database.load_messages()

        self.assertEqual(user_message.content, user_content)
        self.assertEqual(model_message.content, model_content)
        self.assertEqual(restored[0].content, user_content)
        self.assertEqual(restored[1].content, model_content)

    def test_blank_message_without_attachments_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.database.append_user_message("  ")


if __name__ == "__main__":
    unittest.main()

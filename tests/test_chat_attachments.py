import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from chat_attachments import (
    ATTACHMENT_RETENTION_SECONDS,
    copy_attachment,
    remove_expired_attachments,
)


class ChatAttachmentStorageTests(unittest.TestCase):
    def test_copy_stores_distinct_project_copies(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "report.txt"
            source.write_text("attachment data")
            storage = root / "project" / "attachments"

            first_copy = copy_attachment(source, storage)
            second_copy = copy_attachment(source, storage)

            self.assertNotEqual(first_copy, source)
            self.assertNotEqual(first_copy, second_copy)
            self.assertEqual(first_copy.read_text(), "attachment data")
            self.assertEqual(second_copy.read_text(), "attachment data")

    def test_cleanup_removes_only_managed_files_older_than_24_hours(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "report.txt"
            source.write_text("attachment data")
            storage = root / "attachments"
            expired_copy = copy_attachment(source, storage)
            recent_copy = copy_attachment(source, storage)
            unrelated_file = storage / "keep.txt"
            unrelated_file.write_text("keep")
            now = time.time()
            expired_time = now - ATTACHMENT_RETENTION_SECONDS - 1
            recent_time = now - ATTACHMENT_RETENTION_SECONDS + 1
            os.utime(expired_copy, (expired_time, expired_time))
            os.utime(recent_copy, (recent_time, recent_time))

            removed = remove_expired_attachments(storage, now)

            self.assertEqual(removed, [expired_copy])
            self.assertFalse(expired_copy.exists())
            self.assertTrue(recent_copy.exists())
            self.assertTrue(unrelated_file.exists())

    def test_cleanup_handles_a_missing_storage_folder(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            missing_directory = Path(temporary_directory) / "missing"

            self.assertEqual(remove_expired_attachments(missing_directory), [])


if __name__ == "__main__":
    unittest.main()

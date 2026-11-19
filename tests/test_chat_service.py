from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from google.genai import types

from chat_database import ChatDatabase, PendingAttachment
from chat_service import CHAT_MODEL, GeminiChatService


class ChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = ChatDatabase(":memory:")

    def tearDown(self) -> None:
        self.database.close()

    def test_sends_history_and_reads_attachment_when_tool_is_called(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "notes.txt"
            path.write_bytes(b"attachment contents")
            user_message = self.database.append_user_message(
                "Please read the attached note",
                (
                    PendingAttachment(
                        name=path.name,
                        path=path,
                        attached_at_utc="2026-11-19T00:00:00.000000Z",
                    ),
                ),
            )
        self.database.append_model_message("Earlier answer")
        self.database.append_user_message("What did the note say?")
        attachment_id = user_message.attachments[0].id

        tool_call = types.FunctionCall(
            name="read_attachment",
            args={"attachment_id": attachment_id},
        )
        function_call_content = types.Content(
            role="model",
            parts=[types.Part(function_call=tool_call)],
        )
        models = Mock()
        models.generate_content.side_effect = [
            SimpleNamespace(
                function_calls=[tool_call],
                candidates=[SimpleNamespace(content=function_call_content)],
                text=None,
            ),
            SimpleNamespace(function_calls=[], candidates=[], text="It says hello."),
        ]
        uploaded_file = SimpleNamespace(
            name="files/test-file",
            uri="https://example.test/file",
            mime_type="text/plain",
            state="ACTIVE",
        )
        files = Mock()
        uploaded_payloads = []

        def upload_file(*, file, config):
            uploaded_payloads.append(file.read())
            return uploaded_file

        files.upload.side_effect = upload_file
        client = SimpleNamespace(models=models, files=files)

        answer = GeminiChatService(self.database).generate(client)

        self.assertEqual(answer, "It says hello.")
        self.assertEqual(CHAT_MODEL, "gemini-3.5-flash-lite")
        files.upload.assert_called_once()
        self.assertEqual(uploaded_payloads, [b"attachment contents"])
        files.delete.assert_called_once_with(name="files/test-file")
        self.assertEqual(models.generate_content.call_count, 2)
        first_call = models.generate_content.call_args_list[0].kwargs
        self.assertEqual(first_call["model"], CHAT_MODEL)
        self.assertEqual(
            first_call["config"].thinking_config.thinking_level.name,
            "MINIMAL",
        )
        self.assertIn("Markdown", first_call["config"].system_instruction)
        self.assertIn("jangan menghasilkan HTML", first_call["config"].system_instruction)
        self.assertEqual(
            [message.role for message in first_call["contents"]],
            ["user", "model", "user", "model", "user"],
        )
        function_response = models.generate_content.call_args_list[1].kwargs[
            "contents"
        ][-1].parts[0].function_response
        self.assertEqual(
            function_response.parts[0].file_data.file_uri,
            "https://example.test/file",
        )

    def test_expired_attachment_is_not_uploaded_to_gemini(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "notes.txt"
            path.write_bytes(b"old attachment")
            user_message = self.database.append_user_message(
                "",
                (
                    PendingAttachment(
                        name=path.name,
                        path=path,
                        attached_at_utc="2026-11-18T00:00:00.000000Z",
                    ),
                ),
            )
        expired_at = user_message.attachments[0].sent_at_utc
        from datetime import datetime, timedelta, timezone

        sent_at = datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
        self.database.expire_attachments(
            now=sent_at + timedelta(hours=25)
        )
        tool_call = types.FunctionCall(
            name="read_attachment",
            args={"attachment_id": user_message.attachments[0].id},
        )
        function_call_content = types.Content(
            role="model",
            parts=[types.Part(function_call=tool_call)],
        )
        models = Mock()
        models.generate_content.side_effect = [
            SimpleNamespace(
                function_calls=[tool_call],
                candidates=[SimpleNamespace(content=function_call_content)],
                text=None,
            ),
            SimpleNamespace(function_calls=[], candidates=[], text="The file expired."),
        ]
        files = Mock()

        answer = GeminiChatService(self.database).generate(
            SimpleNamespace(models=models, files=files)
        )

        self.assertEqual(answer, "The file expired.")
        files.upload.assert_not_called()
        function_response = models.generate_content.call_args_list[1].kwargs[
            "contents"
        ][-1].parts[0].function_response
        self.assertIn("error", function_response.response)


if __name__ == "__main__":
    unittest.main()

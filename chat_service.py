from __future__ import annotations

import asyncio
from collections.abc import Callable
from io import BytesIO
import logging
import sys
import time
from typing import Any

from google import genai
from google.genai import types
from PySide6.QtCore import QThread, Signal

from api_keys import run_with_api_key_fallback
from chat_database import (
    ChatDatabase,
    StoredMessage,
    StoredAttachment,
    format_wib_timestamp,
    utc_now_iso,
)


CHAT_MODEL = "gemini-3.5-flash-lite"
MAX_TOOL_CALLS_PER_REQUEST = 10
FILE_PROCESSING_TIMEOUT_SECONDS = 120
LOGGER = logging.getLogger(__name__)


class GeminiChatService:
    def __init__(self, database: ChatDatabase) -> None:
        self._database = database

    def generate(
        self,
        client: genai.Client,
        on_text_chunk: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
        unsaved_message: StoredMessage | None = None,
    ) -> str:
        messages = self._database.load_messages()
        if unsaved_message is not None:
            messages = (*messages, unsaved_message)
        if not messages:
            raise RuntimeError("Tidak ada pesan untuk dikirim ke Gemini.")

        contents: list[types.Content] = []
        for message in messages:
            next_content = self._content_for_message(message)
            if contents and contents[-1].role == next_content.role:
                contents[-1].parts.extend(next_content.parts)
            else:
                contents.append(next_content)
        tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="read_attachment",
                    description=(
                        "Membaca isi sebuah lampiran yang pernah dikirim dalam "
                        "chat ini. Gunakan hanya jika isi lampiran dibutuhkan."
                    ),
                    parameters={
                        "type": "OBJECT",
                        "properties": {
                            "attachment_id": {
                                "type": "INTEGER",
                                "description": (
                                    "ID lampiran yang ditampilkan di riwayat."
                                ),
                            }
                        },
                        "required": ["attachment_id"],
                    },
                )
            ]
        )
        current_utc = utc_now_iso()
        current_wib = format_wib_timestamp(current_utc)
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            system_instruction=(
                "Kamu adalah asisten chat desktop. Jawab dengan bahasa yang "
                "sesuai dengan pengguna. Gunakan format Markdown untuk pemformatan; "
                "jangan menghasilkan HTML atau tag HTML. Timestamp dan waktu pesan yang diberikan "
                "adalah konteks internal untuk memahami waktu relatif. Jangan "
                "menampilkan atau menyebut waktu masuk pesan kecuali pengguna "
                "secara eksplisit meminta waktu tersebut. Waktu sekarang adalah "
                f"{current_wib} WIB (Asia/Jakarta), UTC {current_utc}. "
                "Riwayat pesan lengkap disertakan; jangan mengarang pesan yang "
                "tidak ada. Baca lampiran yang relevan menggunakan tool "
                "read_attachment; jika pesan terbaru hanya berisi lampiran, "
                "baca lampirannya sebelum menjawab."
            ),
            tools=[tool],
        )

        tool_call_count = 0
        uploaded_file_names: list[str] = []
        unsaved_attachments = {
            attachment.id: attachment
            for attachment in (
                unsaved_message.attachments if unsaved_message is not None else ()
            )
            if attachment.data is not None
        }
        try:
            while True:
                streamed_text: list[str] = []
                function_calls: list[types.FunctionCall] = []
                candidate_content: types.Content | None = None
                for response in client.models.generate_content_stream(
                    model=CHAT_MODEL,
                    contents=contents,
                    config=config,
                ):
                    if response.text:
                        streamed_text.append(response.text)
                        if on_text_chunk is not None and not response.function_calls:
                            on_text_chunk(response.text)
                    if response.function_calls:
                        function_calls = response.function_calls
                    if response.candidates:
                        candidate = response.candidates[0]
                        if candidate.content is not None:
                            candidate_content = candidate.content

                if not function_calls:
                    answer = "".join(streamed_text).strip()
                    if not answer:
                        raise RuntimeError("Gemini tidak mengembalikan teks balasan.")
                    return answer

                if on_stream_reset is not None:
                    on_stream_reset()
                tool_call_count += len(function_calls)
                if tool_call_count > MAX_TOOL_CALLS_PER_REQUEST:
                    raise RuntimeError(
                        "Gemini meminta terlalu banyak pembacaan lampiran dalam "
                        "satu permintaan."
                    )

                if candidate_content is None:
                    raise RuntimeError("Gemini mengirim pemanggilan tool tanpa konten.")
                contents.append(candidate_content)
                function_responses = [
                    self._handle_tool_call(
                        function_call,
                        client,
                        uploaded_file_names,
                        unsaved_attachments,
                    )
                    for function_call in function_calls
                ]
                contents.append(types.Content(role="user", parts=function_responses))
        finally:
            for file_name in uploaded_file_names:
                try:
                    client.files.delete(name=file_name)
                except Exception:
                    LOGGER.exception(
                        "Gagal menghapus file sementara yang diunggah ke Gemini: %s",
                        file_name,
                    )

    @staticmethod
    def _content_for_message(message: StoredMessage) -> types.Content:
        parts: list[types.Part] = []
        attachment_lines: list[str] = []
        for attachment in message.attachments:
            status = "tersedia" if attachment.available else "sudah kedaluwarsa"
            attachment_lines.append(
                f"- ID {attachment.id}: {attachment.name} "
                f"({attachment.mime_type}, {status})"
            )
        context = f"Waktu pesan WIB: {message.timestamp}"
        if attachment_lines:
            context += "\nLampiran:\n" + "\n".join(attachment_lines)
        if message.content:
            context += f"\nIsi pesan:\n{message.content}"
        elif not attachment_lines:
            context += "\nPesan ini hanya memiliki lampiran yang sudah kedaluwarsa."
        parts.append(types.Part.from_text(text=context))
        return types.Content(role=message.role, parts=parts)

    def _handle_tool_call(
        self,
        function_call: types.FunctionCall,
        client: genai.Client,
        uploaded_file_names: list[str],
        unsaved_attachments: dict[int, StoredAttachment],
    ) -> types.Part:
        name = function_call.name or ""
        if name != "read_attachment":
            return types.Part.from_function_response(
                name=name or "unknown",
                response={"error": "Tool tidak dikenal."},
            )

        arguments = function_call.args or {}
        attachment_id = arguments.get("attachment_id")
        if isinstance(attachment_id, bool):
            attachment_id = None
        elif isinstance(attachment_id, str) and attachment_id.isdecimal():
            attachment_id = int(attachment_id)
        elif not isinstance(attachment_id, int):
            attachment_id = None
        if attachment_id is None:
            return types.Part.from_function_response(
                name=name,
                response={"error": "ID lampiran tidak valid."},
            )

        attachment = unsaved_attachments.get(
            attachment_id
        ) or self._database.read_active_attachment(attachment_id)
        if attachment is None or attachment.data is None:
            return types.Part.from_function_response(
                name=name,
                response={
                    "error": (
                        "Lampiran tidak ditemukan atau masa simpannya sudah "
                        "berakhir."
                    )
                },
            )
        with BytesIO(attachment.data) as file_data:
            uploaded_file = client.files.upload(
                file=file_data,
                config=types.UploadFileConfig(
                    display_name=attachment.name,
                    mime_type=attachment.mime_type,
                ),
            )
        if not uploaded_file.name or not uploaded_file.uri:
            raise RuntimeError(
                f"Gemini tidak memberikan referensi untuk {attachment.name}."
            )
        uploaded_file_names.append(uploaded_file.name)
        uploaded_file = self._wait_for_file(client, uploaded_file)
        if not uploaded_file.uri:
            raise RuntimeError(
                f"Gemini tidak memberikan URI untuk {attachment.name}."
            )
        return types.Part.from_function_response(
            name=name,
            response={
                "status": "ok",
                "attachment_id": attachment.id,
                "name": attachment.name,
            },
            parts=[
                types.FunctionResponsePart(
                    file_data=types.FunctionResponseFileData(
                        file_uri=uploaded_file.uri,
                        mime_type=uploaded_file.mime_type or attachment.mime_type,
                    )
                )
            ],
        )

    @staticmethod
    def _wait_for_file(
        client: genai.Client,
        uploaded_file: types.File,
    ) -> types.File:
        deadline = time.monotonic() + FILE_PROCESSING_TIMEOUT_SECONDS
        while True:
            state = uploaded_file.state
            state_name = getattr(state, "name", str(state)).upper()
            if state_name in ("ACTIVE", "FILE_STATE_ACTIVE"):
                return uploaded_file
            if state_name in ("FAILED", "FILE_STATE_FAILED"):
                raise RuntimeError("Gemini gagal memproses file lampiran.")
            if state_name not in ("PROCESSING", "FILE_STATE_PROCESSING"):
                return uploaded_file
            if time.monotonic() >= deadline:
                raise TimeoutError("Pemrosesan file di Gemini melewati batas waktu.")
            time.sleep(1)
            uploaded_file = client.files.get(name=uploaded_file.name)


class ChatResponseWorker(QThread):
    reply_received = Signal(str)
    reply_chunk_received = Signal(str)
    stream_reset = Signal()
    error_occurred = Signal(str)

    def __init__(
        self,
        api_keys: tuple[str, ...],
        database: ChatDatabase,
        unsaved_message: StoredMessage | None = None,
    ) -> None:
        super().__init__()
        self._api_keys = api_keys
        self._database = database
        self._unsaved_message = unsaved_message

    def run(self) -> None:
        try:
            response = asyncio.run(self._run_request())
            if response is not None:
                self.reply_received.emit(response)
        except Exception as error:
            self.error_occurred.emit(str(error))

    async def _run_request(self) -> str | None:
        async def attempt(api_key: str) -> str:
            def generate() -> str:
                try:
                    return self._generate_with_key(api_key)
                except Exception:
                    self.stream_reset.emit()
                    raise

            return await asyncio.to_thread(generate)

        def on_fallback(next_key: int, total_keys: int) -> None:
            print(
                f"Chat Gemini gagal - mencoba key {next_key} dari {total_keys}...",
                file=sys.stderr,
            )

        return await run_with_api_key_fallback(
            self._api_keys,
            attempt,
            on_fallback,
            lambda: False,
        )

    def _generate_with_key(self, api_key: str) -> str:
        client = genai.Client(api_key=api_key)
        try:
            return GeminiChatService(self._database).generate(
                client,
                self.reply_chunk_received.emit,
                self.stream_reset.emit,
                self._unsaved_message,
            )
        finally:
            client.close()

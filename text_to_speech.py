from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Signal


SOUND_OF_TEXT_ENDPOINT = "https://api.soundoftext.com/sounds"
SOUND_OF_TEXT_ENGINE = "Google"
INDONESIAN_VOICE = "id-ID"
REQUEST_TIMEOUT_SECONDS = 8
POLL_INTERVAL_MILLISECONDS = 400
MAX_POLL_ATTEMPTS = 40


class _RequestCancelled(Exception):
    pass


class SoundOfTextWorker(QThread):
    audio_ready = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def run(self) -> None:
        try:
            audio_url = self._request_audio_url()
        except _RequestCancelled:
            return
        except (OSError, ValueError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.error_occurred.emit(str(error))
        else:
            if not self.isInterruptionRequested():
                self.audio_ready.emit(audio_url)

    def _request_audio_url(self) -> str:
        if not self._text.strip():
            raise ValueError("Pesan AI kosong.")
        if self.isInterruptionRequested():
            raise _RequestCancelled

        payload = {
            "engine": SOUND_OF_TEXT_ENGINE,
            "data": {
                "text": self._text,
                "voice": INDONESIAN_VOICE,
            },
        }
        response = self._request_json(
            SOUND_OF_TEXT_ENDPOINT,
            method="POST",
            payload=payload,
        )
        audio_url = self._get_audio_url(response)
        if audio_url is not None:
            return self._validate_audio_url(audio_url)

        sound_id = response.get("id")
        if sound_id is None:
            raise RuntimeError(
                self._error_message(response)
                or "Sound of Text tidak mengembalikan ID audio."
            )
        status = str(response.get("status", "")).lower()
        if status in {"failed", "failure", "error"}:
            raise RuntimeError(
                self._error_message(response)
                or "Sound of Text gagal membuat audio."
            )

        result_url = f"{SOUND_OF_TEXT_ENDPOINT}/{quote(str(sound_id), safe='')}"
        for _attempt in range(MAX_POLL_ATTEMPTS):
            self._wait_before_poll()
            response = self._request_json(result_url, method="GET")
            audio_url = self._get_audio_url(response)
            if audio_url is not None:
                return self._validate_audio_url(audio_url)
            status = str(response.get("status", "")).lower()
            if status in {"failed", "failure", "error"}:
                raise RuntimeError(
                    self._error_message(response)
                    or "Sound of Text gagal membuat audio."
                )

        raise TimeoutError("Sound of Text terlalu lama membuat audio.")

    def _wait_before_poll(self) -> None:
        remaining = POLL_INTERVAL_MILLISECONDS
        while remaining > 0:
            if self.isInterruptionRequested():
                raise _RequestCancelled
            interval = min(remaining, 100)
            self.msleep(interval)
            remaining -= interval

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        data = (
            json.dumps(payload).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = ""
            try:
                error_data = json.loads(error.read(4096).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                error_data = {}
            if isinstance(error_data, dict):
                detail = self._error_message(error_data)
            raise RuntimeError(
                f"Sound of Text HTTP {error.code}"
                + (f": {detail}" if detail else "")
            ) from error
        except URLError as error:
            raise RuntimeError(
                f"Sound of Text tidak dapat diakses: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise TimeoutError(
                "Koneksi ke Sound of Text melewati batas waktu."
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Sound of Text mengembalikan respons yang tidak valid."
            ) from error
        if not isinstance(result, dict):
            raise RuntimeError(
                "Sound of Text mengembalikan respons yang tidak valid."
            )
        return result

    @staticmethod
    def _get_audio_url(response: dict[str, object]) -> str | None:
        location = response.get("location") or response.get("url")
        return location if isinstance(location, str) and location else None

    @staticmethod
    def _error_message(response: dict[str, object]) -> str:
        for key in ("message", "error"):
            message = response.get(key)
            if isinstance(message, str) and message:
                return message
        return ""

    @staticmethod
    def _validate_audio_url(audio_url: str) -> str:
        if urlparse(audio_url).scheme != "https":
            raise RuntimeError(
                "Sound of Text mengembalikan URL audio yang tidak aman."
            )
        return audio_url

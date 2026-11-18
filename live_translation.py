import asyncio
import shutil
import sys
import threading

from google import genai
from google.genai import types
from PySide6.QtCore import QThread, Signal

from api_keys import run_with_api_key_fallback
from audio_capture import AudioCaptureError, get_monitor_source


MODEL = "gemini-3.5-live-translate-preview"
SAMPLE_RATE = 16_000
CHUNK_MILLISECONDS = 100
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MILLISECONDS // 1_000


class LiveTranslationWorker(QThread):
    subtitle_received = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, api_keys: tuple[str, ...]) -> None:
        super().__init__()
        self._api_keys = api_keys
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            asyncio.run(self._run_translation())
        except (AudioCaptureError, RuntimeError, OSError) as error:
            self.error_occurred.emit(str(error))
        except Exception as error:
            self.error_occurred.emit(f"Koneksi Gemini gagal: {error}")

    async def _run_translation(self) -> None:
        monitor_source = get_monitor_source()
        if not shutil.which("parec"):
            raise AudioCaptureError(
                "Perintah parec tidak ditemukan. Pasang paket pulseaudio-utils."
            )
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code="id",
                echo_target_language=False,
            ),
            system_instruction=(
                "Terjemahkan ucapan yang terdengar ke Bahasa Indonesia yang alami "
                "dan akurat. Pertahankan maksud, konteks, serta nama diri. "
                "Jangan menambahkan komentar atau mengulang terjemahan yang sudah "
                "ditampilkan; terjemahkan hanya ucapan terbaru."
            ),
        )

        async def attempt(api_key: str) -> None:
            await self._translate_with_key(api_key, monitor_source, config)

        def on_fallback(next_key: int, total_keys: int) -> None:
            print(
                f"API gagal - mencoba key {next_key} dari {total_keys}...",
                file=sys.stderr,
            )

        async def wait_for_stop() -> None:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.1)

        translation_task = asyncio.create_task(
            run_with_api_key_fallback(
                self._api_keys,
                attempt,
                on_fallback,
                self._stop_event.is_set,
            )
        )
        stop_task = asyncio.create_task(wait_for_stop())
        try:
            done, _ = await asyncio.wait(
                {translation_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if translation_task in done:
                await translation_task
            else:
                translation_task.cancel()
                await asyncio.gather(translation_task, return_exceptions=True)
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    async def _translate_with_key(
        self,
        api_key: str,
        monitor_source: str,
        config: types.LiveConnectConfig,
    ) -> None:
        client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta"},
        )
        process: asyncio.subprocess.Process | None = None
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            try:
                process = await asyncio.create_subprocess_exec(
                    "parec",
                    f"--device={monitor_source}",
                    "--raw",
                    "--format=s16le",
                    f"--rate={SAMPLE_RATE}",
                    "--channels=1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise AudioCaptureError(
                    f"Gagal memulai capture audio: {error}"
                ) from error
            try:
                if process.stdout is None or process.stderr is None:
                    raise AudioCaptureError("Tidak bisa membuka stream audio sistem.")

                async def send_audio() -> None:
                    while not self._stop_event.is_set():
                        try:
                            chunk = await process.stdout.readexactly(CHUNK_BYTES)
                        except asyncio.IncompleteReadError as error:
                            if self._stop_event.is_set():
                                return
                            stderr = await process.stderr.read()
                            detail = stderr.decode(errors="replace").strip()
                            raise AudioCaptureError(
                                f"Capture audio berhenti: "
                                f"{detail or 'parec menutup stream.'}"
                            ) from error

                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=chunk,
                                mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                            )
                        )

                async def receive_translation() -> None:
                    async for response in session.receive():
                        content = response.server_content
                        if content and content.output_transcription:
                            text = (content.output_transcription.text or "").strip()
                            if text:
                                self.subtitle_received.emit(text)

                async def wait_for_stop() -> None:
                    while not self._stop_event.is_set():
                        await asyncio.sleep(0.1)

                sender = asyncio.create_task(send_audio())
                receiver = asyncio.create_task(receive_translation())
                stop_waiter = asyncio.create_task(wait_for_stop())
                tasks = {sender, receiver, stop_waiter}
                try:
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    completed_tasks = done - {stop_waiter}
                    for task in completed_tasks:
                        exception = task.exception()
                        if exception:
                            raise exception
                    if stop_waiter not in done:
                        raise RuntimeError("Koneksi terjemahan Gemini terputus.")
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

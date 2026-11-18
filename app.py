import argparse
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from api_keys import parse_api_keys
from live_translation import LiveTranslationWorker
from subtitle_text import merge_transcript_text, recent_wrapped_lines
from system_tray import TranslationTray


MAX_SUBTITLE_LINES = 3
MAX_TRANSCRIPT_CONTEXT_WORDS = 300
SUBTITLE_CLEAR_AFTER_SECONDS = 3.5
SUBTITLE_REFRESH_MILLISECONDS = 30


class SubtitleOverlay(QWidget):
    translation_finished = Signal()
    translation_error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._worker: LiveTranslationWorker | None = None
        self._subtitle_text = ""
        self._last_subtitle_at = 0.0
        self._available_geometry: QRect | None = None
        self._pending_subtitle = ""
        self._build_ui()
        self._configure_screen()

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(SUBTITLE_REFRESH_MILLISECONDS)
        self._render_timer.timeout.connect(self._render_subtitle)

        self._clear_timer = QTimer(self)
        self._clear_timer.timeout.connect(self._clear_stale_subtitle)
        self._clear_timer.start(300)

    def _build_ui(self) -> None:
        self.setWindowTitle("Terjemahan Langsung")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.subtitle_label = QLabel()
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet(
            "QLabel { background-color: rgba(15, 23, 42, 195); color: white; "
            "font-size: 20px; font-weight: 600; padding: 8px 18px; "
            "border-radius: 8px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.subtitle_label)

    def _configure_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        self._available_geometry = screen.availableGeometry()
        width = min(1_200, max(320, int(self._available_geometry.width() * 0.84)))
        height = (
            self.subtitle_label.fontMetrics().lineSpacing() * MAX_SUBTITLE_LINES + 20
        )
        self.subtitle_label.setFixedSize(width, height)
        self.setFixedWidth(width)
        self.setFixedHeight(height)
        self._position_overlay()

    def _position_overlay(self) -> None:
        if self._available_geometry is None:
            return
        available = self._available_geometry
        self.move(
            available.x() + (available.width() - self.width()) // 2,
            available.y() + available.height() - self.height() - 32,
        )

    def start_translation(self) -> bool:
        if self._worker and self._worker.isRunning():
            return True

        api_keys = parse_api_keys(os.environ)
        if not api_keys:
            message = "API key tidak ditemukan. Isi GEMINI_API_KEYS di file .env."
            print(message, file=sys.stderr)
            self.translation_error.emit(message)
            return False

        self._worker = LiveTranslationWorker(api_keys)
        self._worker.subtitle_received.connect(self._show_subtitle)
        self._worker.error_occurred.connect(self._show_error)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        return True

    def stop_translation(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()

    def is_translation_running(self) -> bool:
        return bool(self._worker and self._worker.isRunning())

    def _on_worker_finished(self) -> None:
        self._worker = None
        self.translation_finished.emit()

    def _show_subtitle(self, text: str) -> None:
        self._subtitle_text = merge_transcript_text(self._subtitle_text, text)
        self._subtitle_text = " ".join(
            self._subtitle_text.split()[-MAX_TRANSCRIPT_CONTEXT_WORDS:]
        )
        self._last_subtitle_at = time.monotonic()
        available_width = max(120, self.width() - 40)
        self._pending_subtitle = recent_wrapped_lines(
            self._subtitle_text,
            self.subtitle_label.fontMetrics().horizontalAdvance,
            available_width,
            MAX_SUBTITLE_LINES,
        )
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _render_subtitle(self) -> None:
        if not self._pending_subtitle:
            return
        if self.subtitle_label.text() != self._pending_subtitle:
            self.subtitle_label.setText(self._pending_subtitle)
        if not self.isVisible():
            self.show()
            self.raise_()

    def _clear_stale_subtitle(self) -> None:
        if (
            self._subtitle_text
            and time.monotonic() - self._last_subtitle_at
            > SUBTITLE_CLEAR_AFTER_SECONDS
        ):
            self._subtitle_text = ""
            self._pending_subtitle = ""
            self._render_timer.stop()
            self.subtitle_label.clear()
            self.hide()

    def _show_error(self, message: str) -> None:
        print(f"Terjemahan berhenti: {message}", file=sys.stderr)
        self._subtitle_text = ""
        self._pending_subtitle = ""
        self._render_timer.stop()
        self.subtitle_label.clear()
        self.hide()
        self.translation_error.emit(message)

    def shutdown(self) -> None:
        self._clear_timer.stop()
        self._render_timer.stop()
        if self._worker and self._worker.isRunning():
            worker = self._worker
            worker.stop()
            worker.wait()
            self._worker = None

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()


def main() -> int:
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    parser = argparse.ArgumentParser(description="Subtitle Gemini Live untuk XFCE")
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Jalankan idle di tray; klik ikon untuk menyalakan terjemahan",
    )
    arguments = parser.parse_args()

    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    overlay = SubtitleOverlay()
    tray: TranslationTray | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TranslationTray(app, overlay)
    elif arguments.tray:
        print(
            "System tray tidak tersedia. Aktifkan Notification Area/Status "
            "Notifier di panel XFCE.",
            file=sys.stderr,
        )
        return 2

    app.aboutToQuit.connect(overlay.shutdown)
    if tray:
        app.aboutToQuit.connect(tray.hide)
    else:
        overlay.translation_error.connect(app.quit)

    signal.signal(signal.SIGINT, lambda _signum, _frame: app.quit())
    signal.signal(signal.SIGTERM, lambda _signum, _frame: app.quit())
    signal_timer = QTimer(app)
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(200)
    if not arguments.tray:

        def start_in_foreground_mode() -> None:
            started = overlay.start_translation()
            if tray:
                tray.set_active(started)
            elif not started:
                app.quit()

        QTimer.singleShot(0, start_in_foreground_mode)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

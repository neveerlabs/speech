from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

if TYPE_CHECKING:
    from app import SubtitleOverlay


class TranslationTray(QObject):
    def __init__(self, app: QApplication, overlay: SubtitleOverlay) -> None:
        super().__init__(app)
        self._app = app
        self._overlay = overlay
        self._icon = QSystemTrayIcon(self)
        self._menu = QMenu()

        self._toggle_action = QAction("Aktifkan terjemahan", self._menu)
        self._toggle_action.setCheckable(True)
        self._toggle_action.toggled.connect(self._toggle_translation)
        self._menu.addAction(self._toggle_action)
        self._menu.addSeparator()
        exit_action = QAction("Keluar", self._menu)
        exit_action.triggered.connect(self._app.quit)
        self._menu.addAction(exit_action)

        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._icon_activated)
        self._icon.setIcon(self._theme_icon(False))
        self._icon.setToolTip("Subtitle Gemini - nonaktif")
        self._icon.show()

        self._overlay.translation_finished.connect(self._translation_finished)
        self._overlay.translation_error.connect(self._translation_error)

    def _theme_icon(self, active: bool) -> QIcon:
        theme_name = "audio-volume-high" if active else "audio-volume-muted"
        icon = QIcon.fromTheme(theme_name)
        if icon.isNull():
            standard_icon = (
                QStyle.StandardPixmap.SP_MediaVolume
                if active
                else QStyle.StandardPixmap.SP_MediaVolumeMuted
            )
            icon = self._app.style().standardIcon(standard_icon)
        return icon

    def _icon_activated(
        self, reason: QSystemTrayIcon.ActivationReason
    ) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_action.setChecked(not self._toggle_action.isChecked())

    def _toggle_translation(self, enabled: bool) -> None:
        if enabled:
            if self._overlay.start_translation():
                self.set_active(True)
            else:
                self.set_active(False)
            return

        self._overlay.stop_translation()
        if self._overlay.is_translation_running():
            self._toggle_action.setEnabled(False)
            self._toggle_action.setText("Menghentikan terjemahan...")
            self._icon.setIcon(self._theme_icon(False))
            self._icon.setToolTip("Subtitle Gemini - menghentikan")
        else:
            self.set_active(False)

    def set_active(self, active: bool) -> None:
        signals_blocked = self._toggle_action.blockSignals(True)
        self._toggle_action.setChecked(active)
        self._toggle_action.setEnabled(True)
        self._toggle_action.setText(
            "Matikan terjemahan" if active else "Aktifkan terjemahan"
        )
        self._toggle_action.blockSignals(signals_blocked)
        self._icon.setIcon(self._theme_icon(active))
        self._icon.setToolTip(
            "Subtitle Gemini - aktif" if active else "Subtitle Gemini - nonaktif"
        )

    def _translation_finished(self) -> None:
        self.set_active(False)

    def _translation_error(self, message: str) -> None:
        self._icon.showMessage(
            "Subtitle Gemini",
            message,
            QSystemTrayIcon.MessageIcon.Warning,
            6_000,
        )
        self._toggle_action.blockSignals(True)
        self._toggle_action.setChecked(False)
        self._toggle_action.setEnabled(False)
        self._toggle_action.setText("Menghentikan terjemahan...")
        self._toggle_action.blockSignals(False)
        self._icon.setIcon(self._theme_icon(False))
        self._icon.setToolTip("Subtitle Gemini - menghentikan")

    def hide(self) -> None:
        self._icon.hide()

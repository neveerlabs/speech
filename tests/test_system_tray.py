import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QLoggingCategory, QObject, Signal
from PySide6.QtWidgets import QApplication

import app
from system_tray import TranslationTray


class FakeOverlay(QObject):
    translation_finished = Signal()
    translation_error = Signal(str)

    def start_translation(self) -> bool:
        return True

    def stop_translation(self) -> None:
        self.translation_finished.emit()

    def is_translation_running(self) -> bool:
        return False


class FakeChatPanel(QObject):
    visibility_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.visible = False

    def isVisible(self) -> bool:
        return self.visible

    def show_panel(self) -> None:
        self.visible = True
        self.visibility_changed.emit(True)

    def hide(self) -> None:
        self.visible = False
        self.visibility_changed.emit(False)


class TranslationTrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.panel = FakeChatPanel()
        self.tray = TranslationTray(self.app, FakeOverlay(), self.panel)

    def tearDown(self) -> None:
        self.tray.hide()

    def test_tooltip_shows_only_subtitle_status(self) -> None:
        self.assertEqual(self.tray._icon.toolTip(), "Subtitle - nonaktif")

        self.tray.set_active(True)

        self.assertEqual(self.tray._icon.toolTip(), "Subtitle - aktif")

    def test_close_menu_action_is_labeled_close(self) -> None:
        action_labels = [action.text() for action in self.tray._menu.actions()]

        self.assertIn("close", action_labels)

    def test_atspi_accessibility_log_noise_is_filtered(self) -> None:
        category = QLoggingCategory("qt.accessibility.atspi")

        self.assertTrue(callable(app.main))
        self.assertFalse(category.isWarningEnabled())
        self.assertTrue(category.isCriticalEnabled())

    def test_panel_action_opens_the_chat_panel(self) -> None:
        self.tray._panel_action.trigger()

        self.assertTrue(self.panel.visible)
        self.assertEqual(self.tray._panel_action.text(), "Tutup panel")

    def test_panel_action_closes_visible_panel(self) -> None:
        self.tray._panel_action.trigger()
        self.tray._panel_action.trigger()

        self.assertFalse(self.panel.visible)
        self.assertEqual(self.tray._panel_action.text(), "Tampilkan panel")


if __name__ == "__main__":
    unittest.main()

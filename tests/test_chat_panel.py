import os
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QSize, Qt, QUrl
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QImage,
    QPixmap,
    QWheelEvent,
    QTextFormat,
    QTextTable,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QWidget,
)

from chat_attachments import copy_attachment
from chat_database import ChatDatabase, PendingAttachment, utc_now_iso
from chat_panel import (
    MAX_ATTACHMENT_SIZE_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    _AttachedFile,
    _AttachmentPreviewItem,
    _UserMessageRow,
    _WrappedMessageLabel,
    ChatPanel,
)


class _FakeSignal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self._callbacks:
            callback(*args)


class _FakeChatWorker:
    def __init__(self, _api_keys, _database, unsaved_message=None) -> None:
        self.reply_received = _FakeSignal()
        self.reply_chunk_received = _FakeSignal()
        self.stream_reset = _FakeSignal()
        self.error_occurred = _FakeSignal()
        self.finished = _FakeSignal()
        self.unsaved_message = unsaved_message
        self.started = False

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return False

    def deleteLater(self) -> None:
        pass


class _FakeSpeechWorker:
    instances = []

    def __init__(self, text: str) -> None:
        self.text = text
        self.audio_ready = _FakeSignal()
        self.error_occurred = _FakeSignal()
        self.finished = _FakeSignal()
        self.running = False
        self.interruption_requested = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.running = True

    def isRunning(self) -> bool:
        return self.running

    def requestInterruption(self) -> None:
        self.interruption_requested = True

    def wait(self) -> bool:
        self.running = False
        return True

    def deleteLater(self) -> None:
        pass

    def complete(self, audio_url: str) -> None:
        self.audio_ready.emit(audio_url)
        self.running = False
        self.finished.emit()


class ChatPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.database = ChatDatabase(":memory:")
        self.panel = ChatPanel(database=self.database)
        _FakeSpeechWorker.instances.clear()

    def tearDown(self) -> None:
        self.panel.hide()
        self.panel.shutdown()
        self.database.close()

    def _select_file_from_attachment_menu(self) -> None:
        self.panel._attach_button.click()
        self.panel._file_action.trigger()

    def test_panel_is_taller_than_it_is_wide(self) -> None:
        self.assertGreater(self.panel.height(), self.panel.width())

    def test_close_button_hides_panel(self) -> None:
        self.panel.show_panel()
        self.assertTrue(self.panel.isVisible())

        self.panel._close_button.click()

        self.assertFalse(self.panel.isVisible())

    def test_long_error_toast_is_compact_and_keeps_gemini_status(self) -> None:
        self.panel._show_error_toast(
            "Chat Gemini gagal",
            "503 UNAVAILABLE.\n" + ("response detail " * 40),
        )

        toast_text = self.panel._copy_toast.text()
        self.assertLessEqual(len(toast_text), 180)
        self.assertIn("503 UNAVAILABLE", toast_text)
        self.assertTrue(toast_text.endswith("..."))

    def test_close_icon_is_large_inside_a_standard_round_hover_target(self) -> None:
        button = self.panel._close_button
        self.assertEqual((button.width(), button.height()), (28, 28))
        self.assertEqual(button.iconSize().width(), 20)
        self.assertFalse(button.icon().isNull())
        style = button.styleSheet()
        self.assertIn("border: none", style)
        self.assertIn("border-radius: 14px", style)
        self.assertIn("QToolButton:hover", style)

    def test_header_notification_button_toggles_popup(self) -> None:
        self.panel.show_panel()
        self.assertEqual(
            self.panel._notification_button.accessibleName(),
            "Notifikasi",
        )
        self.assertEqual(
            self.panel._settings_button.accessibleName(),
            "Pengaturan",
        )
        self.assertEqual(
            self.panel._github_button.accessibleName(),
            "GitHub",
        )
        self.assertFalse(self.panel._notification_button.icon().isNull())
        self.assertFalse(self.panel._settings_button.icon().isNull())
        self.assertFalse(self.panel._github_button.icon().isNull())
        self.assertLess(
            self.panel._notification_button.x(),
            self.panel._github_button.x(),
        )
        self.assertLess(
            self.panel._github_button.x(),
            self.panel._settings_button.x(),
        )
        self.assertLess(
            self.panel._settings_button.x(),
            self.panel._close_button.x(),
        )

        self.panel._notification_button.click()
        self.assertTrue(self.panel._notification_popup.isVisible())
        self.panel._notification_button.click()
        self.assertFalse(self.panel._notification_popup.isVisible())

        self.panel._settings_button.click()
        self.panel._github_button.click()

        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.database.load_messages(), ())

    def test_errors_are_cached_and_notification_detail_can_be_copied(self) -> None:
        self.panel.show_panel()
        self.panel._show_error_toast(
            "Chat Gemini gagal",
            "503 UNAVAILABLE: temporary network failure",
        )
        self.panel._show_error_toast(
            "Gagal menyimpan pesan",
            "database is locked",
        )

        self.assertEqual(len(self.panel._notifications), 2)
        self.assertEqual(self.panel._notification_badge.text(), "2")
        self.assertRegex(
            self.panel._notification_rows[0]._date_label.text(),
            r"\d{2}/\d{2}/\d{4}, \d{2}:\d{2}",
        )
        self.panel._notification_button.click()
        self.assertTrue(self.panel._notification_popup.isVisible())
        self.assertEqual(len(self.panel._notification_rows), 2)
        self.assertTrue(self.panel._notification_list_divider.isVisible())
        self.assertEqual(self.panel._notification_list_divider.height(), 1)
        self.assertEqual(self.panel._notification_list_layout.spacing(), 2)
        self.assertLessEqual(self.panel._notification_popup.height(), 140)
        self.assertEqual(self.panel._notification_rows[0].height(), 46)
        header_title = self.panel._notification_list_page.findChild(
            QLabel,
            "notificationsTitle",
        )
        self.assertEqual(
            header_title.mapTo(self.panel._notification_popup, QPoint(0, 0)).x(),
            self.panel._notification_rows[0]
            ._title_label.mapTo(self.panel._notification_popup, QPoint(0, 0))
            .x(),
        )
        self.assertEqual(
            header_title.mapTo(self.panel._notification_popup, QPoint(0, 0)).x(),
            4,
        )

        notification = self.panel._notifications[0]
        self.panel._notification_rows[0].activated.emit()

        self.assertTrue(notification.is_read)
        self.assertEqual(self.panel._notification_badge.text(), "1")
        self.assertIs(
            self.panel._notification_stack.currentWidget(),
            self.panel._notification_detail_page,
        )
        self.assertTrue(self.panel._notification_detail_divider.isVisible())
        self.assertEqual(self.panel._notification_detail_layout.spacing(), 3)
        self.assertEqual(
            self.panel._notification_back_button.mapTo(
                self.panel._notification_popup,
                QPoint(0, 0),
            ).x(),
            2,
        )
        self.assertEqual(
            self.panel._notification_detail_text.toPlainText(),
            notification.details,
        )
        self.assertEqual(
            self.panel._notification_detail_text.lineWrapMode(),
            QTextEdit.LineWrapMode.NoWrap,
        )
        self.assertEqual(
            self.panel._notification_detail_text.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertEqual(
            self.panel._notification_detail_text.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        copy_icon_key = self.panel._notification_copy_button.icon().cacheKey()
        self.panel._notification_copy_button.click()
        self.assertEqual(
            QApplication.clipboard().text(),
            notification.details,
        )
        self.assertEqual(self.panel._copy_toast.height(), 24)
        expected_toast_width = (
            self.panel._copy_toast.fontMetrics().horizontalAdvance("Error copied")
            + 20
        )
        self.assertEqual(self.panel._copy_toast.width(), expected_toast_width)
        self.assertFalse(self.panel._notification_copy_button.icon().isNull())
        self.assertNotEqual(
            self.panel._notification_copy_button.icon().cacheKey(),
            copy_icon_key,
        )
        self.panel._notification_copy_reset_timer.stop()
        self.panel._reset_notification_copy_button()
        self.assertEqual(
            self.panel._notification_copy_button.accessibleName(),
            "Salin detail error",
        )

        self.panel._notification_back_button.click()
        self.panel._mark_all_notifications_button.click()
        self.assertFalse(self.panel._notification_badge.isVisible())
        self.panel._clear_notifications_button.click()
        self.assertEqual(self.panel._notifications, [])
        self.assertEqual(self.panel._notification_rows, [])
        empty_label = self.panel._notification_list_content.findChild(
            QLabel,
            "emptyNotificationsLabel",
        )
        self.assertEqual(empty_label.text(), "Tidak ada notifikasi")
        self.assertGreaterEqual(empty_label.minimumHeight(), 48)
        self.assertGreaterEqual(self.panel._notification_popup.height(), 96)

    def test_clicking_outside_notification_popup_closes_it(self) -> None:
        self.panel.show_panel()
        self.panel._show_error_toast("Chat Gemini gagal", "Unavailable")
        self.panel._notification_button.click()
        self.assertTrue(self.panel._notification_popup.isVisible())

        QTest.mouseClick(self.panel._input, Qt.MouseButton.LeftButton)

        self.assertFalse(self.panel._notification_popup.isVisible())

        self.panel._notification_button.click()
        position = QPoint(5, 5)
        global_position = self.panel._input.mapToGlobal(position)
        wheel_event = QWheelEvent(
            QPointF(position),
            QPointF(global_position),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        self.panel._notification_dismiss_filter.eventFilter(
            self.panel._input,
            wheel_event,
        )

        self.assertFalse(self.panel._notification_popup.isVisible())

    def test_panel_can_be_dragged_from_the_header(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()
        initial_position = self.panel.pos()

        QTest.mousePress(
            self.panel._header,
            Qt.MouseButton.LeftButton,
            pos=QPoint(20, 15),
        )
        QTest.mouseMove(self.panel._header, QPoint(110, 20), delay=10)
        QTest.mouseRelease(
            self.panel._header,
            Qt.MouseButton.LeftButton,
            pos=QPoint(110, 20),
        )

        self.assertNotEqual(self.panel.pos(), initial_position)

    def test_header_border_reaches_both_panel_edges(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()

        self.assertEqual(self.panel._header.y(), 0)
        self.assertLessEqual(self.panel._header.height(), 34)
        self.assertEqual(self.panel._header.layout().spacing(), 2)
        self.assertEqual(
            self.panel._header.findChild(QLabel).text(),
            "Chat Speech AI",
        )
        input_left = self.panel._input_frame.mapTo(
            self.panel,
            QPoint(0, 0),
        ).x()
        input_right = input_left + self.panel._input_frame.width()
        title = self.panel._header.findChild(QLabel)
        self.assertEqual(title.mapTo(self.panel, QPoint(0, 0)).x(), input_left)
        self.assertEqual(
            self.panel._close_button.mapTo(
                self.panel,
                QPoint(self.panel._close_button.width(), 0),
            ).x(),
            input_right,
        )
        image = QImage(self.panel.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        self.panel.render(image)
        border_y = self.panel._header.height() - 1
        self.assertEqual(
            image.pixelColor(2, border_y),
            QColor("#3a3f46"),
        )
        self.assertEqual(
            image.pixelColor(self.panel.width() - 3, border_y),
            QColor("#3a3f46"),
        )
        self.assertEqual(
            image.pixelColor(self.panel.width() // 2, 1),
            QColor("#24272d"),
        )

    def test_panel_position_snaps_inside_available_desktop_area(self) -> None:
        screen = self.app.primaryScreen()
        self.assertIsNotNone(screen)
        geometry = screen.availableGeometry()
        self.panel.move(geometry.topLeft() + QPoint(8, 12))

        self.panel._snap_to_available_geometry()

        self.assertEqual(self.panel.pos(), geometry.topLeft())
        position = self.panel._constrained_position(
            QPoint(geometry.right() + 500, geometry.bottom() + 500)
        )
        self.assertGreaterEqual(position.x(), geometry.left())
        self.assertGreaterEqual(position.y(), geometry.top())
        self.assertLessEqual(
            position.x() + self.panel.width() - 1,
            geometry.right(),
        )
        self.assertLessEqual(
            position.y() + self.panel.height() - 1,
            geometry.bottom(),
        )

    def test_clicking_and_dragging_the_body_does_not_move_the_panel(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()
        initial_position = self.panel.pos()
        viewport = self.panel._scroll_area.viewport()

        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(viewport, QPoint(70, 60), delay=10)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=QPoint(70, 60))

        self.assertEqual(self.panel.pos(), initial_position)

    def test_text_selection_does_not_move_the_panel(self) -> None:
        self.panel.show_panel()
        self.panel._input.setPlainText("select this text")
        self.app.processEvents()
        initial_position = self.panel.pos()
        self.panel._input.setFocus()
        QTest.keyClick(
            self.panel._input,
            Qt.Key.Key_End,
            Qt.KeyboardModifier.ShiftModifier,
        )

        self.assertEqual(self.panel.pos(), initial_position)
        self.assertEqual(self.panel._input.textCursor().selectedText(), "select this text")

    def test_submit_button_looks_disabled_until_text_is_entered(self) -> None:
        self.assertFalse(self.panel._submit_button.isEnabled())

        self.panel._input.setPlainText("test")

        self.assertTrue(self.panel._submit_button.isEnabled())

    def test_attachment_menu_rotates_and_lists_the_requested_actions(self) -> None:
        self.panel.show_panel()
        self.panel._attach_button.click()
        self.app.processEvents()

        self.assertTrue(self.panel._attachment_menu.isVisible())
        self.assertEqual(
            [action.text() for action in self.panel._attachment_menu.actions()],
            ["Camera", "Screenshot", "Photo", "File"],
        )
        QTest.qWait(220)
        self.assertAlmostEqual(self.panel._attach_button.rotation, 45.0, delta=1)

        self.panel._attachment_menu.hide()
        QTest.qWait(220)
        self.assertAlmostEqual(self.panel._attach_button.rotation, 0.0, delta=1)

    def test_photo_menu_uses_a_photo_and_video_filter(self) -> None:
        self.panel.show_panel()
        with patch(
            "chat_panel.QFileDialog.getOpenFileNames",
            return_value=([], ""),
        ) as file_dialog:
            self.panel._attach_button.click()
            self.panel._media_action.trigger()

        file_dialog.assert_called_once()
        self.assertIn("*.jpg", file_dialog.call_args.args[3])
        self.assertIn("*.mp4", file_dialog.call_args.args[3])
        self.assertEqual(self.panel._attachments, [])

    def test_enter_submits_message(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Send with Enter")
        self.panel._input.setFocus()

        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            QTest.keyClick(self.panel._input, Qt.Key.Key_Return)

            self.assertTrue(self.panel._is_sending)
            self.assertEqual(
                [
                    (message.role, message.content)
                    for message in self.database.load_messages()
                ],
                [("user", "Send with Enter")],
            )

    def test_shift_enter_inserts_newline_without_submitting(self) -> None:
        self.panel._input.setPlainText("First line")
        cursor = self.panel._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.panel._input.setTextCursor(cursor)
        self.panel._input.setFocus()

        QTest.keyClick(
            self.panel._input,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ShiftModifier,
        )

        self.assertEqual(self.panel._input.toPlainText(), "First line\n")
        self.assertEqual(self.database.load_messages(), ())
        self.assertFalse(self.panel._is_sending)

    def test_sending_message_persists_history_and_displays_gemini_reply(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Hello Gemini")
        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            self.panel._submit_button.click()
            worker = self.panel._chat_worker
            self.assertIsNotNone(worker)
            self.assertTrue(worker.started)
            self.assertTrue(self.panel._is_sending)

            worker.reply_received.emit("Hello from Gemini")
            worker.finished.emit()

        messages = self.database.load_messages()
        self.assertEqual(
            [(message.role, message.content) for message in messages],
            [
                ("user", "Hello Gemini"),
                ("model", "Hello from Gemini"),
            ],
        )
        self.assertEqual(self.panel._message_layout.count(), 2)
        self.assertFalse(self.panel._is_sending)
        model_row = self.panel._message_layout.itemAt(1).widget()
        self.assertIsNone(model_row.findChild(QWidget, "modelMessageBubble"))
        self.assertIsNotNone(model_row.findChild(QTextBrowser, "messageContent"))

    def test_database_save_failure_still_sends_unsaved_message_to_gemini(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Keep sending despite database failure")
        with (
            patch.object(
                self.database,
                "append_user_message",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("chat_panel.ChatResponseWorker", _FakeChatWorker),
        ):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            self.assertTrue(worker.started)
            self.assertEqual(
                worker.unsaved_message.content,
                "Keep sending despite database failure",
            )
            self.assertEqual(self.database.load_messages(), ())
            self.assertIn("database is locked", self.panel._copy_toast.text())

            worker.error_occurred.emit("Gemini unavailable")
            worker.finished.emit()

        self.assertEqual(self.database.load_messages(), ())
        user_row = self.panel._message_layout.itemAt(0).widget()
        self.assertIsInstance(user_row, _UserMessageRow)
        self.assertFalse(user_row._retry_button.isHidden())
        self.assertIn("Gemini unavailable", self.panel._copy_toast.text())

    def test_database_save_failure_then_gemini_success_persists_exchange(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Persist after Gemini replies")
        with (
            patch.object(
                self.database,
                "append_user_message",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("chat_panel.ChatResponseWorker", _FakeChatWorker),
        ):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            self.assertEqual(
                worker.unsaved_message.content,
                "Persist after Gemini replies",
            )
            worker.reply_received.emit("Gemini reply")
            worker.finished.emit()

        self.assertEqual(
            [(message.role, message.content) for message in self.database.load_messages()],
            [
                ("user", "Persist after Gemini replies"),
                ("model", "Gemini reply"),
            ],
        )

    def test_gemini_failure_rolls_back_user_and_retry_saves_successful_exchange(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Retry this turn")
        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            self.assertEqual(
                [message.content for message in self.database.load_messages()],
                ["Retry this turn"],
            )

            worker.error_occurred.emit("temporary network error")
            worker.finished.emit()

            self.assertEqual(self.database.load_messages(), ())
            user_row = self.panel._message_layout.itemAt(0).widget()
            self.assertFalse(user_row._retry_button.isHidden())
            user_row._retry_button.click()

            retry_worker = self.panel._chat_worker
            self.assertTrue(retry_worker.started)
            retry_worker.reply_received.emit("Recovered reply")
            retry_worker.finished.emit()

        self.assertEqual(
            [(message.role, message.content) for message in self.database.load_messages()],
            [("user", "Retry this turn"), ("model", "Recovered reply")],
        )
        self.assertTrue(user_row._retry_button.isHidden())

    def test_retry_icon_sits_left_and_bottom_aligned_to_failed_user_bubble(self) -> None:
        self.panel.show_panel()
        self.panel._add_message_row(
            self.database.append_user_message("A message that needs retry")
        )
        self.app.processEvents()
        user_row = self.panel._message_layout.itemAt(0).widget()
        user_row.set_retry_visible(True)
        self.app.processEvents()

        self.assertLess(user_row._retry_button.x(), user_row._bubble.x())
        self.assertLessEqual(
            user_row._bubble.x()
            - user_row._retry_button.x()
            - user_row._retry_button.width(),
            3,
        )
        self.assertEqual(
            user_row._retry_button.y() + user_row._retry_button.height(),
            user_row._bubble.y() + user_row._bubble.height(),
        )

    def test_database_reply_save_retry_does_not_call_gemini_again(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Save the existing reply")
        original_save_reply = self.database.append_model_message
        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            with patch.object(
                self.database,
                "append_model_message",
                side_effect=sqlite3.OperationalError("disk is full"),
            ):
                worker.reply_received.emit("Already generated reply")
                worker.finished.emit()

            user_row = self.panel._message_layout.itemAt(0).widget()
            self.assertFalse(user_row._retry_button.isHidden())
            self.assertIn("disk is full", self.panel._copy_toast.text())
            user_row._retry_button.click()

        self.assertEqual(
            [(message.role, message.content) for message in self.database.load_messages()],
            [("user", "Save the existing reply"), ("model", "Already generated reply")],
        )
        self.assertTrue(user_row._retry_button.isHidden())
        self.assertEqual(self.panel._chat_worker, None)

    def test_database_exchange_retry_saves_existing_gemini_reply(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Save both messages")
        with (
            patch.object(
                self.database,
                "append_user_message",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("chat_panel.ChatResponseWorker", _FakeChatWorker),
        ):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            with patch.object(
                self.database,
                "append_exchange",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                worker.reply_received.emit("Keep this generated reply")
                worker.finished.emit()

            user_row = self.panel._message_layout.itemAt(0).widget()
            self.assertFalse(user_row._retry_button.isHidden())
            self.assertEqual(self.database.load_messages(), ())
            user_row._retry_button.click()

        self.assertEqual(
            [(message.role, message.content) for message in self.database.load_messages()],
            [("user", "Save both messages"), ("model", "Keep this generated reply")],
        )
        self.assertTrue(user_row._retry_button.isHidden())
        self.assertEqual(self.panel._chat_worker, None)

    def test_streaming_reply_reveals_characters_and_persists_one_final_message(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Hello Gemini")
        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            self.assertIsNotNone(worker)
            streamed_text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            worker.reply_chunk_received.emit(streamed_text)

            model_row = self.panel._message_layout.itemAt(1).widget()
            content_label = model_row.findChild(QTextBrowser, "messageContent")
            self.assertEqual(content_label.toPlainText(), "")
            self.panel._display_next_reply_character()
            self.assertEqual(content_label.toPlainText(), streamed_text[:32])
            self.panel._display_next_reply_character()
            self.assertEqual(content_label.toPlainText(), streamed_text)

            worker.reply_received.emit(streamed_text)
            worker.finished.emit()

        self.assertEqual(
            [(message.role, message.content) for message in self.database.load_messages()],
            [("user", "Hello Gemini"), ("model", streamed_text)],
        )
        self.assertEqual(content_label.text(), streamed_text)
        self.assertFalse(self.panel._is_sending)

    def test_streaming_reply_renders_markdown_before_it_finishes(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Hello Gemini")
        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            self.assertIsNotNone(worker)
            streamed_text = "# Live heading\n**bold**"
            worker.reply_chunk_received.emit(streamed_text)
            self.panel._display_next_reply_character()

            model_row = self.panel._message_layout.itemAt(1).widget()
            content_label = model_row.findChild(QTextBrowser, "messageContent")
            self.assertEqual(content_label.toPlainText(), "Live heading\nbold")
            self.assertEqual(
                content_label.document().begin().blockFormat().headingLevel(),
                1,
            )
            bold_block = content_label.document().begin().next()
            self.assertGreaterEqual(
                bold_block.begin().fragment().charFormat().fontWeight(),
                QFont.Weight.DemiBold,
            )

            worker.reply_received.emit(streamed_text)
            worker.finished.emit()

    def test_stream_reset_discards_partial_text_before_retry(self) -> None:
        self.panel._api_keys = ("test-key",)
        self.panel._input.setPlainText("Hello Gemini")
        with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
            self.panel._submit_message()
            worker = self.panel._chat_worker
            self.assertIsNotNone(worker)
            worker.reply_chunk_received.emit("bad")
            self.panel._display_next_reply_character()
            self.panel._display_next_reply_character()

            worker.stream_reset.emit()

            model_row = self.panel._message_layout.itemAt(1).widget()
            content_label = model_row.findChild(QTextBrowser, "messageContent")
            self.assertEqual(content_label.toPlainText(), "")
            worker.reply_chunk_received.emit("good")
            for _ in range(4):
                self.panel._display_next_reply_character()
            worker.reply_received.emit("good")
            worker.finished.emit()

        self.assertEqual(
            [message.content for message in self.database.load_messages()],
            ["Hello Gemini", "good"],
        )

    def test_model_message_uses_available_row_width_without_a_bubble(self) -> None:
        message = self.database.append_model_message(
            "This response should use the conversation width."
        )
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()

        model_row = self.panel._message_layout.itemAt(0).widget()
        content_label = model_row.findChild(QTextBrowser, "messageContent")
        self.assertIsNotNone(content_label)
        self.assertIsNone(model_row.findChild(QWidget, "modelMessageBubble"))
        self.assertGreater(content_label.width(), 360)
        self.assertEqual(model_row._actions.y(), content_label.geometry().bottom() + 4)
        self.assertGreater(model_row.height(), content_label.height())

    def test_long_model_message_wraps_to_its_compact_required_height(self) -> None:
        message = self.database.append_model_message(
            "This longer assistant response should wrap at the conversation width. "
            * 5
        )
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()

        model_row = self.panel._message_layout.itemAt(0).widget()
        content_label = model_row.findChild(QTextBrowser, "messageContent")
        self.assertIsNotNone(content_label)
        self.assertGreater(
            content_label.height(),
            content_label.fontMetrics().lineSpacing(),
        )
        self.assertEqual(
            model_row.height(),
            content_label.height() + model_row.layout().spacing() + model_row._actions.height(),
        )

    def test_model_message_copy_works_and_actions_keep_requested_order(self) -> None:
        message = self.database.append_model_message("A response")
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()

        model_row = self.panel._message_layout.itemAt(0).widget()
        self.assertEqual(
            [button.objectName() for button in model_row._left_actions],
            [
                "modelMessageCopyButton",
                "modelMessageLikeButton",
                "modelMessageDislikeButton",
                "modelMessageSpeechButton",
            ],
        )
        self.assertEqual(
            model_row._regenerate_button.objectName(),
            "modelMessageRegenerateButton",
        )
        self.assertGreater(
            model_row._regenerate_button.x(),
            model_row._speech_button.x(),
        )
        self.assertTrue(
            all(
                not button.toolTip()
                for button in model_row._left_actions
                + (model_row._regenerate_button,)
            )
        )
        self.assertTrue(
            all(button.iconSize().width() > 16 for button in model_row._left_actions)
        )

        for button in model_row._left_actions[1:] + (
            model_row._regenerate_button,
        ):
            button.click()
        self.assertEqual(
            [(stored.role, stored.content) for stored in self.database.load_messages()],
            [("model", "A response")],
        )
        model_row._copy_button.click()
        self.assertEqual(QApplication.clipboard().text(), "A response")
        self.assertEqual(model_row._copy_icon_state, "check")
        self.assertEqual(self.panel._copy_toast.text(), "Message copied")
        self.assertTrue(self.panel._copy_toast.isVisible())
        model_row._copy_feedback_timer.timeout.emit()
        self.assertEqual(model_row._copy_icon_state, "copy")

    def test_like_dislike_buttons_toggle_visual_state_without_saving(self) -> None:
        message = self.database.append_model_message("A response")
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()
        row = self.panel._message_layout.itemAt(0).widget()
        icon_size = QSize(24, 24)
        like_icon = row._like_button.icon().pixmap(icon_size).toImage()
        dislike_icon = row._dislike_button.icon().pixmap(icon_size).toImage()
        like_style = row._like_button.styleSheet()
        dislike_style = row._dislike_button.styleSheet()

        def opaque_bounds(image) -> tuple[int, int, int, int]:
            points = [
                (x, y)
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            ]
            return (
                min(x for x, _ in points),
                min(y for _, y in points),
                max(x for x, _ in points),
                max(y for _, y in points),
            )

        stored_before = [
            (stored.role, stored.content)
            for stored in self.database.load_messages()
        ]

        row._like_button.click()

        self.assertTrue(row._like_button.isChecked())
        self.assertFalse(row._dislike_button.isChecked())
        active_like_icon = row._like_button.icon().pixmap(icon_size).toImage()
        self.assertNotEqual(
            active_like_icon,
            like_icon,
        )
        self.assertEqual(opaque_bounds(active_like_icon), opaque_bounds(like_icon))
        self.assertEqual(row._like_button.styleSheet(), like_style)

        row._dislike_button.click()
        self.assertFalse(row._like_button.isChecked())
        self.assertTrue(row._dislike_button.isChecked())
        self.assertEqual(
            row._like_button.icon().pixmap(icon_size).toImage(),
            like_icon,
        )
        active_dislike_icon = row._dislike_button.icon().pixmap(icon_size).toImage()
        self.assertNotEqual(active_dislike_icon, dislike_icon)
        self.assertEqual(
            opaque_bounds(active_dislike_icon),
            opaque_bounds(dislike_icon),
        )
        self.assertEqual(row._dislike_button.styleSheet(), dislike_style)

        row._dislike_button.click()
        self.assertFalse(row._dislike_button.isChecked())
        self.assertEqual(
            row._dislike_button.icon().pixmap(icon_size).toImage(),
            dislike_icon,
        )
        self.assertEqual(
            [
                (stored.role, stored.content)
                for stored in self.database.load_messages()
            ],
            stored_before,
        )

    def test_model_speech_button_requests_audio_and_stops_playback(self) -> None:
        message = self.database.append_model_message("Hello **there**.")
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()
        row = self.panel._message_layout.itemAt(0).widget()
        player = self.panel._speech_player

        with (
            patch("chat_panel.SoundOfTextWorker", _FakeSpeechWorker),
            patch.object(player, "setSource") as set_source,
            patch.object(player, "play") as play,
            patch.object(player, "stop") as stop,
        ):
            row._speech_button.click()
            worker = _FakeSpeechWorker.instances[-1]
            self.assertEqual(worker.text, "Hello there.")
            self.assertEqual(row._speech_state, "loading")

            audio_url = "https://cdn.example.test/audio.mp3"
            worker.complete(audio_url)
            set_source.assert_any_call(QUrl(audio_url))
            play.assert_called_once_with()

            self.panel._handle_speech_playback_state(
                QMediaPlayer.PlaybackState.PlayingState
            )
            self.assertEqual(row._speech_state, "playing")
            self.assertEqual(
                row._speech_button.accessibleName(),
                "Hentikan pembacaan",
            )

            row._speech_button.click()

        self.assertEqual(row._speech_state, "idle")
        stop.assert_called_once_with()
        set_source.assert_called_with(QUrl())

    def test_clicking_speech_while_loading_cancels_the_request(self) -> None:
        message = self.database.append_model_message("A response")
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()
        row = self.panel._message_layout.itemAt(0).widget()

        with (
            patch("chat_panel.SoundOfTextWorker", _FakeSpeechWorker),
            patch.object(self.panel._speech_player, "stop"),
            patch.object(self.panel._speech_player, "setSource"),
        ):
            row._speech_button.click()
            worker = _FakeSpeechWorker.instances[-1]
            row._speech_button.click()

        self.assertTrue(worker.interruption_requested)
        self.assertEqual(row._speech_state, "idle")

    def test_speech_request_switches_rows_and_cancels_pending_row(self) -> None:
        first_message = self.database.append_model_message("First response")
        second_message = self.database.append_model_message("Second response")
        self.panel.show_panel()
        self.panel._add_message_row(first_message)
        self.panel._add_message_row(second_message)
        self.app.processEvents()
        first_row = self.panel._message_layout.itemAt(0).widget()
        second_row = self.panel._message_layout.itemAt(1).widget()

        with (
            patch("chat_panel.SoundOfTextWorker", _FakeSpeechWorker),
            patch.object(self.panel._speech_player, "stop"),
            patch.object(self.panel._speech_player, "setSource"),
        ):
            first_row._speech_button.click()
            worker = _FakeSpeechWorker.instances[-1]
            second_row._speech_button.click()

            self.assertTrue(worker.interruption_requested)
            self.assertIs(self.panel._pending_speech_row, second_row)
            self.assertEqual(first_row._speech_state, "idle")
            self.assertEqual(second_row._speech_state, "loading")

            second_row._speech_button.click()
            self.assertIsNone(self.panel._pending_speech_row)
            self.assertEqual(second_row._speech_state, "idle")
            second_row._speech_button.click()
            self.assertIs(self.panel._pending_speech_row, second_row)
            self.assertEqual(second_row._speech_state, "loading")
            self.panel.hide()
            self.assertIsNone(self.panel._pending_speech_row)
            self.assertEqual(second_row._speech_state, "idle")

        self.assertIsNone(self.panel._pending_speech_row)
        self.assertEqual(second_row._speech_state, "idle")

    def test_speech_text_renders_markdown_and_includes_code_block_content(self) -> None:
        message = self.database.append_model_message(
            "Use **this** command:\n\n```bash\nprintf hello\n```"
        )
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()
        row = self.panel._message_layout.itemAt(0).widget()

        text = row.speech_text()

        self.assertIn("Use this command:", text)
        self.assertIn("printf hello", text)
        self.assertNotIn("SPEECHCODEBLOCK", text)

    def test_scroll_to_latest_button_tracks_scroll_position(self) -> None:
        self.panel.show_panel()
        self.panel._add_message_row(
            self.database.append_model_message(
                "A long response that creates enough content to scroll. " * 120
            )
        )
        self.app.processEvents()
        QTest.qWait(50)
        self.app.processEvents()
        scrollbar = self.panel._scroll_area.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)

        scrollbar.setValue(0)
        self.app.processEvents()

        button = self.panel._scroll_to_latest_button
        self.assertTrue(button.isVisible())
        input_top = self.panel._input_frame.mapTo(
            self.panel,
            QPoint(0, 0),
        ).y()
        input_right = (
            self.panel._input_frame.mapTo(
                self.panel,
                QPoint(0, 0),
            ).x()
            + self.panel._input_frame.width()
        )
        self.assertLess(button.y() + button.height(), input_top)
        self.assertEqual(button.x() + button.width(), input_right)
        button.click()
        self.app.processEvents()
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        self.assertFalse(button.isVisible())

    def test_panel_opens_at_latest_message(self) -> None:
        for index in range(40):
            self.panel._add_message_row(
                self.database.append_model_message(f"Message {index}")
            )

        self.panel.show_panel()
        self.app.processEvents()
        QTest.qWait(50)
        self.app.processEvents()

        scrollbar = self.panel._scroll_area.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertEqual(scrollbar.value(), scrollbar.maximum())

    def test_latest_message_scrolls_above_the_overlaid_composer(self) -> None:
        for index in range(36):
            message = self.database.append_model_message(f"Message {index}")
            self.panel._add_message_row(message)

        self.panel.show_panel()
        QTest.qWait(50)
        self.app.processEvents()
        self.panel._scroll_to_bottom()
        self.app.processEvents()

        last_row = self.panel._message_layout.itemAt(
            self.panel._message_layout.count() - 1
        ).widget()
        last_row_bottom = last_row.mapTo(
            self.panel,
            QPoint(0, last_row.height()),
        ).y()
        composer_top = self.panel._input_frame.mapTo(
            self.panel,
            QPoint(0, 0),
        ).y()
        self.assertLessEqual(last_row_bottom, composer_top - 4)
        self.assertGreater(
            last_row.mapTo(self.panel, QPoint(0, 0)).y(),
            self.panel._header.height(),
        )

    def test_chat_controls_and_attachment_tiles_have_no_tooltips(self) -> None:
        self.panel.show_panel()
        self.panel._add_message_row(
            self.database.append_user_message("User message")
        )
        self.panel._add_message_row(
            self.database.append_model_message("Model response")
        )
        self.app.processEvents()

        self.assertTrue(
            all(
                not button.toolTip()
                for button in self.panel.findChildren(QToolButton)
            )
        )

    def test_user_bubble_width_tracks_its_text_and_stays_right_aligned(self) -> None:
        short_message = self.database.append_user_message("Hai")
        long_message = self.database.append_user_message(
            "A longer message that should wrap inside the maximum bubble width. " * 4
        )
        model_message = self.database.append_model_message(
            "The response can use the whole row up to the user's right edge."
        )
        self.panel.show_panel()
        self.panel._add_message_row(short_message)
        self.panel._add_message_row(long_message)
        self.panel._add_message_row(model_message)
        self.app.processEvents()

        short_row = self.panel._message_layout.itemAt(0).widget()
        long_row = self.panel._message_layout.itemAt(1).widget()
        model_row = self.panel._message_layout.itemAt(2).widget()
        short_bubble = short_row._bubble
        long_bubble = long_row._bubble
        model_label = model_row.findChild(QTextBrowser, "messageContent")

        self.assertGreater(short_row.width(), short_bubble.width())
        self.assertLess(short_bubble.width(), 72)
        self.assertLess(short_bubble.width(), 360)
        prefix_width = QFontMetrics(
            long_bubble._label.font()
        ).horizontalAdvance("Ada ")
        self.assertEqual(long_bubble.width(), long_row.width() - prefix_width)
        self.assertEqual(long_bubble.x(), prefix_width)
        self.assertGreater(long_bubble.width(), short_bubble.width())
        self.assertGreater(long_bubble.height(), short_bubble.height())
        self.assertEqual(
            short_bubble.x() + short_bubble.width(),
            short_row.width(),
        )
        self.assertEqual(model_label.width(), model_row.width())
        self.assertEqual(
            model_row.width(),
            long_bubble.x() + long_bubble.width(),
        )
        self.assertEqual(
            self.panel._scroll_area.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertLessEqual(
            long_row.width(),
            self.panel._scroll_area.viewport().width(),
        )
        self.assertEqual(self.panel._message_layout.spacing(), 4)

    def test_chat_messages_render_markdown(self) -> None:
        markdown = (
            "# Heading\n\n"
            "**Bold**, *italic*, ~~strike~~, and `inline code`.\n\n"
            "- First item\n  - Nested item\n\n"
            "> A quotation\n\n"
            "```bash\necho 'Hello'\n```\n\n"
            "[Link](https://example.com)\n\n"
            "| Name | Status |\n| --- | --- |\n| Item | Done |\n\n"
            "- [x] Completed\n\n---"
        )
        message = self.database.append_user_message(markdown)
        model_message = self.database.append_model_message(markdown)
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.panel._add_message_row(model_message)
        self.app.processEvents()

        user_row = self.panel._message_layout.itemAt(0).widget()
        model_row = self.panel._message_layout.itemAt(1).widget()
        user_label = user_row._bubble._label
        model_label = model_row.findChild(QTextBrowser, "messageContent")

        self.assertEqual(
            user_label.textFormat(),
            Qt.TextFormat.MarkdownText,
        )
        self.assertIsNotNone(model_label)
        self.assertEqual(model_label.textFormat(), Qt.TextFormat.MarkdownText)
        self.assertEqual(
            user_label.document().toHtml(),
            model_label.document().toHtml(),
        )
        self.assertEqual(len(user_label._code_widgets), 1)
        self.assertEqual(len(model_label._code_widgets), 1)
        self.assertTrue(
            user_label.textInteractionFlags()
            & Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.assertTrue(
            model_label.textInteractionFlags()
            & Qt.TextInteractionFlag.LinksAccessibleByMouse
        )

    def test_markdown_uses_compact_lists_and_github_style_blocks(self) -> None:
        markdown = (
            "# Heading\n\n"
            "1. First item\n   1. Nested item\n2. Second item\n\n"
            "> A quote\n\n"
            "Inline `code`, **bold**, ~~strikethrough~~ with "
            "[a link](https://example.com)\n\n"
            "```python\nprint('hello')\n```\n\n"
            "| Name | Value |\n| --- | --- |\n| item | 1 |\n\n"
            "- [ ] Task\n- [x] Done\n"
        )
        view = _WrappedMessageLabel(markdown, self.panel)
        document = view.document()
        html = document.toHtml().lower()
        block = document.begin()
        first_list_block = None
        nested_list_block = None
        heading_block = None
        quote_block = None
        inline_code_is_styled = False
        bold_is_demibold = False
        link_is_styled = False
        while block.isValid():
            if block.blockFormat().headingLevel():
                heading_block = block
            if block.blockFormat().property(
                QTextFormat.Property.BlockQuoteLevel
            ):
                quote_block = block
            if block.textList() is not None:
                if block.text() == "First item":
                    first_list_block = block
                elif block.text() == "Nested item":
                    nested_list_block = block
            fragment_iterator = block.begin()
            while not fragment_iterator.atEnd():
                fragment = fragment_iterator.fragment()
                if fragment.isValid():
                    char_format = fragment.charFormat()
                    if fragment.text() == "code":
                        inline_code_is_styled = (
                            char_format.fontFixedPitch()
                            and char_format.background().color()
                            == QColor("#2d333b")
                        )
                    if fragment.text() == "bold":
                        bold_is_demibold = (
                            char_format.fontWeight() == QFont.Weight.DemiBold
                        )
                    if char_format.isAnchor():
                        link_is_styled = (
                            char_format.foreground().color()
                            == QColor("#58a6ff")
                        )
                fragment_iterator += 1
            block = block.next()

        self.assertIsNotNone(first_list_block)
        self.assertIsNotNone(nested_list_block)
        self.assertIsNotNone(heading_block)
        self.assertIsNotNone(quote_block)
        self.assertEqual(first_list_block.textList().format().indent(), 0)
        self.assertEqual(first_list_block.blockFormat().leftMargin(), 12)
        self.assertEqual(nested_list_block.textList().format().indent(), 0)
        self.assertEqual(nested_list_block.blockFormat().leftMargin(), 28)
        self.assertEqual(
            quote_block.blockFormat().background().color(),
            QColor("#262b33"),
        )
        self.assertTrue(inline_code_is_styled)
        self.assertTrue(bold_is_demibold)
        self.assertTrue(link_is_styled)
        self.assertGreater(
            heading_block.begin().fragment().charFormat().fontPointSize(),
            view.font().pointSizeF(),
        )
        self.assertIn("<h1", html)
        self.assertIn("<ol", html)
        self.assertEqual(len(view._code_widgets), 1)
        code_block = next(iter(view._code_widgets.values()))
        self.assertEqual(code_block._editor.toPlainText(), "print('hello')")
        fixed_font = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont
        )
        self.assertEqual(code_block._editor.font().family(), fixed_font.family())
        self.assertIn("<table", html)
        table = next(
            (
                frame
                for frame in document.rootFrame().childFrames()
                if isinstance(frame, QTextTable)
            ),
            None,
        )
        self.assertIsNotNone(table)
        self.assertEqual(table.format().border(), 1)
        self.assertEqual(table.format().cellPadding(), 5)
        self.assertIn('href="https://example.com"', html)
        self.assertIn('class="unchecked"', html)
        self.assertIn("blockquote", document.defaultStyleSheet())

    def test_code_blocks_limit_lines_scroll_copy_and_preserve_following_text(
        self,
    ) -> None:
        source_lines = [f"line_{index} = {index}" for index in range(18)]
        source_lines.append('long_line = "' + "x" * 200 + '"')
        source = "\n".join(source_lines)
        markdown = (
            f"Before code\n\n```python\n{source}\n```\n\n"
            "After fenced block"
        )
        view = _WrappedMessageLabel(markdown, None)
        view.resize(360, 400)
        view.show()
        self.app.processEvents()

        code_block = next(iter(view._code_widgets.values()))
        editor = code_block._editor
        line_height = editor.fontMetrics().lineSpacing()
        self.assertEqual(editor.toPlainText(), source)
        self.assertEqual(code_block.findChildren(QLabel)[0].text(), "python")
        self.assertGreater(editor.verticalScrollBar().maximum(), 0)
        self.assertGreater(editor.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(editor.viewport().height(), line_height * 10)
        self.assertTrue(code_block._copy_button.isVisible())
        self.assertIn("border: none", code_block._copy_button.styleSheet())
        self.assertNotIn("1px solid", code_block._copy_button.styleSheet())
        editor.verticalScrollBar().setValue(editor.verticalScrollBar().maximum())
        editor.horizontalScrollBar().setValue(
            editor.horizontalScrollBar().maximum()
        )
        self.assertGreater(editor.firstVisibleBlock().blockNumber(), 0)
        self.assertGreater(editor.horizontalScrollBar().value(), 0)

        after_block = view.document().begin()
        while after_block.isValid() and after_block.text() != "After fenced block":
            after_block = after_block.next()
        self.assertTrue(after_block.isValid())
        after_y = view.document().documentLayout().blockBoundingRect(after_block).y()
        self.assertGreaterEqual(
            after_y,
            code_block.y() + code_block.height(),
        )

        initial_y = code_block.y()
        view.resize(360, 150)
        self.app.processEvents()
        view.verticalScrollBar().setValue(10)
        self.app.processEvents()
        self.assertEqual(code_block.y(), initial_y - 10)
        view.verticalScrollBar().setValue(0)
        view.resize(360, 400)
        self.app.processEvents()

        QTest.mouseMove(view, QPoint(view.width() - 2, view.height() - 2))
        QTest.qWait(80)
        self.assertTrue(code_block._copy_button.isVisible())
        QTest.mouseMove(code_block, QPoint(60, 40))
        QTest.qWait(20)
        self.assertTrue(code_block._copy_button.isVisible())
        QTest.mouseClick(code_block._copy_button, Qt.MouseButton.LeftButton)
        self.assertEqual(QApplication.clipboard().text(), source)
        view.close()

    def test_code_blocks_use_language_aware_syntax_colors(self) -> None:
        python_view = _WrappedMessageLabel(
            "```python\n# 🙂\ndef greet():\n    return 'hello'\n```",
            None,
        )
        bash_view = _WrappedMessageLabel(
            '```bash\necho "$HOME"\n```',
            None,
        )
        python_view.show()
        bash_view.show()
        self.app.processEvents()
        python_editor = next(iter(python_view._code_widgets.values()))._editor
        bash_editor = next(iter(bash_view._code_widgets.values()))._editor

        def color_at(editor, text: str) -> QColor:
            cursor = editor.document().find(text)
            self.assertFalse(cursor.isNull())
            return cursor.charFormat().foreground().color()

        self.assertEqual(color_at(python_editor, "def"), QColor("#ff7b72"))
        self.assertEqual(color_at(python_editor, "greet"), QColor("#d2a8ff"))
        self.assertEqual(color_at(python_editor, "'hello'"), QColor("#a5d6ff"))
        self.assertEqual(python_editor.verticalScrollBar().maximum(), 0)
        self.assertEqual(color_at(bash_editor, '"$HOME"'), QColor("#a5d6ff"))
        self.assertNotEqual(
            color_at(bash_editor, "echo"),
            QColor("#e6edf3"),
        )

        capped_view = _WrappedMessageLabel(
            "```python\n"
            + "\n".join(f"line_{index}" for index in range(11))
            + "\n```",
            None,
        )
        capped_view.show()
        self.app.processEvents()
        capped_editor = next(iter(capped_view._code_widgets.values()))._editor
        self.assertGreater(capped_editor.verticalScrollBar().maximum(), 0)
        self.assertEqual(capped_editor.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(
            capped_editor.viewport().height(),
            capped_editor.fontMetrics().lineSpacing() * 10,
        )
        long_view = _WrappedMessageLabel(
            "```python\n"
            + "\n".join("print(1)" for _ in range(1000))
            + "\n```",
            None,
        )
        long_view.show()
        self.app.processEvents()
        long_editor = next(iter(long_view._code_widgets.values()))._editor
        expected_gutter_width = (
            long_editor.fontMetrics().horizontalAdvance("9999") + 4
        )
        self.assertEqual(long_editor.blockCount(), 1000)
        self.assertEqual(long_editor.line_number_area_width(), expected_gutter_width)
        self.assertEqual(
            long_editor._line_number_area.width(),
            expected_gutter_width,
        )
        python_view.close()
        bash_view.close()
        capped_view.close()
        long_view.close()

    def test_copying_code_shows_toast_and_temporarily_changes_icon(self) -> None:
        message = self.database.append_model_message(
            "```bash\necho 'Code copied'\n```"
        )
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()
        row = self.panel._message_layout.itemAt(0).widget()
        content = row.findChild(QTextBrowser, "messageContent")
        code_block = next(iter(content._code_widgets.values()))

        code_block._copy_button.click()

        self.assertEqual(QApplication.clipboard().text(), "echo 'Code copied'")
        self.assertEqual(code_block._copy_icon_state, "check")
        self.assertTrue(code_block._copy_feedback_timer.isActive())
        self.assertTrue(self.panel._copy_toast.isVisible())
        self.assertEqual(self.panel._copy_toast.text(), "Code copied")
        self.assertEqual(code_block._copy_feedback_timer.interval(), 1600)
        code_block._copy_feedback_timer.timeout.emit()
        self.assertEqual(code_block._copy_icon_state, "copy")

        user_message = self.database.append_user_message(
            "```python\nprint('user code')\n```"
        )
        self.panel._add_message_row(user_message)
        self.app.processEvents()
        user_row = self.panel._message_layout.itemAt(1).widget()
        user_code_block = next(
            iter(user_row._bubble._label._code_widgets.values())
        )
        user_code_block._copy_button.click()
        self.assertEqual(
            QApplication.clipboard().text(),
            "print('user code')",
        )
        self.assertEqual(self.panel._copy_toast.text(), "Code copied")

    def test_long_model_reply_is_fully_stored_and_auto_scrolled_into_view(self) -> None:
        content = "Pembahasan lengkap:\n\n" + "\n".join(
            f"{index}. **Bagian {index}:** " + ("penjelasan panjang " * 8)
            for index in range(1, 41)
        ).rstrip()
        message = self.database.append_model_message(content)
        self.panel.show_panel()
        self.panel._add_message_row(message)
        for _ in range(5):
            QTest.qWait(20)
            self.app.processEvents()

        model_row = self.panel._message_layout.itemAt(0).widget()
        content_label = model_row.findChild(QTextBrowser, "messageContent")
        scrollbar = self.panel._scroll_area.verticalScrollBar()

        self.assertIsNotNone(content_label)
        self.assertEqual(self.database.get_message(message.id).content, content)
        self.assertEqual(content_label.text(), content)
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertEqual(scrollbar.value(), scrollbar.maximum())

    def test_composer_backdrop_starts_at_frame_midpoint(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()

        self.assertIn("background: transparent", self.panel._footer.styleSheet())
        self.assertEqual(
            self.panel._scroll_area.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        scroll_style = self.panel._scroll_area.styleSheet()
        self.assertIn("QScrollBar::up-arrow:vertical", scroll_style)
        self.assertIn("height: 0", scroll_style)
        input_top = self.panel._input_frame.mapTo(
            self.panel,
            QPoint(0, 0),
        ).y()
        input_left = self.panel._input_frame.mapTo(
            self.panel,
            QPoint(0, 0),
        ).x()
        input_middle_y = input_top + self.panel._input_frame.height() // 2
        input_bottom_y = input_top + self.panel._input_frame.height()
        bottom_gap_y = self.panel.height() - 5
        viewport = self.panel._scroll_area.viewport()
        viewport_top_left = viewport.mapTo(self.panel, QPoint(0, 0))
        corner_probe = QWidget(viewport)
        corner_probe.setGeometry(
            input_left + 8 - viewport_top_left.x(),
            input_top + 2 - viewport_top_left.y(),
            3,
            3,
        )
        corner_probe.setStyleSheet("background-color: #ff00ff;")
        corner_probe.show()
        self.app.processEvents()
        rendered_panel = self.panel.grab().toImage()
        self.assertGreater(
            self.panel._body_stack.geometry().bottom(),
            self.panel._footer.y(),
        )
        self.assertEqual(self.panel._composer_backdrop.y(), input_middle_y)
        self.assertEqual(
            rendered_panel.pixelColor(input_left + 8, input_top + 2),
            QColor("#ff00ff"),
        )
        self.assertEqual(
            rendered_panel.pixelColor(2, input_top - 1),
            QColor("#202327"),
        )
        self.assertEqual(
            rendered_panel.pixelColor(2, input_middle_y - 1),
            QColor("#202327"),
        )
        self.assertEqual(
            rendered_panel.pixelColor(2, input_middle_y + 1),
            QColor("#24272d"),
        )
        self.assertEqual(
            rendered_panel.pixelColor(self.panel.width() // 2, input_bottom_y + 1),
            QColor("#24272d"),
        )
        self.assertEqual(
            rendered_panel.pixelColor(self.panel.width() // 2, bottom_gap_y),
            QColor("#24272d"),
        )

    def test_user_message_actions_copy_and_show_toast_only(self) -> None:
        message = self.database.append_user_message("Copy this message")
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.app.processEvents()
        row = self.panel._message_layout.itemAt(0).widget()
        row._set_hovered(True)
        QTest.mouseMove(row._copy_button, QPoint(10, 10))
        self.app.processEvents()
        hovered_copy_icon = row._copy_button.icon().pixmap(16, 16).toImage()
        normal_copy_icon = row._action_icon("copy").pixmap(16, 16).toImage()
        self.assertEqual(hovered_copy_icon, normal_copy_icon)
        self.assertNotIn(":hover", row._copy_button.styleSheet())

        row._delete_button.click()
        row._edit_button.click()
        self.assertEqual(
            [(stored.role, stored.content) for stored in self.database.load_messages()],
            [("user", "Copy this message")],
        )

        row._copy_button.click()

        self.assertEqual(QApplication.clipboard().text(), "Copy this message")
        self.assertTrue(self.panel._copy_toast.isVisible())
        self.assertEqual(self.panel._copy_toast.text(), "Message copied")
        self.assertEqual(self.panel._copy_toast_timer.interval(), 2200)
        self.assertEqual(row._copy_feedback_timer.interval(), 2200)

    def test_sent_attachment_moves_from_cache_into_database(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "notes.txt"
            source.write_bytes(b"persist this attachment")
            self.panel._attachment_directory = root / "draft-cache"
            self.panel._api_keys = ("test-key",)
            self.panel._copy_local_attachments([source])
            self.panel._update_attachment_preview()
            cached_path = self.panel._attachments[0].stored_path
            with patch("chat_panel.ChatResponseWorker", _FakeChatWorker):
                self.panel._submit_message()

            self.assertTrue(cached_path.exists())
            message = self.database.load_messages()[0]
            self.assertEqual(message.content, "")
            self.assertEqual(message.attachments[0].name, "notes.txt")
            self.assertEqual(
                self.database.read_active_attachment(message.attachments[0].id).data,
                b"persist this attachment",
            )

    def test_sent_media_and_files_render_in_a_grid_and_media_opens_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "photo.png"
            image = QImage(96, 64, QImage.Format.Format_ARGB32)
            image.fill(QColor("#7799bb"))
            self.assertTrue(image.save(str(image_path), "PNG"))
            file_path = root / "notes.txt"
            file_path.write_text("attachment content")
            message = self.database.append_user_message(
                "Lihat lampiran ini",
                (
                    PendingAttachment(image_path.name, image_path, utc_now_iso()),
                    PendingAttachment(file_path.name, file_path, utc_now_iso()),
                ),
            )
            self.panel.show_panel()
            self.panel._add_message_row(message)
            self.app.processEvents()

            row = self.panel._message_layout.itemAt(0).widget()
            grid = row.findChild(QWidget, "messageAttachmentGrid")
            media_tile = row.findChild(QWidget, "messageMediaAttachment")
            file_tile = row.findChild(QWidget, "messageFileAttachment")

            self.assertIsNotNone(grid)
            self.assertIsNotNone(row._bubble)
            self.assertLess(grid.y() + grid.height(), row._bubble.y())
            self.assertGreaterEqual(
                row._actions.y(),
                row._bubble.y() + row._bubble.height(),
            )
            self.assertIsNotNone(media_tile)
            self.assertFalse(media_tile._thumbnail.isNull())
            self.assertIsNotNone(file_tile)
            self.assertEqual(file_tile.toolTip(), "")
            self.assertEqual(grid.layout().count(), 2)

            QTest.mouseClick(media_tile, Qt.MouseButton.LeftButton)
            self.app.processEvents()

            self.assertIs(
                self.panel._body_stack.currentWidget(),
                self.panel._media_preview_page,
            )
            self.assertFalse(self.panel._media_preview_page._surface.image.isNull())

    def test_expired_sent_attachments_render_warning_tiles(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "old-photo.png"
            image = QImage(64, 48, QImage.Format.Format_ARGB32)
            image.fill(QColor("#7799bb"))
            self.assertTrue(image.save(str(image_path), "PNG"))
            message = self.database.append_user_message(
                "",
                (
                    PendingAttachment(image_path.name, image_path, utc_now_iso()),
                ),
            )
            sent_at = datetime.fromisoformat(
                message.attachments[0].sent_at_utc.replace("Z", "+00:00")
            )
            self.database.expire_attachments(
                now=sent_at + timedelta(hours=25)
            )
            expired_message = self.database.get_message(message.id)
            self.panel.show_panel()
            self.panel._add_message_row(expired_message)
            self.app.processEvents()

            tile = self.panel._message_layout.itemAt(0).widget().findChild(
                QWidget,
                "messageMediaAttachment",
            )
            self.assertIsNotNone(tile)
            self.assertTrue(tile._attachment.expired)
            self.assertEqual(tile.toolTip(), "")

    def test_user_bubble_height_is_independent_of_attachment_grid(self) -> None:
        text_only = self.database.append_user_message("Teks singkat")
        with TemporaryDirectory() as temporary_directory:
            file_path = Path(temporary_directory) / "notes.txt"
            file_path.write_text("attachment content")
            with_attachment = self.database.append_user_message(
                "Teks singkat",
                (
                    PendingAttachment(file_path.name, file_path, utc_now_iso()),
                ),
            )
            self.panel.show_panel()
            self.panel._add_message_row(text_only)
            self.panel._add_message_row(with_attachment)
            self.app.processEvents()

            first_row = self.panel._message_layout.itemAt(0).widget()
            second_row = self.panel._message_layout.itemAt(1).widget()

            self.assertEqual(first_row._bubble.height(), second_row._bubble.height())
            self.assertIsNotNone(
                second_row.findChild(QWidget, "messageAttachmentGrid")
            )

    def test_chat_body_starts_without_sample_messages(self) -> None:
        self.assertEqual(self.panel._message_layout.count(), 0)

    def test_selected_file_is_copied_and_shown_in_the_composer(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "notes.txt"
            source.write_text("attachment content")
            storage = root / "project" / "attachments"
            self.panel._attachment_directory = storage
            self.panel.show_panel()
            self.app.processEvents()

            with patch(
                "chat_panel.QFileDialog.getOpenFileNames",
                return_value=([str(source)], ""),
            ):
                self._select_file_from_attachment_menu()

            stored_path = self.panel._attachments[0].stored_path
            self.assertTrue(stored_path.is_file())
            self.assertNotEqual(stored_path, source)
            self.assertEqual(stored_path.read_text(), "attachment content")
            self.assertTrue(self.panel._attachment_preview.isVisible())
            self.assertEqual(self.panel._attachment_preview.toolTip(), "")
            preview_card = self.panel._attachment_preview.findChild(
                QWidget,
                "fileAttachmentPreview",
            )
            self.assertIsNotNone(preview_card)
            self.assertEqual(self.panel._input.toPlainText(), "")
            self.assertEqual(self.panel._message_layout.count(), 0)
            self.assertEqual(
                self.panel._input_frame.height(),
                self.panel._input.height() + 132,
            )

    def test_pasting_clipboard_image_attaches_a_png(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            self.panel._attachment_directory = Path(temporary_directory) / "attachments"
            image = QImage(32, 24, QImage.Format.Format_ARGB32)
            image.fill(QColor("#7799bb"))
            QApplication.clipboard().setImage(image)

            self.panel._input.setFocus()
            QTest.keyClick(
                self.panel._input,
                Qt.Key.Key_V,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.app.processEvents()
            QApplication.clipboard().clear()

            self.assertEqual(len(self.panel._attachments), 1)
            attachment = self.panel._attachments[0]
            self.assertEqual(attachment.name, "pasted-image.png")
            self.assertEqual(attachment.media_type, "image")
            self.assertTrue(attachment.stored_path.is_file())
            stored_image = QImage(str(attachment.stored_path))
            self.assertEqual(stored_image.size(), image.size())
            self.assertEqual(self.panel._input.toPlainText(), "")

    def test_attachment_count_is_capped_across_input_methods(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sources = []
            for index in range(MAX_ATTACHMENTS_PER_MESSAGE + 1):
                source = root / f"note-{index}.txt"
                source.write_text(f"note {index}")
                sources.append(str(source))
            self.panel._attachment_directory = root / "attachments"
            image = QImage(8, 8, QImage.Format.Format_ARGB32)
            image.fill(QColor("#7799bb"))

            with (
                patch(
                    "chat_panel.QFileDialog.getOpenFileNames",
                    return_value=(sources, ""),
                ),
                patch("chat_panel.QMessageBox.warning") as warning,
            ):
                self.panel._select_attachments()
                self.panel._attach_pasted_image(image)

            self.assertEqual(len(self.panel._attachments), 10)
            self.assertEqual(
                len(list(self.panel._attachment_directory.iterdir())),
                10,
            )
            self.assertEqual(warning.call_count, 2)

    def test_identical_attachment_contents_are_skipped(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "resume.txt"
            second = root / "renamed-resume.txt"
            first.write_bytes(b"same attachment content")
            second.write_bytes(first.read_bytes())
            self.panel._attachment_directory = root / "attachments"

            with patch("chat_panel.QMessageBox.warning") as warning:
                changed = self.panel._copy_local_attachments([first, second])

            self.assertTrue(changed)
            self.assertEqual(len(self.panel._attachments), 1)
            self.assertEqual(self.panel._attachments[0].name, first.name)
            self.assertEqual(
                len(list(self.panel._attachment_directory.iterdir())),
                1,
            )
            self.assertIn("duplikat", warning.call_args.args[2])

    def test_pasted_image_duplicate_is_skipped(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            self.panel._attachment_directory = Path(temporary_directory) / "attachments"
            image = QImage(32, 24, QImage.Format.Format_ARGB32)
            image.fill(QColor("#7799bb"))

            with patch("chat_panel.QMessageBox.information") as information:
                self.assertTrue(self.panel._store_image_attachment(image, "image.png"))
                self.assertFalse(self.panel._store_image_attachment(image, "image.png"))

            self.assertEqual(len(self.panel._attachments), 1)
            self.assertEqual(len(list(self.panel._attachment_directory.iterdir())), 1)
            information.assert_called_once()

    def test_file_over_18_mb_is_rejected_without_copying(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "too-large.bin"
            with source.open("wb") as attachment_file:
                attachment_file.truncate(MAX_ATTACHMENT_SIZE_BYTES + 1)
            self.panel._attachment_directory = root / "attachments"

            with patch("chat_panel.QMessageBox.warning") as warning:
                self.panel._copy_local_attachments([source])

            self.assertEqual(self.panel._attachments, [])
            self.assertFalse(self.panel._attachment_directory.exists())
            warning.assert_called_once()

    def test_pasted_image_over_18_mb_is_rejected_and_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            self.panel._attachment_directory = Path(temporary_directory) / "attachments"
            width, height = 2_600, 2_000
            image_data = os.urandom(width * height * 4)
            image = QImage(
                image_data,
                width,
                height,
                width * 4,
                QImage.Format.Format_ARGB32,
            )

            with patch("chat_panel.QMessageBox.warning") as warning:
                self.panel._attach_pasted_image(image)

            self.assertEqual(self.panel._attachments, [])
            self.assertEqual(list(self.panel._attachment_directory.iterdir()), [])
            warning.assert_called_once()

    def test_dropping_a_local_file_into_chat_attaches_a_copy(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "dropped.png"
            image = QImage(40, 20, QImage.Format.Format_ARGB32)
            image.fill(QColor("#7799bb"))
            self.assertTrue(image.save(str(source), "PNG"))
            self.panel._attachment_directory = root / "attachments"
            self.panel.show_panel()
            self.app.processEvents()

            mime_data = QMimeData()
            mime_data.setUrls([QUrl.fromLocalFile(str(source))])
            viewport = self.panel._scroll_area.viewport()
            drag_event = QDragEnterEvent(
                QPoint(10, 10),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(viewport, drag_event)
            self.assertTrue(drag_event.isAccepted())
            self.assertTrue(self.panel._drop_overlay.isVisible())

            drop_event = QDropEvent(
                QPointF(10, 10),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(self.panel._drop_overlay, drop_event)

            self.assertTrue(drop_event.isAccepted())
            self.assertFalse(self.panel._drop_overlay.isVisible())
            self.assertEqual(len(self.panel._attachments), 1)
            attachment = self.panel._attachments[0]
            self.assertEqual(attachment.name, source.name)
            self.assertEqual(attachment.media_type, "image")
            self.assertTrue(attachment.stored_path.is_file())
            self.assertNotEqual(attachment.stored_path, source)

    def test_drag_leave_hides_drop_overlay(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile("/tmp/example.txt")])
        drag_event = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(self.panel._scroll_area.viewport(), drag_event)
        self.assertTrue(self.panel._drop_overlay.isVisible())

        QApplication.sendEvent(
            self.panel._drop_overlay,
            QDragLeaveEvent(),
        )
        QTest.qWait(120)

        self.assertFalse(self.panel._drop_overlay.isVisible())

    def test_media_previews_are_tiles_and_files_are_wide_cards(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            photo = root / "photo.png"
            photo.write_bytes(b"not an image")
            notes = root / "notes.txt"
            notes.write_text("attachment content")
            video = root / "clip.mp4"
            video.write_bytes(b"video content")
            self.panel._attachment_directory = root / "attachments"
            self.panel.show_panel()
            self.app.processEvents()

            with patch(
                "chat_panel.QFileDialog.getOpenFileNames",
                return_value=([str(photo), str(notes), str(video)], ""),
            ):
                self._select_file_from_attachment_menu()
            self.app.processEvents()

            media_tiles = self.panel._attachment_preview.findChildren(
                QWidget,
                "mediaAttachmentPreview",
            )
            file_cards = self.panel._attachment_preview.findChildren(
                QWidget,
                "fileAttachmentPreview",
            )

            self.assertEqual([item.media_type for item in self.panel._attachments],
                             ["image", None, "video"])
            self.assertEqual(len(media_tiles), 2)
            self.assertTrue(all(tile.size().width() == 72 for tile in media_tiles))
            self.assertEqual(len(file_cards), 1)
            self.assertGreater(file_cards[0].width(), 72)
            self.assertEqual(
                len(
                    {
                        item.geometry().center().y()
                        for item in media_tiles + file_cards
                    }
                ),
                1,
            )

    def test_many_attachments_scroll_horizontally(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sources = []
            for index in range(8):
                source = root / f"photo-{index}.png"
                source.write_bytes(f"not an image {index}".encode())
                sources.append(str(source))
            self.panel._attachment_directory = root / "attachments"
            self.panel.show_panel()
            self.app.processEvents()

            with patch(
                "chat_panel.QFileDialog.getOpenFileNames",
                return_value=(sources, ""),
            ):
                self._select_file_from_attachment_menu()
            self.app.processEvents()

            self.assertGreater(
                self.panel._attachment_preview.horizontalScrollBar().maximum(),
                0,
            )
            self.assertEqual(
                {
                    item.geometry().top()
                    for item in self.panel._attachment_preview_content.findChildren(
                        QWidget,
                        "mediaAttachmentPreview",
                    )
                },
                {0},
            )

    def test_clicking_image_shows_full_aspect_ratio_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "portrait.png"
            original = QPixmap(60, 120)
            original.fill(QColor("#ffffff"))
            self.assertTrue(original.save(str(source)))
            self.panel._attachment_directory = root / "attachments"
            self.panel.show_panel()
            self.app.processEvents()

            with patch(
                "chat_panel.QFileDialog.getOpenFileNames",
                return_value=([str(source)], ""),
            ):
                self._select_file_from_attachment_menu()
            self.app.processEvents()
            tile = self.panel._attachment_preview_content.findChild(
                QWidget,
                "mediaAttachmentPreview",
            )

            QTest.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
            self.app.processEvents()

            self.assertIs(
                self.panel._body_stack.currentWidget(),
                self.panel._media_preview_page,
            )
            self.assertFalse(self.panel._header.isVisible())
            self.assertFalse(self.panel._footer.isVisible())
            self.assertFalse(self.panel._scroll_to_latest_button.isVisible())
            self.assertEqual(self.panel._body_stack.rect(), self.panel.rect())
            close_button = self.panel._media_preview_page._close_button
            self.assertEqual(
                self.panel._media_preview_page.width()
                - close_button.x()
                - close_button.width(),
                4,
            )
            self.assertEqual(close_button.y(), 4)
            preview = self.panel._media_preview_page._surface
            self.assertFalse(preview.image.isNull())
            self.assertGreater(preview.display_rect.height(), 300)
            self.assertAlmostEqual(
                preview.display_rect.width() / preview.display_rect.height(),
                0.5,
                delta=0.02,
            )

            self.panel._media_preview_page._close_button.click()

            self.assertIs(
                self.panel._body_stack.currentWidget(),
                self.panel._scroll_area,
            )
            self.assertTrue(self.panel._header.isVisible())
            self.assertTrue(self.panel._footer.isVisible())

    def test_clicking_video_opens_video_preview_with_audio(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clip.mp4"
            source.write_bytes(b"video data")
            self.panel._attachment_directory = root / "attachments"
            self.panel.show_panel()
            self.app.processEvents()

            with patch(
                "chat_panel.QFileDialog.getOpenFileNames",
                return_value=([str(source)], ""),
            ):
                self._select_file_from_attachment_menu()
            self.app.processEvents()
            tile = self.panel._attachment_preview_content.findChild(
                QWidget,
                "mediaAttachmentPreview",
            )

            QTest.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
            self.app.processEvents()

            self.assertIs(
                self.panel._body_stack.currentWidget(),
                self.panel._media_preview_page,
            )
            self.assertTrue(self.panel._media_preview_page._surface._is_video)
            self.assertFalse(
                self.panel._media_preview_page._audio_output.isMuted()
            )
            self.assertEqual(
                self.panel._media_preview_page._audio_output.volume(),
                1.0,
            )
            self.assertEqual(
                self.panel._media_preview_page._media_player.source().toLocalFile(),
                str(self.panel._attachments[0].stored_path),
            )

            self.panel._media_preview_page._close_button.click()

            self.assertEqual(
                self.panel._media_preview_page._media_player.playbackState(),
                QMediaPlayer.PlaybackState.StoppedState,
            )

    def test_video_surface_single_click_timer_emits_toggle(self) -> None:
        surface = self.panel._media_preview_page._surface
        spy = QSignalSpy(surface.single_clicked)
        surface._click_timer.start(1)
        QTest.qWait(20)

        self.assertEqual(spy.count(), 1)

    def test_video_playback_toggle_shows_fading_play_pause_indicator(self) -> None:
        page = self.panel._media_preview_page
        page._media_type = "video"
        player = page._media_player

        with (
            patch.object(
                player,
                "playbackState",
                return_value=QMediaPlayer.PlaybackState.PausedState,
            ),
            patch.object(player, "play") as play,
        ):
            page._toggle_video()

        play.assert_called_once()
        self.assertEqual(page._surface._indicator, "pause")

        with (
            patch.object(
                player,
                "playbackState",
                return_value=QMediaPlayer.PlaybackState.PlayingState,
            ),
            patch.object(player, "pause") as pause,
        ):
            page._toggle_video()

        pause.assert_called_once()
        self.assertEqual(page._surface._indicator, "play")

    def test_video_seek_clamps_position_at_both_ends(self) -> None:
        page = self.panel._media_preview_page
        page._media_type = "video"
        page._surface.resize(420, 560)
        player = page._media_player

        with (
            patch.object(player, "position", return_value=2_000),
            patch.object(player, "duration", return_value=60_000),
            patch.object(player, "setPosition") as set_position,
        ):
            page._seek_video(-5_000)
        set_position.assert_called_once_with(0)
        self.assertEqual(page._surface._seek_indicator, "backward")
        self.assertEqual(page._surface._seek_center_x, 105)

        with (
            patch.object(player, "position", return_value=59_000),
            patch.object(player, "duration", return_value=60_000),
            patch.object(player, "setPosition") as set_position,
        ):
            page._seek_video(5_000)
        set_position.assert_called_once_with(60_000)
        self.assertEqual(page._surface._seek_indicator, "forward")
        self.assertEqual(page._surface._seek_center_x, 315)

    def test_video_grid_uses_decoded_frame_as_thumbnail(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            video = Path(temporary_directory) / "clip.mp4"
            video.write_bytes(b"video data")
            tile = _AttachmentPreviewItem(
                _AttachedFile("clip.mp4", video, "video"),
                None,
            )
            frame_image = QPixmap(160, 90)
            frame_image.fill(QColor("#7799bb"))

            tile._set_video_thumbnail(
                SimpleNamespace(toImage=frame_image.toImage)
            )

            self.assertFalse(tile._thumbnail.isNull())
            self.assertEqual(tile._thumbnail.size(), frame_image.size())
            self.assertEqual(
                tile._thumbnail_player.playbackState(),
                QMediaPlayer.PlaybackState.StoppedState,
            )

    def test_video_double_click_seeks_five_seconds_and_drag_zoom_resets(self) -> None:
        surface = self.panel._media_preview_page._surface
        surface.resize(420, 560)
        surface.set_media(QPixmap(100, 100).toImage(), True)
        surface.show()
        seek_spy = QSignalSpy(surface.seek_requested)
        QTest.mouseDClick(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(30, 200),
        )
        QTest.mouseRelease(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(30, 200),
        )

        self.assertEqual(seek_spy.count(), 1)
        self.assertEqual(seek_spy.at(0)[0], -5_000)

        QTest.mouseDClick(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(390, 200),
        )
        QTest.mouseRelease(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(390, 200),
        )
        self.assertEqual(seek_spy.count(), 2)
        self.assertEqual(seek_spy.at(1)[0], 5_000)

        surface.set_media(QPixmap(100, 100).toImage(), False)
        QTest.mouseDClick(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(200, 200),
        )
        QTest.mouseMove(
            surface,
            QPoint(260, 260),
            delay=10,
        )

        self.assertGreater(surface._zoom_factor, 1)
        zoomed_factor = surface._zoom_factor
        QTest.mouseMove(surface, QPoint(201, 200), delay=10)
        self.assertGreater(surface._zoom_factor, 1)
        self.assertLess(surface._zoom_factor, zoomed_factor)
        QTest.mouseRelease(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(201, 200),
        )
        self.assertEqual(surface._zoom_factor, 1)

    def test_zoom_starts_from_a_second_press_and_tracks_back_toward_origin(self) -> None:
        surface = self.panel._media_preview_page._surface
        surface.resize(420, 560)
        surface.set_media(QPixmap(100, 100).toImage(), False)
        surface.show()
        surface._click_timer.start()

        QTest.mousePress(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(160, 160),
        )
        self.assertTrue(surface._double_click_active)

        QTest.mouseMove(surface, QPoint(220, 160), delay=10)
        zoomed_factor = surface._zoom_factor
        QTest.mouseMove(surface, QPoint(163, 160), delay=10)

        self.assertGreater(zoomed_factor, surface._zoom_factor)
        self.assertGreater(surface._zoom_factor, 1)
        QTest.mouseRelease(
            surface,
            Qt.MouseButton.LeftButton,
            pos=QPoint(163, 160),
        )
        self.assertEqual(surface._zoom_factor, 1)

    def test_remove_button_deletes_copy_and_updates_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "photo.jpg"
            source.write_bytes(b"image content")
            self.panel._attachment_directory = root / "attachments"
            self.panel.show_panel()
            self.app.processEvents()
            initial_height = self.panel._input_frame.height()

            with patch(
                "chat_panel.QFileDialog.getOpenFileNames",
                return_value=([str(source)], ""),
            ):
                self._select_file_from_attachment_menu()
            self.app.processEvents()

            stored_path = self.panel._attachments[0].stored_path
            remove_button = self.panel._attachment_preview.findChild(
                QToolButton,
                "removeAttachmentButton",
            )
            self.assertIsNotNone(remove_button)
            remove_button.click()
            self.app.processEvents()

            self.assertFalse(stored_path.exists())
            self.assertEqual(self.panel._attachments, [])
            self.assertFalse(self.panel._attachment_preview.isVisible())
            self.assertEqual(self.panel._input_frame.height(), initial_height)

    def test_draft_attachment_does_not_expire_before_being_sent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "notes.txt"
            source.write_text("attachment content")
            storage = root / "attachments"
            stored_path = copy_attachment(source, storage)
            os.utime(stored_path, (0, 0))
            self.panel._attachment_directory = storage
            self.panel._attachments = [
                _AttachedFile("notes.txt", stored_path, None)
            ]

            self.panel._expire_attachments()

            self.assertTrue(stored_path.exists())
            self.assertEqual(len(self.panel._attachments), 1)

    def test_attachment_and_submit_buttons_are_inside_input_border(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()
        frame = self.panel._input_frame
        self.assertGreater(frame.width(), self.panel.width() - 28)
        input_bottom = (
            self.panel._input.mapTo(frame, QPoint(0, 0)).y()
            + self.panel._input.height()
        )
        attach_position = self.panel._attach_button.mapTo(frame, QPoint(0, 0))
        submit_position = self.panel._submit_button.mapTo(frame, QPoint(0, 0))

        self.assertTrue(frame.rect().contains(attach_position))
        self.assertTrue(frame.rect().contains(submit_position))
        self.assertGreater(attach_position.y(), input_bottom)
        self.assertEqual(attach_position.x(), 6)
        self.assertEqual(frame.height(), self.panel._input.height() + 56)
        self.assertEqual(
            frame.height()
            - (submit_position.y() + self.panel._submit_button.height()),
            6,
        )
        submit_right_inset = (
            frame.width()
            - (submit_position.x() + self.panel._submit_button.width())
        )
        self.assertEqual(submit_right_inset, 6)
        input_left_inset = self.panel._input.x()
        input_right_inset = (
            frame.width()
            - (self.panel._input.x() + self.panel._input.width())
        )
        self.assertEqual(input_left_inset, 6)
        self.assertEqual(input_right_inset, 2)
        self.assertLessEqual(attach_position.y() - input_bottom, 4)

    def test_input_text_starts_near_left_edge_of_border(self) -> None:
        self.panel._input.setPlainText("message")
        self.app.processEvents()
        cursor = self.panel._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        input_cursor_x = self.panel._input.mapTo(
            self.panel._input_frame,
            self.panel._input.cursorRect(cursor).topLeft(),
        ).x()

        self.assertLessEqual(input_cursor_x, 8)

    def test_input_expands_to_four_lines_and_scrolls_longer_text(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()
        initial_height = self.panel._input.height()
        self.panel._input.setPlainText("line one\nline two")
        self.app.processEvents()
        self.app.processEvents()
        two_line_height = self.panel._input.height()

        self.panel._input.setPlainText(
            "line one\nline two\nline three\nline four\nline five"
        )
        self.app.processEvents()
        self.app.processEvents()

        self.assertGreater(two_line_height, initial_height)
        self.assertLessEqual(
            self.panel._input.height(),
            self.panel._input_line_height() * 4 + 8,
        )
        self.assertEqual(
            self.panel._input.height(),
            self.panel._input_line_height() * 4 + 8,
        )
        self.assertLessEqual(
            self.panel._input.viewport().height(),
            self.panel._input_line_height() * 4 + 8,
        )
        self.assertGreater(self.panel._input.verticalScrollBar().maximum(), 0)
        self.assertFalse(self.panel._input.verticalScrollBar().isVisible())

    def test_input_textarea_uses_balanced_visible_text_insets(self) -> None:
        self.panel.show_panel()
        self.app.processEvents()
        frame = self.panel._input_frame
        input_box = self.panel._input
        left_inset = input_box.x()
        right_inset = frame.width() - input_box.x() - input_box.width()

        self.assertEqual(left_inset, 6)
        self.assertEqual(right_inset, 2)
        self.assertEqual(input_box.document().documentMargin(), 0)

if __name__ == "__main__":
    unittest.main()

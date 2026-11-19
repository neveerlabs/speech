import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QColor,
    QFontMetrics,
    QImage,
    QPixmap,
    QTextFormat,
    QTextTable,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QTextBrowser,
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
    def __init__(self, _api_keys, _database) -> None:
        self.reply_received = _FakeSignal()
        self.error_occurred = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return False

    def deleteLater(self) -> None:
        pass


class ChatPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.database = ChatDatabase(":memory:")
        self.panel = ChatPanel(database=self.database)

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

    def test_model_message_actions_are_visual_only_and_have_requested_order(self) -> None:
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

        for button in model_row._left_actions + (model_row._regenerate_button,):
            button.click()
        self.assertEqual(
            [(stored.role, stored.content) for stored in self.database.load_messages()],
            [("model", "A response")],
        )

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
        greeting_prefix_width = QFontMetrics(
            long_bubble._label.font()
        ).horizontalAdvance("Halo! ")
        self.assertEqual(
            long_bubble.width(),
            long_row.width() - greeting_prefix_width,
        )
        self.assertEqual(long_bubble.x(), greeting_prefix_width)
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
        self.assertEqual(self.panel._message_layout.spacing(), 4)

    def test_chat_messages_render_markdown(self) -> None:
        message = self.database.append_user_message("**Bold** and `code`")
        model_message = self.database.append_model_message("**Formatted answer**")
        self.panel.show_panel()
        self.panel._add_message_row(message)
        self.panel._add_message_row(model_message)
        self.app.processEvents()

        user_row = self.panel._message_layout.itemAt(0).widget()
        model_row = self.panel._message_layout.itemAt(1).widget()
        model_label = model_row.findChild(QTextBrowser, "messageContent")

        self.assertEqual(
            user_row._bubble._label.textFormat(),
            Qt.TextFormat.MarkdownText,
        )
        self.assertIsNotNone(model_label)
        self.assertEqual(model_label.textFormat(), Qt.TextFormat.MarkdownText)

    def test_markdown_uses_compact_lists_and_github_style_blocks(self) -> None:
        markdown = (
            "# Heading\n\n"
            "1. First item\n2. Second item\n\n"
            "> A quote\n\n"
            "Inline `code` and ~~strikethrough~~ with [a link](https://example.com)\n\n"
            "```python\nprint('hello')\n```\n\n"
            "| Name | Value |\n| --- | --- |\n| item | 1 |\n\n"
            "- [ ] Task\n- [x] Done\n"
        )
        view = _WrappedMessageLabel(markdown, self.panel)
        document = view.document()
        html = document.toHtml().lower()
        block = document.begin()
        first_list_block = None
        heading_block = None
        quote_block = None
        code_block = None
        inline_code_is_styled = False
        link_is_styled = False
        while block.isValid():
            if block.blockFormat().headingLevel():
                heading_block = block
            if block.blockFormat().property(
                QTextFormat.Property.BlockQuoteLevel
            ):
                quote_block = block
            if block.blockFormat().property(
                QTextFormat.Property.BlockCodeLanguage
            ):
                code_block = block
            if block.textList() is not None:
                first_list_block = block
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
                    if char_format.isAnchor():
                        link_is_styled = (
                            char_format.foreground().color()
                            == QColor("#58a6ff")
                        )
                fragment_iterator += 1
            block = block.next()

        self.assertIsNotNone(first_list_block)
        self.assertIsNotNone(heading_block)
        self.assertIsNotNone(quote_block)
        self.assertIsNotNone(code_block)
        self.assertEqual(first_list_block.textList().format().indent(), 0)
        self.assertEqual(first_list_block.blockFormat().leftMargin(), 12)
        self.assertEqual(
            quote_block.blockFormat().background().color(),
            QColor("#262b33"),
        )
        self.assertEqual(
            code_block.blockFormat().background().color(),
            QColor("#161b22"),
        )
        self.assertTrue(
            code_block.begin().fragment().charFormat().fontFixedPitch()
        )
        self.assertTrue(inline_code_is_styled)
        self.assertTrue(link_is_styled)
        self.assertGreater(
            heading_block.begin().fragment().charFormat().fontPointSize(),
            view.font().pointSizeF(),
        )
        self.assertIn("<h1", html)
        self.assertIn("<ol", html)
        self.assertIn("<pre", html)
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
        self.assertIn(
            "border-bottom: 1px solid #3a3f46",
            self.panel._header.styleSheet(),
        )
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

            self.assertFalse(cached_path.exists())
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

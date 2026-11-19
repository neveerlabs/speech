from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import logging
import mimetypes
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from PySide6.QtCore import (
    QAbstractAnimation,
    QByteArray,
    QEasingCurve,
    QEvent,
    QElapsedTimer,
    QMimeData,
    QPoint,
    QPointF,
    Property,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextTable,
    QTextLength,
)
from PySide6.QtMultimedia import (
    QAudioOutput,
    QMediaPlayer,
    QVideoFrame,
    QVideoSink,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QTextBrowser,
)

from chat_attachments import copy_attachment
from chat_database import (
    ChatDatabase,
    MAX_ATTACHMENT_SIZE_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    PendingAttachment,
    StoredMessage,
    utc_now_iso,
)
from chat_service import ChatResponseWorker

LOGGER = logging.getLogger(__name__)
PANEL_SNAP_DISTANCE = 20
MARKDOWN_STYLESHEET = """
body { color: #f3f4f6; }
p { margin: 0 0 8px 0; }
h1, h2, h3, h4, h5, h6 {
    color: #f0f6fc;
    font-weight: 600;
    margin: 16px 0 8px 0;
    padding-bottom: 4px;
}
h1, h2 { border-bottom: 1px solid #30363d; }
h1 { font-size: 1.45em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.15em; }
a { color: #58a6ff; text-decoration: none; }
blockquote {
    color: #b1bac4;
    border-left: 3px solid #3d444d;
    margin: 8px 0;
    padding: 0 0 0 12px;
}
code {
    color: #e6edf3;
    background-color: #2d333b;
    font-family: monospace;
}
pre {
    color: #e6edf3;
    background-color: #161b22;
    border: 1px solid #30363d;
    margin: 8px 0;
    padding: 8px;
}
pre code { background-color: transparent; }
table { border-collapse: collapse; margin: 8px 0; }
th, td { border: 1px solid #3d444d; padding: 4px 8px; }
th { background-color: #2d333b; font-weight: 600; }
hr { color: #30363d; }
"""


def _style_markdown_document(document: QTextDocument) -> None:
    block = document.begin()
    while block.isValid():
        block_format = block.blockFormat()
        heading_level = block_format.headingLevel()
        quote_level = int(
            block_format.property(QTextFormat.Property.BlockQuoteLevel) or 0
        )
        code_block = (
            block_format.property(QTextFormat.Property.BlockCodeLanguage)
            is not None
        )
        markdown_list = block.textList()
        character_style = QTextCharFormat()
        if markdown_list is not None:
            list_format = markdown_list.format()
            if list_format.indent() != 0:
                list_format.setIndent(0)
                markdown_list.setFormat(list_format)
            block_format.setLeftMargin(12)
            block_format.setTopMargin(2)
            block_format.setBottomMargin(2)
        elif quote_level:
            block_format.setLeftMargin(12)
            block_format.setRightMargin(0)
            block_format.setTopMargin(6)
            block_format.setBottomMargin(6)
            block_format.setBackground(QColor("#262b33"))
            character_style.setForeground(QColor("#b1bac4"))
        elif code_block:
            block_format.setLeftMargin(8)
            block_format.setRightMargin(8)
            block_format.setTopMargin(4)
            block_format.setBottomMargin(4)
            block_format.setBackground(QColor("#161b22"))
            character_style.setForeground(QColor("#e6edf3"))
            character_style.setFontFixedPitch(True)
        elif heading_level:
            block_format.setTopMargin(12 if heading_level == 1 else 10)
            block_format.setBottomMargin(6)
            character_style.setForeground(QColor("#f0f6fc"))
            character_style.setFontWeight(QFont.Weight.DemiBold)
            character_style.setFontPointSize(
                max(10, 17 - (heading_level - 1) * 2)
            )
        QTextCursor(block).setBlockFormat(block_format)
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        if character_style.hasProperty(QTextFormat.Property.ForegroundBrush) or (
            character_style.hasProperty(QTextFormat.Property.FontFamily)
        ):
            cursor.mergeCharFormat(character_style)

        fragment_iterator = block.begin()
        while not fragment_iterator.atEnd():
            fragment = fragment_iterator.fragment()
            if fragment.isValid():
                format_style = QTextCharFormat()
                original_format = fragment.charFormat()
                if original_format.isAnchor():
                    format_style.setForeground(QColor("#58a6ff"))
                    format_style.setUnderlineStyle(
                        QTextCharFormat.UnderlineStyle.SingleUnderline
                    )
                if original_format.fontFixedPitch():
                    format_style.setForeground(QColor("#e6edf3"))
                    format_style.setBackground(QColor("#2d333b"))
                    format_style.setFontFixedPitch(True)
                if format_style.properties():
                    cursor = QTextCursor(document)
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(
                        fragment.position() + fragment.length(),
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    cursor.mergeCharFormat(format_style)
            fragment_iterator += 1
        block = block.next()

    for frame in document.rootFrame().childFrames():
        if not isinstance(frame, QTextTable):
            continue
        table_format = frame.format()
        table_format.setWidth(
            QTextLength(QTextLength.Type.PercentageLength, 100)
        )
        table_format.setBorder(1)
        table_format.setBorderBrush(QColor("#3d444d"))
        table_format.setBorderCollapse(True)
        table_format.setCellPadding(5)
        table_format.setCellSpacing(0)
        frame.setFormat(table_format)
        for row in range(frame.rows()):
            for column in range(frame.columns()):
                cell = frame.cellAt(row, column)
                cell_format = cell.format()
                for border in (
                    "TableCellTopBorder",
                    "TableCellBottomBorder",
                    "TableCellLeftBorder",
                    "TableCellRightBorder",
                ):
                    cell_format.setProperty(
                        getattr(QTextFormat.Property, border),
                        1.0,
                    )
                for brush in (
                    "TableCellTopBorderBrush",
                    "TableCellBottomBorderBrush",
                    "TableCellLeftBorderBrush",
                    "TableCellRightBorderBrush",
                ):
                    cell_format.setProperty(
                        getattr(QTextFormat.Property, brush),
                        QColor("#3d444d"),
                    )
                if row == 0:
                    cell_format.setBackground(QColor("#2d333b"))
                    cell.setFormat(cell_format)
                    cursor = cell.firstCursorPosition()
                    cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                    header_style = QTextCharFormat()
                    header_style.setForeground(QColor("#f0f6fc"))
                    header_style.setFontWeight(QFont.Weight.DemiBold)
                    cursor.mergeCharFormat(header_style)
                else:
                    cell.setFormat(cell_format)


@dataclass(frozen=True)
class _AttachedFile:
    name: str
    stored_path: Path
    media_type: str | None
    attached_at_utc: str = ""
    content_sha256: str = ""
    attachment_id: int | None = None
    expired: bool = False


class _UserMessageBubble(QWidget):
    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._text = text
        self.setMinimumWidth(0)
        self.setMaximumWidth(360)
        self.setMinimumHeight(30)
        self.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Minimum,
        )
        self._label = _MarkdownTextView(text, self)
        self._label.setStyleSheet("color: #f3f4f6; background: transparent;")

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        text_width = max(1, width - 14)
        document = _markdown_document(self._text, self._label.font())
        document.setTextWidth(text_width)
        text_height = int(document.size().height() + 0.999)
        return max(self.minimumHeight(), text_height + 10)

    def sizeHint(self) -> QSize:
        document = _markdown_document(self._text, self._label.font())
        text_width = int(document.idealWidth() + 0.999)
        width = min(
            self.maximumWidth(),
            max(self.minimumWidth(), text_width + 14),
        )
        return QSize(width, self.heightForWidth(width))

    def set_text(self, text: str) -> None:
        self._text = text
        self._label.setText(text)
        self.updateGeometry()

    @property
    def text(self) -> str:
        return self._text

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._label.setGeometry(self.rect().adjusted(7, 5, -7, -5))
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#59616b"), 1))
        painter.setBrush(QColor("#3a3f46"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 9, 9)


class _UserMessageRow(QWidget):
    message_copied = Signal()
    media_activated = Signal(str)

    def __init__(
        self,
        message: StoredMessage,
        parent: QWidget,
        attachments: tuple[_AttachedFile, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("userMessageRow")
        self.setMouseTracking(True)
        self._text = message.content
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        self._bubble = (
            _UserMessageBubble(message.content, self)
            if message.content.strip()
            else None
        )
        self._attachment_grid: QWidget | None = None
        self._attachment_tiles: list[_AttachmentPreviewItem] = []
        if attachments:
            self._attachment_grid = QWidget(self)
            self._attachment_grid.setObjectName("messageAttachmentGrid")
            grid_layout = QGridLayout(self._attachment_grid)
            grid_layout.setContentsMargins(0, 0, 0, 0)
            grid_layout.setHorizontalSpacing(6)
            grid_layout.setVerticalSpacing(6)
            for index, attachment in enumerate(attachments):
                tile = _AttachmentPreviewItem(
                    attachment,
                    self._attachment_grid,
                    message_attachment=True,
                )
                tile.media_activated.connect(self.media_activated)
                self._attachment_tiles.append(tile)
                grid_layout.addWidget(tile, index // 2, index % 2)
            self._attachment_grid.setSizePolicy(
                QSizePolicy.Policy.Maximum,
                QSizePolicy.Policy.Fixed,
            )
        self._rest_y = 4
        self._hover_y = 0
        self._animated_content_rest_y = self._rest_y
        self._actions_expanded = False
        self._hover_active = False
        self._actions_gap = 0

        self._actions = QWidget(self)
        self._actions.setObjectName("userMessageActions")
        actions_layout = QHBoxLayout(self._actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(0)
        self._copy_icon_state = "copy"
        self._delete_button = self._create_action(
            "delete", "Hapus pesan", lambda: None
        )
        self._copy_button = self._create_action(
            "copy", "Salin pesan", self._copy_message
        )
        self._edit_button = self._create_action(
            "edit", "Edit pesan", lambda: None
        )
        self._action_buttons = (
            self._delete_button,
            self._copy_button,
            self._edit_button,
        )
        for button in self._action_buttons:
            actions_layout.addWidget(button)

        self._animated_content = self._bubble or self._attachment_grid
        self._hover_targets = tuple(
            widget
            for widget in (
                self._bubble,
                self._bubble._label if self._bubble is not None else None,
                (
                    self._bubble._label.viewport()
                    if self._bubble is not None
                    else None
                ),
                self._attachment_grid,
                *self._attachment_tiles,
                self._actions,
                *self._action_buttons,
            )
            if widget is not None
        )
        for widget in self._hover_targets:
            widget.installEventFilter(self)
        self._hover_exit_timer = QTimer(self)
        self._hover_exit_timer.setSingleShot(True)
        self._hover_exit_timer.setInterval(80)
        self._hover_exit_timer.timeout.connect(self._sync_hover_state)

        self._opacity = QGraphicsOpacityEffect(self._actions)
        self._opacity.setOpacity(0)
        self._actions.setGraphicsEffect(self._opacity)
        self._actions.hide()

        self._position_animation = QPropertyAnimation(
            self._animated_content,
            b"pos",
            self,
        )
        self._position_animation.setDuration(150)
        self._position_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._opacity_animation = QPropertyAnimation(self._opacity, b"opacity", self)
        self._opacity_animation.setDuration(150)
        self._opacity_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._opacity_animation.finished.connect(self._hide_actions)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(2200)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)

    def sizeHint(self) -> QSize:
        bubble_size = self._bubble.sizeHint() if self._bubble is not None else QSize()
        grid_size = (
            self._attachment_grid.sizeHint()
            if self._attachment_grid is not None
            else QSize()
        )
        content_height = bubble_size.height() + grid_size.height()
        if self._bubble is not None and self._attachment_grid is not None:
            content_height += 4
        width = max(
            bubble_size.width(),
            grid_size.width(),
            self._actions.sizeHint().width(),
        )
        height = content_height + self._rest_y
        if self._actions_expanded:
            height = max(
                height,
                self._hover_y
                + content_height
                + self._actions_gap
                + self._actions.sizeHint().height(),
            )
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._layout_children()
        super().resizeEvent(event)

    def _layout_children(self) -> None:
        content_height = 0
        if self._attachment_grid is not None:
            grid_size = self._attachment_grid.sizeHint()
            grid_x = max(0, self.width() - grid_size.width())
            grid_y = self._rest_y
            if (
                self._animated_content is self._attachment_grid
                and self._position_animation.state()
                == QAbstractAnimation.State.Running
            ):
                grid_y = self._attachment_grid.y()
            self._attachment_grid.setGeometry(
                grid_x,
                grid_y,
                grid_size.width(),
                grid_size.height(),
            )
            content_height += grid_size.height()

        if self._bubble is not None:
            if self._attachment_grid is not None:
                content_height += 4
            greeting_prefix_width = QFontMetrics(
                self._bubble._label.font()
            ).horizontalAdvance("Halo! ")
            self._bubble.setMaximumWidth(
                max(
                    self._bubble.minimumWidth(),
                    self.width() - greeting_prefix_width,
                )
            )
            bubble_size = self._bubble.sizeHint()
            bubble_x = max(0, self.width() - bubble_size.width())
            bubble_y = self._rest_y + content_height
            self._animated_content_rest_y = bubble_y
            self._bubble.resize(bubble_size)
            if self._position_animation.state() != QAbstractAnimation.State.Running:
                self._bubble.move(bubble_x, bubble_y)
            else:
                self._bubble.move(bubble_x, self._bubble.y())
            content_height += bubble_size.height()
        else:
            self._animated_content_rest_y = self._rest_y

        actions_size = self._actions.sizeHint()
        lift = self._rest_y - self._hover_y if self._actions_expanded else 0
        actions_y = self._rest_y + content_height + self._actions_gap - lift
        self._actions.setGeometry(
            max(0, self.width() - actions_size.width()),
            actions_y,
            actions_size.width(),
            actions_size.height(),
        )

    def set_text(self, text: str) -> None:
        self._text = text
        if self._bubble is None and text.strip():
            self._bubble = _UserMessageBubble(text, self)
            self._animated_content = self._bubble
            self._bubble.installEventFilter(self)
            self._bubble._label.installEventFilter(self)
            self._hover_targets += (self._bubble, self._bubble._label)
            self._position_animation.setTargetObject(self._animated_content)
        elif self._bubble is not None:
            self._bubble.set_text(text)
        self.updateGeometry()
        self._layout_children()

    def eventFilter(self, watched, event) -> bool:
        if watched in self._hover_targets:
            if event.type() == QEvent.Type.Enter:
                self._hover_exit_timer.stop()
                if not self._hover_active:
                    self._set_hovered(True)
            elif event.type() == QEvent.Type.Leave:
                self._hover_exit_timer.start()
        return super().eventFilter(watched, event)

    def _create_action(
        self,
        name: str,
        label: str,
        callback: Callable[[], None],
    ) -> QToolButton:
        button = QToolButton(self._actions)
        button.setObjectName(f"userMessage{name.title()}Button")
        button.setAccessibleName(label)
        button.setFixedSize(18, 18)
        button.setIcon(self._action_icon(name))
        button.setIconSize(QSize(16, 16))
        color = self._action_color(name)
        button.setStyleSheet(
            f"QToolButton {{ color: {color}; background: transparent; "
            "border: none; }"
        )
        button.clicked.connect(callback)
        return button

    def _action_color(self, name: str) -> str:
        return {
            "delete": "#f87171",
            "copy": "#aeb4bd",
            "edit": "#aeb4bd",
            "check": "#aeb4bd",
        }[name]

    def _action_icon(self, name: str) -> QIcon:
        paths = {
            "delete": (
                "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12z"
                "M19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"
            ),
            "copy": (
                "M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 "
                "2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 "
                "16H8V7h11v14z"
            ),
            "edit": (
                "M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25z"
                "M20.71 7.04a1 1 0 000-1.41l-2.34-2.34a1 1 0 00-1.41 "
                "0l-1.83 1.83 3.75 3.75 1.83-1.83z"
            ),
            "check": "M9 16.17 4.83 12 3.41 13.41 9 19l12-12-1.41-1.41z",
        }
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            f'viewBox="0 0 24 24"><path fill="{self._action_color(name)}" '
            f'd="{paths[name]}"/></svg>'
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)

    def _reset_copy_feedback(self) -> None:
        self._copy_icon_state = "copy"
        self._copy_button.setIcon(self._action_icon("copy"))

    def _set_hovered(self, hovered: bool) -> None:
        self._hover_active = hovered
        animated_content = self._animated_content
        if animated_content is None:
            return
        if hovered:
            self._actions_expanded = True
            self.updateGeometry()
            parent_layout = self.parentWidget().layout()
            if parent_layout is not None:
                parent_layout.activate()
            self._layout_children()

        self._position_animation.stop()
        self._position_animation.setStartValue(animated_content.pos())
        self._position_animation.setEndValue(
            QPoint(
                max(0, self.width() - animated_content.width()),
                self._animated_content_rest_y
                - (self._rest_y - self._hover_y if hovered else 0),
            )
        )
        self._position_animation.start()

        self._opacity_animation.stop()
        if hovered:
            self._actions.show()
        self._opacity_animation.setStartValue(self._opacity.opacity())
        self._opacity_animation.setEndValue(1 if hovered else 0)
        self._opacity_animation.start()

    def _sync_hover_state(self) -> None:
        cursor_position = QCursor.pos()
        for widget in self._hover_targets:
            if (
                widget.isVisible()
                and widget.rect().contains(widget.mapFromGlobal(cursor_position))
            ):
                return
        if self._hover_active:
            self._set_hovered(False)

    def _hide_actions(self) -> None:
        if self._opacity.opacity() == 0:
            self._actions.hide()
            self._actions_expanded = False
            self.updateGeometry()
            parent_layout = self.parentWidget().layout()
            if parent_layout is not None:
                parent_layout.activate()

    def _delete_message(self) -> None:
        self.hide()
        self.deleteLater()

    def _copy_message(self) -> None:
        QApplication.clipboard().setText(self._text)
        self._copy_icon_state = "check"
        self._copy_button.setIcon(self._action_icon("check"))
        self._copy_feedback_timer.start()
        self.message_copied.emit()


class _ConversationMessageRow(QWidget):
    _ACTION_ICONS = {
        "like": (
            '<path d="M7 10v12M15 5.88 14 10h5.83a2 2 0 0 1 '
            "1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H5a2 2 0 0 1-2-2v-8"
            "a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 3a3 3 "
            '0 0 1 3 2.88Z"/>'
        ),
        "dislike": (
            '<g transform="rotate(180 12 12)"><path d="M7 10v12M15 '
            "5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 "
            "0 1 17.5 22H5a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 "
            '2 0 0 0 1.79-1.11L12 3a3 3 0 0 1 3 2.88Z"/></g>'
        ),
        "copy": (
            '<rect x="8" y="8" width="13" height="13" rx="2"/>'
            '<path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 '
            '0 0 0 2 2h3"/>'
        ),
        "speech": (
            '<path d="M11 5 6 9H3v6h3l5 4V5Z"/>'
            '<path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>'
        ),
        "regenerate": (
            '<path d="M20 7v5h-5M4 17v-5h5"/>'
            '<path d="M5.64 9a7 7 0 0 1 11.6-2.6L20 12M4 12l2.76 '
            '5.6A7 7 0 0 0 18.36 15"/>'
        ),
    }

    def __init__(self, message: StoredMessage, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName(f"{message.role}MessageRow")
        size_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(
            size_policy
        )
        row_layout = QVBoxLayout(self)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(3)
        self._add_message_content(message, row_layout, self)
        self._actions = QWidget(self)
        self._actions.setObjectName("modelMessageActions")
        actions_layout = QHBoxLayout(self._actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        self._like_button = self._create_action("like", "Suka")
        self._dislike_button = self._create_action("dislike", "Tidak suka")
        self._copy_button = self._create_action("copy", "Salin")
        self._speech_button = self._create_action("speech", "Bacakan")
        self._regenerate_button = self._create_action("regenerate", "Buat ulang")
        self._left_actions = (
            self._copy_button,
            self._like_button,
            self._dislike_button,
            self._speech_button,
        )
        for button in self._left_actions:
            actions_layout.addWidget(button)
        actions_layout.addStretch()
        actions_layout.addWidget(self._regenerate_button)
        row_layout.addWidget(self._actions)

    def _create_action(self, name: str, accessible_name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(f"modelMessage{name.title()}Button")
        button.setAccessibleName(accessible_name)
        button.setFixedSize(22, 22)
        button.setIcon(self._action_icon(name))
        button.setIconSize(QSize(19, 19))
        button.setStyleSheet(
            "QToolButton { color: #aeb4bd; background: transparent; "
            "border: none; padding: 0; }"
        )
        return button

    @classmethod
    def _action_icon(cls, name: str) -> QIcon:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            'viewBox="0 0 24 24" fill="none" stroke="#aeb4bd" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            f"{cls._ACTION_ICONS[name]}</svg>"
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self.layout().heightForWidth(width)

    @staticmethod
    def _add_message_content(
        message: StoredMessage,
        layout: QVBoxLayout,
        parent: QWidget,
    ) -> None:
        if message.content:
            content_label = _WrappedMessageLabel(message.content, parent)
            content_label.setObjectName("messageContent")
            size_policy = QSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Minimum,
            )
            size_policy.setHeightForWidth(True)
            content_label.setSizePolicy(size_policy)
            content_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            content_label.setStyleSheet("color: #f3f4f6; background: transparent;")
            layout.addWidget(content_label)
        for attachment in message.attachments:
            status = "" if attachment.available else " (kedaluwarsa)"
            attachment_label = _WrappedMessageLabel(
                f"Lampiran: {attachment.name}{status}",
                parent,
            )
            size_policy = QSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Minimum,
            )
            size_policy.setHeightForWidth(True)
            attachment_label.setSizePolicy(size_policy)
            attachment_label.setStyleSheet(
                "color: #cbd5e1; background: transparent; font-size: 11px;"
            )
            layout.addWidget(attachment_label)


class _MarkdownTextView(QTextBrowser):
    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._markdown_source = text
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.setContentsMargins(0, 0, 0, 0)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.setStyleSheet(
            "QTextBrowser { color: #f3f4f6; background: transparent; "
            "border: none; padding: 0; }"
        )
        self.document().setDocumentMargin(0)
        self.document().setDefaultStyleSheet(MARKDOWN_STYLESHEET)
        self.setMarkdown(text)
        self._markdown_source = text
        _style_markdown_document(self.document())

    def text(self) -> str:
        return self._markdown_source

    def textFormat(self) -> Qt.TextFormat:
        return Qt.TextFormat.MarkdownText

    def sizeHint(self) -> QSize:
        document = _markdown_document(self.text(), self.font())
        width = max(1, min(400, int(document.idealWidth() + 0.999)))
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().lineSpacing())

    def setText(self, text: str) -> None:
        self._markdown_source = text
        self.document().clear()
        self.document().setDefaultStyleSheet(MARKDOWN_STYLESHEET)
        self.setMarkdown(text)
        _style_markdown_document(self.document())

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        document = _markdown_document(self.text(), self.font())
        document.setTextWidth(max(1, width))
        return max(1, int(document.size().height() + 0.999))


class _WrappedMessageLabel(_MarkdownTextView):
    pass


def _markdown_document(text: str, font: QFont) -> QTextDocument:
    document = QTextDocument()
    document.setDefaultFont(font)
    document.setDocumentMargin(0)
    document.setDefaultStyleSheet(MARKDOWN_STYLESHEET)
    document.setMarkdown(text)
    _style_markdown_document(document)
    return document


class _MediaSurface(QWidget):
    single_clicked = Signal()
    seek_requested = Signal(int)
    _ZOOM_SENSITIVITY = 40
    _MAX_ZOOM = 8.0
    _INDICATOR_DURATION_MS = 850
    _SEEK_INDICATOR_DURATION_MS = 750

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._image = QImage()
        self._is_video = False
        self._error_message = ""
        self._position = 0
        self._duration = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QApplication.doubleClickInterval())
        self._click_timer.timeout.connect(self._emit_single_click)
        self._double_click_active = False
        self._zoom_dragged = False
        self._zoom_start = QPointF()
        self._zoom_anchor = QPointF()
        self._zoom_factor = 1.0
        self._indicator = ""
        self._indicator_clock = QElapsedTimer()
        self._seek_indicator = ""
        self._seek_center_x = 0.0
        self._seek_clock = QElapsedTimer()
        self._indicator_timer = QTimer(self)
        self._indicator_timer.setInterval(16)
        self._indicator_timer.timeout.connect(self._update_indicator)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    @property
    def image(self) -> QImage:
        return self._image

    @property
    def display_rect(self) -> QRectF:
        bounds = QRectF(self.rect())
        if self._is_video:
            bounds.setBottom(bounds.bottom() - 3)
        return self._fit_rect(self._image, bounds)

    def set_media(self, image: QImage, is_video: bool) -> None:
        self._click_timer.stop()
        self._indicator_timer.stop()
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        self._image = image
        self._is_video = is_video
        self._position = 0
        self._duration = 0
        self._error_message = ""
        self._indicator = ""
        self._seek_indicator = ""
        self._double_click_active = False
        self._zoom_dragged = False
        self._zoom_factor = 1.0
        self.update()

    def set_video_frame(self, frame: QVideoFrame) -> None:
        if not self._is_video:
            return
        image = frame.toImage()
        if not image.isNull():
            self._image = image
            self._error_message = ""
            self.update()

    def set_error(self, message: str) -> None:
        self._error_message = message
        self.update()

    def set_position(self, position: int) -> None:
        self._position = max(0, position)
        self.update()

    def set_duration(self, duration: int) -> None:
        self._duration = max(0, duration)
        self.update()

    def show_playback_indicator(self, is_playing: bool) -> None:
        self._indicator = "pause" if is_playing else "play"
        self._indicator_clock.start()
        self._indicator_timer.start()
        self.update()

    def show_seek_indicator(self, offset: int) -> None:
        self._seek_indicator = "forward" if offset > 0 else "backward"
        self._seek_center_x = self.width() * (0.75 if offset > 0 else 0.25)
        self._seek_clock.start()
        self._indicator_timer.start()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        panel_bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(panel_bounds, 16, 16)
        painter.setClipPath(clip)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#17191d"))

        media_bounds = QRectF(panel_bounds)
        if self._is_video:
            media_bounds.setBottom(media_bounds.bottom() - 3)
        image_rect = self._fit_rect(self._image, media_bounds)
        if not self._image.isNull():
            painter.drawImage(
                self._zoomed_rect(image_rect)
                if self._zoom_factor > 1
                else image_rect,
                self._image,
            )
        elif self._error_message:
            painter.setPen(QColor("#cbd0d7"))
            painter.drawText(
                media_bounds.adjusted(24, 24, -24, -24),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._error_message,
            )

        if self._is_video:
            self._paint_progress(painter)
        if self._indicator:
            self._paint_playback_indicator(painter)
        if self._seek_indicator:
            self._paint_seek_indicator(painter)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#525861"), 1))
        painter.drawRoundedRect(
            panel_bounds.adjusted(0.5, 0.5, -0.5, -0.5),
            16,
            16,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._click_timer.isActive():
                self._begin_double_click(event.position())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._begin_double_click(event.position())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _begin_double_click(self, position: QPointF) -> None:
        self._click_timer.stop()
        self._double_click_active = True
        self._zoom_dragged = False
        self._zoom_start = position
        self._zoom_anchor = position
        self._zoom_factor = 1.0
        self.grabMouse()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._double_click_active:
            delta = event.position() - self._zoom_start
            distance = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
            if (
                distance >= QApplication.startDragDistance()
                or self._zoom_dragged
            ):
                self._zoom_dragged = True
                self._zoom_factor = min(
                    self._MAX_ZOOM,
                    1 + distance / self._ZOOM_SENSITIVITY,
                )
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._double_click_active:
            self._double_click_active = False
            if QWidget.mouseGrabber() is self:
                self.releaseMouse()
            if self._zoom_dragged:
                self._zoom_factor = 1.0
                self._zoom_dragged = False
                self.update()
            elif self._is_video:
                offset = -5_000 if event.position().x() < self.width() / 2 else 5_000
                self.seek_requested.emit(offset)
            event.accept()
            return
        self._click_timer.start()
        event.accept()

    def _fit_rect(self, image: QImage, bounds: QRectF) -> QRectF:
        if image.isNull() or bounds.isEmpty():
            return QRectF()
        scale = min(
            bounds.width() / image.width(),
            bounds.height() / image.height(),
        )
        width = image.width() * scale
        height = image.height() * scale
        return QRectF(
            bounds.center().x() - width / 2,
            bounds.center().y() - height / 2,
            width,
            height,
        )

    def _emit_single_click(self) -> None:
        self.single_clicked.emit()

    def _zoomed_rect(self, image_rect: QRectF) -> QRectF:
        anchor_x = min(max(self._zoom_anchor.x(), image_rect.left()), image_rect.right())
        anchor_y = min(max(self._zoom_anchor.y(), image_rect.top()), image_rect.bottom())
        width = image_rect.width() * self._zoom_factor
        height = image_rect.height() * self._zoom_factor
        return QRectF(
            anchor_x - (anchor_x - image_rect.left()) * self._zoom_factor,
            anchor_y - (anchor_y - image_rect.top()) * self._zoom_factor,
            width,
            height,
        )

    def _paint_progress(self, painter: QPainter) -> None:
        bar = QRectF(0, self.height() - 3, self.width(), 3)
        painter.fillRect(bar, QColor(255, 255, 255, 70))
        if self._duration:
            progress = min(1.0, self._position / self._duration)
            painter.fillRect(
                QRectF(bar.left(), bar.top(), bar.width() * progress, bar.height()),
                QColor("#cbd7ff"),
            )

    def _paint_playback_indicator(self, painter: QPainter) -> None:
        elapsed = self._indicator_clock.elapsed()
        opacity = max(0.0, 1 - elapsed / self._INDICATOR_DURATION_MS)
        if opacity <= 0:
            return
        size = 76
        bounds = QRectF(
            (self.width() - size) / 2,
            (self.height() - size) / 2,
            size,
            size,
        )
        painter.save()
        painter.setOpacity(opacity)
        painter.setPen(QPen(QColor("#f3f4f6"), 2))
        painter.setBrush(QColor(20, 22, 26, 190))
        painter.drawEllipse(bounds)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f3f4f6"))
        center = bounds.center()
        if self._indicator == "play":
            play = QPainterPath()
            play.moveTo(center.x() - 8, center.y() - 13)
            play.lineTo(center.x() + 13, center.y())
            play.lineTo(center.x() - 8, center.y() + 13)
            play.closeSubpath()
            painter.drawPath(play)
        else:
            painter.drawRoundedRect(
                QRectF(center.x() - 11, center.y() - 13, 7, 26),
                2,
                2,
            )
            painter.drawRoundedRect(
                QRectF(center.x() + 4, center.y() - 13, 7, 26),
                2,
                2,
            )
        painter.restore()

    def _paint_seek_indicator(self, painter: QPainter) -> None:
        elapsed = self._seek_clock.elapsed()
        progress = min(1.0, elapsed / self._SEEK_INDICATOR_DURATION_MS)
        direction = 1 if self._seek_indicator == "forward" else -1
        center_x = self._seek_center_x
        center_y = self.height() / 2
        size = 88
        bounds = QRectF(
            center_x - size / 2,
            center_y - size / 2,
            size,
            size,
        )
        arrow_offset = direction * (progress - 0.5) * 24

        painter.save()
        painter.setOpacity(1 - progress)
        painter.setPen(QPen(QColor(243, 244, 246, 115), 1))
        painter.setBrush(QColor(20, 22, 26, 205))
        painter.drawEllipse(bounds)

        painter.setPen(
            QPen(
                QColor("#f3f4f6"),
                3,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        arrow = QPainterPath()
        for index in range(2):
            tip_x = center_x + arrow_offset + direction * (index * 12)
            if direction > 0:
                arrow.moveTo(tip_x - 5, center_y - 18)
                arrow.lineTo(tip_x + 3, center_y - 10)
                arrow.lineTo(tip_x - 5, center_y - 2)
            else:
                arrow.moveTo(tip_x + 5, center_y - 18)
                arrow.lineTo(tip_x - 3, center_y - 10)
                arrow.lineTo(tip_x + 5, center_y - 2)
        painter.drawPath(arrow)

        painter.setPen(QColor("#f3f4f6"))
        painter.setFont(QFont(self.font().family(), 17, QFont.Weight.DemiBold))
        painter.drawText(
            QRectF(center_x - 30, center_y + 4, 60, 28),
            Qt.AlignmentFlag.AlignCenter,
            "5s",
        )
        painter.restore()

    def _update_indicator(self) -> None:
        if self._indicator_clock.elapsed() >= self._INDICATOR_DURATION_MS:
            self._indicator = ""
        if self._seek_clock.elapsed() >= self._SEEK_INDICATOR_DURATION_MS:
            self._seek_indicator = ""
        if not self._indicator and not self._seek_indicator:
            self._indicator_timer.stop()
        self.update()


class _MediaPreviewPage(QWidget):
    close_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._media_type: str | None = None
        self._media_path: Path | None = None
        self._surface = _MediaSurface(self)
        self._surface.single_clicked.connect(self._toggle_video)
        self._surface.seek_requested.connect(self._seek_video)

        self._video_sink = QVideoSink(self)
        self._video_sink.videoFrameChanged.connect(self._surface.set_video_frame)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(1.0)
        self._audio_output.setMuted(False)
        self._media_player = QMediaPlayer(self)
        self._media_player.setAudioOutput(self._audio_output)
        self._media_player.setVideoSink(self._video_sink)
        self._media_player.positionChanged.connect(self._surface.set_position)
        self._media_player.durationChanged.connect(self._surface.set_duration)
        self._media_player.errorOccurred.connect(self._handle_video_error)

        self._close_button = QToolButton(self)
        self._close_button.setObjectName("closeMediaPreviewButton")
        self._close_button.setText("×")
        self._close_button.setAccessibleName("Tutup pratinjau media")
        self._close_button.setFixedSize(30, 30)
        self._close_button.setStyleSheet(
            "QToolButton { color: #e5e7eb; background-color: #30343b; "
            "border: 1px solid #6b7280; border-radius: 15px; font-size: 19px; }"
            "QToolButton:hover { background-color: #3b414a; }"
        )
        self._close_button.clicked.connect(self.close_requested.emit)

    @property
    def media_path(self) -> Path | None:
        return self._media_path

    def set_media(self, attachment: _AttachedFile) -> None:
        self._media_player.stop()
        self._media_player.setSource(QUrl())
        self._media_type = attachment.media_type
        self._media_path = attachment.stored_path
        image = (
            QPixmap(str(attachment.stored_path)).toImage()
            if attachment.media_type == "image"
            else QImage()
        )
        self._surface.set_media(
            image,
            attachment.media_type == "video",
        )
        if attachment.media_type == "image" and image.isNull():
            self._surface.set_error(
                f"Tidak dapat memuat pratinjau gambar.\n{attachment.name}"
            )
        if attachment.media_type == "video":
            self._media_player.setSource(
                QUrl.fromLocalFile(str(attachment.stored_path))
            )
            self._media_player.play()

    def stop_media(self) -> None:
        self._media_player.stop()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._surface.setGeometry(self.rect())
        self._close_button.move(self.width() - 34, 4)
        self._close_button.raise_()
        super().resizeEvent(event)

    def _handle_video_error(self, _error, error_string: str) -> None:
        if self._media_type != "video":
            return
        self._surface.set_error(
            f"Tidak dapat memutar video.\n{self._media_path}\n{error_string}"
        )

    def _toggle_video(self) -> None:
        if self._media_type != "video":
            return
        if (
            self._media_player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        ):
            self._media_player.pause()
            self._surface.show_playback_indicator(False)
            return
        if (
            self._media_player.duration()
            and self._media_player.position() >= self._media_player.duration()
        ):
            self._media_player.setPosition(0)
        self._media_player.play()
        self._surface.show_playback_indicator(True)

    def _seek_video(self, offset: int) -> None:
        if self._media_type != "video":
            return
        target = max(0, self._media_player.position() + offset)
        duration = self._media_player.duration()
        if duration:
            target = min(duration, target)
        self._media_player.setPosition(target)
        self._surface.show_seek_indicator(offset)


class _AttachmentPreviewItem(QWidget):
    media_activated = Signal(str)
    remove_requested = Signal(str)
    _MEDIA_SIZE = 72

    def __init__(
        self,
        attachment: _AttachedFile,
        parent: QWidget,
        message_attachment: bool = False,
    ) -> None:
        super().__init__(parent)
        self._attachment = attachment
        self._message_attachment = message_attachment
        if message_attachment:
            object_name = (
                "messageMediaAttachment"
                if attachment.media_type
                else "messageFileAttachment"
            )
        else:
            object_name = (
                "mediaAttachmentPreview"
                if attachment.media_type
                else "fileAttachmentPreview"
            )
        self.setObjectName(object_name)
        self._thumbnail_player: QMediaPlayer | None = None
        self._thumbnail_sink: QVideoSink | None = None
        self._thumbnail_audio: QAudioOutput | None = None
        if message_attachment:
            self.setFixedSize(166, 84)
            if attachment.media_type and not attachment.expired:
                self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._thumbnail = (
                QPixmap(str(attachment.stored_path))
                if attachment.media_type == "image" and not attachment.expired
                else QPixmap()
            )
        elif attachment.media_type:
            self.setFixedSize(self._MEDIA_SIZE, self._MEDIA_SIZE)
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._thumbnail = (
                QPixmap(str(attachment.stored_path))
                if attachment.media_type == "image"
                else QPixmap()
            )
        else:
            self.setMinimumHeight(56)
            self.setMaximumHeight(56)
            self.setMinimumWidth(120)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            self._thumbnail = QPixmap()

        self._remove_button = QToolButton(self)
        self._remove_button.setObjectName("removeAttachmentButton")
        self._remove_button.setText("×")
        self._remove_button.setAccessibleName(
            f"Batalkan lampiran {attachment.name}"
        )
        self._remove_button.setFixedSize(20, 20)
        self._remove_button.setStyleSheet(
            "QToolButton { color: #f3f4f6; background-color: rgba(15, 17, 20, 220); "
            "border: 1px solid rgba(255, 255, 255, 45); border-radius: 10px; "
            "font-size: 15px; padding: 0; }"
            "QToolButton:hover { background-color: #b94343; }"
        )
        self._remove_button.clicked.connect(
            lambda _checked=False: self.remove_requested.emit(
                str(self._attachment.stored_path)
            )
        )
        if message_attachment:
            self._remove_button.hide()
        self._media_pressed = False
        if attachment.media_type == "video" and not attachment.expired:
            self._start_video_thumbnail()

    def stop_thumbnail(self) -> None:
        if self._thumbnail_player is not None:
            self._thumbnail_player.stop()
            self._thumbnail_player.setSource(QUrl())

    def _start_video_thumbnail(self) -> None:
        self._thumbnail_sink = QVideoSink(self)
        self._thumbnail_sink.videoFrameChanged.connect(
            self._set_video_thumbnail
        )
        self._thumbnail_audio = QAudioOutput(self)
        self._thumbnail_audio.setMuted(True)
        self._thumbnail_player = QMediaPlayer(self)
        self._thumbnail_player.setAudioOutput(self._thumbnail_audio)
        self._thumbnail_player.setVideoSink(self._thumbnail_sink)
        self._thumbnail_player.errorOccurred.connect(
            self._handle_video_thumbnail_error
        )
        self._thumbnail_player.setSource(
            QUrl.fromLocalFile(str(self._attachment.stored_path))
        )
        self._thumbnail_player.play()

    def _set_video_thumbnail(self, frame: QVideoFrame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        self._thumbnail = QPixmap.fromImage(image)
        self.stop_thumbnail()
        self.update()

    def _handle_video_thumbnail_error(self, _error, error_string: str) -> None:
        LOGGER.warning(
            "Gagal memuat thumbnail video %s: %s",
            self._attachment.stored_path,
            error_string,
        )
        self.stop_thumbnail()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            self._attachment.media_type
            and not self._attachment.expired
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._media_pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            self._media_pressed
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.media_activated.emit(str(self._attachment.stored_path))
            event.accept()
            self._media_pressed = False
            return
        self._media_pressed = False
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._remove_button.move(self.width() - 20, 0)
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._attachment.expired:
            self._paint_expired(painter)
        elif self._attachment.media_type:
            self._paint_media(painter)
        else:
            self._paint_file(painter)

    def _paint_media(self, painter: QPainter) -> None:
        bounds = self.rect().adjusted(0, 0, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor("#343942"))

        if not self._thumbnail.isNull():
            scaled = self._thumbnail.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            source = QRect(
                (scaled.width() - self.width()) // 2,
                (scaled.height() - self.height()) // 2,
                self.width(),
                self.height(),
            )
            painter.drawPixmap(bounds, scaled, source)
        elif self._attachment.media_type != "video":
            self._paint_image_placeholder(painter)
        if self._attachment.media_type == "video":
            self._paint_video_play_icon(painter)

        painter.fillRect(
            QRect(0, self.height() - 19, self.width(), 19),
            QColor(0, 0, 0, 165),
        )
        painter.setPen(QColor("#ffffff"))
        caption = QFontMetrics(painter.font()).elidedText(
            self._attachment.name,
            Qt.TextElideMode.ElideMiddle,
            self.width() - 8,
        )
        painter.drawText(
            QRect(4, self.height() - 19, self.width() - 8, 19),
            Qt.AlignmentFlag.AlignCenter,
            caption,
        )
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#59616b"), 1))
        painter.drawRoundedRect(bounds, 8, 8)

    def _paint_video_play_icon(self, painter: QPainter) -> None:
        center = self.rect().center()
        diameter = min(30, self.height() // 2)
        bounds = QRect(
            center.x() - diameter // 2,
            center.y() - diameter // 2,
            diameter,
            diameter,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 17, 20, 175))
        painter.drawEllipse(bounds)
        play = QPainterPath()
        play.moveTo(center.x() - 3, center.y() - 8)
        play.lineTo(center.x() - 3, center.y() + 8)
        play.lineTo(center.x() + 9, center.y())
        play.closeSubpath()
        painter.setBrush(QColor("#f3f4f6"))
        painter.drawPath(play)

    def _paint_image_placeholder(self, painter: QPainter) -> None:
        center = self.rect().center()
        left = center.x() - 15
        top = center.y() - 13
        painter.setPen(QPen(QColor("#cbd0d7"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRect(left, top, 30, 26), 3, 3)
        painter.drawEllipse(QRect(left + 6, top + 5, 5, 5))
        path = QPainterPath()
        path.moveTo(left + 2, top + 21)
        path.lineTo(left + 10, top + 13)
        path.lineTo(left + 15, top + 18)
        path.lineTo(left + 20, top + 14)
        path.lineTo(left + 28, top + 21)
        painter.drawPath(path)

    def _paint_file(self, painter: QPainter) -> None:
        bounds = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor("#4b525c"), 1))
        painter.setBrush(QColor("#30343b"))
        painter.drawRoundedRect(bounds, 8, 8)

        document = QPainterPath()
        document.moveTo(14, 14)
        document.lineTo(28, 14)
        document.lineTo(34, 20)
        document.lineTo(34, 42)
        document.lineTo(14, 42)
        document.closeSubpath()
        painter.setPen(QPen(QColor("#cbd0d7"), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(document)
        painter.drawLine(28, 14, 28, 20)
        painter.drawLine(28, 20, 34, 20)
        painter.drawLine(19, 27, 29, 27)
        painter.drawLine(19, 32, 29, 32)

        painter.setFont(QFont(self.font().family(), 10))
        painter.setPen(QColor("#e5e7eb"))
        name = QFontMetrics(painter.font()).elidedText(
            self._attachment.name,
            Qt.TextElideMode.ElideMiddle,
            max(1, self.width() - 78),
        )
        painter.drawText(
            QRect(44, 0, max(1, self.width() - 76), self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            name,
        )

    def _paint_expired(self, painter: QPainter) -> None:
        bounds = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor("#6b5b39"), 1))
        painter.setBrush(QColor("#30343b"))
        painter.drawRoundedRect(bounds, 8, 8)

        center_x = 24
        center_y = self.height() // 2
        warning = QPainterPath()
        warning.moveTo(center_x, center_y - 15)
        warning.lineTo(center_x + 16, center_y + 13)
        warning.lineTo(center_x - 16, center_y + 13)
        warning.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#fbbf24"))
        painter.drawPath(warning)
        painter.setPen(QColor("#30343b"))
        painter.setFont(QFont(self.font().family(), 14, QFont.Weight.Bold))
        painter.drawText(
            QRect(center_x - 4, center_y - 9, 8, 20),
            Qt.AlignmentFlag.AlignCenter,
            "!",
        )

        painter.setPen(QColor("#e5e7eb"))
        painter.setFont(QFont(self.font().family(), 9))
        caption = QFontMetrics(painter.font()).elidedText(
            f"{self._attachment.name} (kedaluwarsa)",
            Qt.TextElideMode.ElideMiddle,
            max(1, self.width() - 48),
        )
        painter.drawText(
            QRect(44, 0, max(1, self.width() - 50), self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            caption,
        )


class _ChatInputFrame(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#59616b"), 1))
        painter.setBrush(QColor("#2b2f36"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 20, 20)


class _AnimatedPlusButton(QToolButton):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._rotation = 0.0
        self._rotation_animation = QPropertyAnimation(self, b"rotation", self)
        self._rotation_animation.setDuration(180)
        self._rotation_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setMouseTracking(True)

    @Property(float)
    def rotation(self) -> float:
        return self._rotation

    @rotation.setter
    def rotation(self, value: float) -> None:
        self._rotation = value
        self.update()

    def set_menu_open(self, opened: bool) -> None:
        self._rotation_animation.stop()
        self._rotation_animation.setStartValue(self._rotation)
        self._rotation_animation.setEndValue(45.0 if opened else 0.0)
        self._rotation_animation.start()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3a3f46"))
            painter.drawEllipse(self.rect().adjusted(2, 2, -2, -2))

        painter.translate(self.rect().center())
        painter.rotate(self._rotation)
        painter.setPen(
            QPen(
                QColor("#aeb4bd"),
                2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(-6, 0, 6, 0)
        painter.drawLine(0, -6, 0, 6)


class _ChatInput(QPlainTextEdit):
    image_pasted = Signal(QImage)
    submit_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasImage():
            image_data = source.imageData()
            if isinstance(image_data, QPixmap):
                image_data = image_data.toImage()
            if isinstance(image_data, QImage) and not image_data.isNull():
                self.image_pasted.emit(image_data.copy())
                return
        super().insertFromMimeData(source)


class ChatPanel(QWidget):
    visibility_changed = Signal(bool)

    def __init__(
        self,
        api_keys: tuple[str, ...] = (),
        database: ChatDatabase | None = None,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("chatPanel")
        self.setWindowTitle("Panel percakapan")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(420, 560)
        self._drag_offset: QPoint | None = None
        self._has_been_positioned = False
        self._draft_cache = tempfile.TemporaryDirectory(prefix="speech-ai-drafts-")
        self._attachment_directory = Path(self._draft_cache.name)
        self._attachments: list[_AttachedFile] = []
        self._message_media_files: dict[Path, _AttachedFile] = {}
        self._api_keys = api_keys
        self._database = database or ChatDatabase()
        self._owns_database = database is None
        self._chat_worker: ChatResponseWorker | None = None
        self._is_sending = False
        self._pending_scroll_to_bottom = False

        self._build_ui()
        self._expire_attachments()
        self._load_history()
        self._install_drag_filters(self._header)
        self._install_attachment_drop_targets()
        self._drop_overlay_hide_timer = QTimer(self)
        self._drop_overlay_hide_timer.setSingleShot(True)
        self._drop_overlay_hide_timer.setInterval(100)
        self._drop_overlay_hide_timer.timeout.connect(self._drop_overlay.hide)
        self._attachment_cleanup_timer = QTimer(self)
        self._attachment_cleanup_timer.setInterval(60_000)
        self._attachment_cleanup_timer.timeout.connect(self._expire_attachments)
        self._attachment_cleanup_timer.start()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel_path = QPainterPath()
        panel_path.addRoundedRect(
            self.rect().adjusted(0, 0, -1, -1),
            16,
            16,
        )
        painter.setClipPath(panel_path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#202327"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 16, 16)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#525861"), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 16, 16)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._pending_scroll_to_bottom = True
        self.layout().activate()
        input_layout = self._input_frame.layout()
        margins = input_layout.contentsMargins()
        available_width = (
            self._input_frame.width() - margins.left() - margins.right()
        )
        self._input.setMinimumWidth(available_width + 4)
        self._refresh_attachment_preview_size()
        self._adjust_input_height()
        self._position_scroll_to_latest_button()
        QTimer.singleShot(0, self._scroll_to_bottom)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._drag_offset = None
        self._drop_overlay_hide_timer.stop()
        self._drop_overlay.hide()
        self.visibility_changed.emit(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_footer"):
            self.layout().activate()
            self._position_footer()
            self._position_scroll_to_latest_button()
        if hasattr(self, "_drop_overlay"):
            self._drop_overlay.setGeometry(self.rect().adjusted(6, 6, -6, -6))

    def dragEnterEvent(self, event) -> None:
        self._handle_attachment_drop_event(event)

    def dragMoveEvent(self, event) -> None:
        self._handle_attachment_drop_event(event)

    def dragLeaveEvent(self, event) -> None:
        self._drop_overlay_hide_timer.start()
        event.accept()

    def dropEvent(self, event) -> None:
        self._handle_attachment_drop_event(event)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self._scroll_area.viewport()
            and event.type() == QEvent.Type.Wheel
        ):
            self._cancel_pending_scroll()
        if self._handle_attachment_drop_event(event):
            return True
        if watched in self._attachment_drop_targets:
            return super().eventFilter(watched, event)
        if isinstance(watched, (QPlainTextEdit, QToolButton)):
            return super().eventFilter(watched, event)

        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = (
                    mouse_event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                return True
        elif event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
            mouse_event = event
            if mouse_event.buttons() & Qt.MouseButton.LeftButton:
                position = mouse_event.globalPosition().toPoint() - self._drag_offset
                self.move(self._constrained_position(position))
                return True
        elif event.type() == QEvent.Type.MouseButtonRelease:
            mouse_event = event
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                if self._drag_offset is not None:
                    self._snap_to_available_geometry()
                self._drag_offset = None
                return True
        return super().eventFilter(watched, event)

    def _available_geometry_for_position(self, position: QPoint) -> QRect:
        center = position + QPoint(self.width() // 2, self.height() // 2)
        screen = QGuiApplication.screenAt(center)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect()

    def _constrained_position(self, position: QPoint) -> QPoint:
        geometry = self._available_geometry_for_position(position)
        if geometry.isNull():
            return position
        minimum_x = geometry.left()
        minimum_y = geometry.top()
        maximum_x = max(minimum_x, geometry.right() - self.width() + 1)
        maximum_y = max(minimum_y, geometry.bottom() - self.height() + 1)
        return QPoint(
            min(max(position.x(), minimum_x), maximum_x),
            min(max(position.y(), minimum_y), maximum_y),
        )

    def _snap_to_available_geometry(self) -> None:
        position = self._constrained_position(self.pos())
        geometry = self._available_geometry_for_position(position)
        if geometry.isNull():
            return
        left = geometry.left()
        top = geometry.top()
        right = max(left, geometry.right() - self.width() + 1)
        bottom = max(top, geometry.bottom() - self.height() + 1)
        x, y = position.x(), position.y()
        if abs(x - left) <= PANEL_SNAP_DISTANCE:
            x = left
        elif abs(x - right) <= PANEL_SNAP_DISTANCE:
            x = right
        if abs(y - top) <= PANEL_SNAP_DISTANCE:
            y = top
        elif abs(y - bottom) <= PANEL_SNAP_DISTANCE:
            y = bottom
        self.move(QPoint(x, y))

    def _install_drag_filters(self, widget: QWidget) -> None:
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            if not isinstance(child, (QPlainTextEdit, QToolButton)):
                child.installEventFilter(self)

    def _install_attachment_drop_targets(self) -> None:
        targets = (
            self._body_stack,
            self._scroll_area,
            self._scroll_area.viewport(),
            self._scroll_area.widget(),
            self._footer,
            self._input_frame,
            self._attachment_preview,
            self._attachment_preview.viewport(),
            self._input,
            self._drop_overlay,
        )
        self._attachment_drop_targets = set(targets)
        self.setAcceptDrops(True)
        for target in targets:
            target.setAcceptDrops(True)
            target.installEventFilter(self)

    def _handle_attachment_drop_event(self, event) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.DragLeave:
            self._drop_overlay_hide_timer.start()
            return True
        if event_type not in (
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
        ):
            return False
        mime_data = event.mimeData()
        has_local_files = any(url.isLocalFile() for url in mime_data.urls())
        if not mime_data.hasImage() and not has_local_files:
            if self._drop_overlay.isVisible():
                self._drop_overlay_hide_timer.start()
            return False
        self._drop_overlay_hide_timer.stop()
        if event_type == QEvent.Type.Drop:
            self._process_dropped_attachments(mime_data)
            self._drop_overlay.hide()
        else:
            self._drop_overlay.raise_()
            self._drop_overlay.show()
        event.acceptProposedAction()
        return True

    def _process_dropped_attachments(self, mime_data: QMimeData) -> None:
        sources = [
            Path(url.toLocalFile())
            for url in mime_data.urls()
            if url.isLocalFile()
        ]
        changed = self._copy_local_attachments(sources)
        if mime_data.hasImage():
            image = self._image_from_mime_data(mime_data)
            if not image.isNull():
                changed = (
                    self._store_image_attachment(image, "dropped-image.png")
                    or changed
                )
        if changed:
            self._update_attachment_preview()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self._main_layout = layout
        layout.setContentsMargins(8, 8, 8, 12)
        layout.setSpacing(0)

        header = QWidget(self)
        self._header = header
        header.setObjectName("chatHeader")
        header.setStyleSheet(
            "QWidget#chatHeader { background-color: #24272d; "
            "border-bottom: 1px solid #3a3f46; "
            "border-top-left-radius: 8px; border-top-right-radius: 8px; }"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 2, 8, 8)
        title = QLabel("Chat AI", header)
        title.setStyleSheet("color: #f3f4f6; font-size: 15px; font-weight: 600;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self._close_button = QToolButton(header)
        self._close_button.setObjectName("closePanelButton")
        self._close_button.setText("×")
        self._close_button.setAccessibleName("Tutup panel")
        self._close_button.setFixedSize(30, 30)
        self._close_button.setStyleSheet(
            "QToolButton { color: #e5e7eb; background-color: transparent; "
            "border: 1px solid #6b7280; border-radius: 15px; font-size: 19px; }"
            "QToolButton:hover { background-color: #3b414a; }"
        )
        self._close_button.clicked.connect(self.hide)
        header_layout.addWidget(self._close_button)
        layout.addWidget(header)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setStyleSheet(
            "QScrollArea { background: #202327; border: none; }"
            "QScrollArea > QWidget > QWidget { background: #202327; }"
            "QScrollBar:vertical { width: 8px; background: transparent; margin: 0; }"
            "QScrollBar::handle:vertical { background: #59616b; "
            "border-radius: 4px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { "
            "height: 0; border: none; background: transparent; }"
            "QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical { "
            "width: 0; height: 0; background: transparent; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { "
            "background: transparent; }"
        )
        self._scroll_area.verticalScrollBar().rangeChanged.connect(
            self._handle_scroll_range_changed
        )
        self._scroll_area.verticalScrollBar().actionTriggered.connect(
            self._cancel_pending_scroll
        )
        self._scroll_area.verticalScrollBar().sliderPressed.connect(
            self._cancel_pending_scroll
        )

        messages = QWidget()
        messages.setStyleSheet("background: #202327;")
        message_layout = QVBoxLayout(messages)
        message_layout.setContentsMargins(0, 8, 0, 8)
        message_layout.setSpacing(4)
        message_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._message_layout = message_layout

        self._scroll_area.setWidget(messages)
        self._body_stack = QStackedWidget(self)
        self._body_stack.addWidget(self._scroll_area)
        self._media_preview_page = _MediaPreviewPage(self._body_stack)
        self._media_preview_page.close_requested.connect(
            self._close_media_preview
        )
        self._body_stack.addWidget(self._media_preview_page)
        layout.addWidget(self._body_stack, 1)

        self._composer_backdrop = QWidget(self)
        self._composer_backdrop.setObjectName("composerBackdrop")
        self._composer_backdrop.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._composer_backdrop.setStyleSheet(
            "QWidget#composerBackdrop { background-color: #24272d; "
            "border-bottom-left-radius: 16px; "
            "border-bottom-right-radius: 16px; }"
        )
        self._composer_backdrop.hide()

        footer = QWidget(self)
        self._footer = footer
        footer.setObjectName("chatFooter")
        footer.setStyleSheet("background: transparent;")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(0)

        input_frame = _ChatInputFrame(footer)
        self._input_frame = input_frame
        input_frame.setObjectName("chatInputFrame")
        input_frame_layout = QVBoxLayout(input_frame)
        input_frame_layout.setContentsMargins(6, 12, 6, 6)
        input_frame_layout.setSpacing(4)

        self._attachment_preview = QScrollArea(input_frame)
        self._attachment_preview.setObjectName("attachmentPreview")
        self._attachment_preview.setWidgetResizable(False)
        self._attachment_preview.setFrameShape(QScrollArea.Shape.NoFrame)
        self._attachment_preview.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._attachment_preview.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._attachment_preview.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:horizontal { height: 8px; background: #24272d; margin: 0; }"
            "QScrollBar::handle:horizontal { background: #59616b; border-radius: 4px; "
            "min-width: 24px; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { "
            "width: 0; }"
        )
        self._attachment_preview_content = QWidget()
        self._attachment_preview_content.setObjectName(
            "attachmentPreviewContent"
        )
        self._attachment_preview_content.setFixedHeight(
            _AttachmentPreviewItem._MEDIA_SIZE
        )
        self._attachment_preview_layout = QHBoxLayout(
            self._attachment_preview_content
        )
        self._attachment_preview_layout.setContentsMargins(0, 0, 0, 0)
        self._attachment_preview_layout.setSpacing(6)
        self._attachment_preview_layout.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._attachment_preview.setWidget(self._attachment_preview_content)
        self._attachment_preview.setFixedHeight(
            _AttachmentPreviewItem._MEDIA_SIZE
        )
        self._attachment_preview.hide()
        input_frame_layout.addWidget(self._attachment_preview)

        self._input = _ChatInput(input_frame)
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText("Tulis pesan...")
        self._input.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._input.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._input.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._input.setContentsMargins(0, 0, 0, 0)
        self._input.setViewportMargins(0, 0, 0, 0)
        self._input.document().setDocumentMargin(0)
        self._input.setFixedHeight(self._input_line_height() + 8)
        self._input.setStyleSheet(
            "QPlainTextEdit { color: #e5e7eb; background: transparent; border: none; "
            "padding: 0; selection-background-color: #59616b; }"
        )
        input_frame_layout.addWidget(self._input)

        input_actions = QHBoxLayout()
        input_actions.setContentsMargins(0, 0, 0, 0)
        input_actions.setSpacing(0)

        self._attach_button = _AnimatedPlusButton(input_frame)
        self._attach_button.setObjectName("attachButton")
        self._attach_button.setAccessibleName("Buka menu lampiran")
        self._attach_button.setFixedSize(34, 34)
        self._attach_button.setStyleSheet(
            "QToolButton { background: transparent; border: none; }"
        )
        self._create_attachment_menu()
        input_actions.addWidget(self._attach_button)
        input_actions.addStretch()

        self._submit_button = QToolButton(input_frame)
        self._submit_button.setObjectName("submitButton")
        self._submit_button.setAccessibleName("Kirim pesan")
        self._submit_button.setIcon(self._send_icon("#777f8a"))
        self._submit_button.setIconSize(QSize(18, 18))
        self._submit_button.setEnabled(False)
        self._submit_button.setFixedSize(34, 34)
        self._submit_button.setStyleSheet(
            "QToolButton { background-color: #59616b; border: none; "
            "border-radius: 17px; }"
            "QToolButton:disabled { background-color: #373c43; }"
        )
        input_actions.addWidget(self._submit_button)
        input_frame_layout.addLayout(input_actions)
        footer_layout.addWidget(input_frame)

        self._scroll_to_latest_button = QToolButton(self)
        self._scroll_to_latest_button.setObjectName("scrollToLatestButton")
        self._scroll_to_latest_button.setAccessibleName("Gulir ke pesan terbaru")
        self._scroll_to_latest_button.setFixedSize(32, 32)
        self._scroll_to_latest_button.setIcon(self._scroll_down_icon())
        self._scroll_to_latest_button.setIconSize(QSize(18, 18))
        self._scroll_to_latest_button.setStyleSheet(
            "QToolButton { color: #e5e7eb; background-color: #24272d; "
            "border: 1px solid #59616b; border-radius: 16px; padding: 0; }"
            "QToolButton:hover { background-color: #3a3f46; }"
        )
        self._scroll_to_latest_button.clicked.connect(self._jump_to_latest)
        self._scroll_to_latest_button.hide()
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.valueChanged.connect(self._update_scroll_to_latest_button)
        scrollbar.rangeChanged.connect(self._update_scroll_to_latest_button)

        self._drop_overlay = QLabel(
            "Lepaskan file untuk melampirkan",
            self,
        )
        self._drop_overlay.setObjectName("attachmentDropOverlay")
        self._drop_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_overlay.setWordWrap(True)
        self._drop_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._drop_overlay.setStyleSheet(
            "QLabel#attachmentDropOverlay { color: #f3f4f6; "
            "background-color: rgba(25, 30, 40, 220); "
            "border: 2px dashed #9aaeff; border-radius: 16px; "
            "font-size: 16px; font-weight: 600; padding: 18px; }"
        )
        self._drop_overlay.setGeometry(self.rect().adjusted(6, 6, -6, -6))
        self._drop_overlay.hide()

        self._copy_toast = QLabel("Message copied", self)
        self._copy_toast.setObjectName("copyMessageToast")
        self._copy_toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._copy_toast.setFixedHeight(30)
        self._copy_toast.setStyleSheet(
            "QLabel#copyMessageToast { color: #f3f4f6; "
            "background-color: #3a3f46; border: 1px solid #59616b; "
            "border-radius: 15px; padding: 0 12px; }"
        )
        self._copy_toast.hide()
        self._copy_toast_timer = QTimer(self)
        self._copy_toast_timer.setSingleShot(True)
        self._copy_toast_timer.setInterval(2200)
        self._copy_toast_timer.timeout.connect(self._copy_toast.hide)

        self._attach_button.clicked.connect(self._toggle_attachment_menu)
        self._submit_button.clicked.connect(self._submit_message)
        self._input.submit_requested.connect(self._submit_message)
        self._input.image_pasted.connect(self._attach_pasted_image)
        self._input.textChanged.connect(self._update_submit_button)
        self._input.textChanged.connect(self._schedule_input_height)
        self._position_footer()
        self._position_scroll_to_latest_button()

    @staticmethod
    def _scroll_down_icon() -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor("#e5e7eb"),
                2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        path = QPainterPath()
        path.moveTo(12, 4)
        path.lineTo(12, 19)
        path.moveTo(6, 13)
        path.lineTo(12, 19)
        path.lineTo(18, 13)
        painter.drawPath(path)
        painter.end()
        return QIcon(pixmap)

    def _position_scroll_to_latest_button(self) -> None:
        if not hasattr(self, "_scroll_to_latest_button"):
            return
        top_left = self._input_frame.mapTo(self, QPoint(0, 0))
        self._scroll_to_latest_button.move(
            top_left.x()
            + self._input_frame.width()
            - self._scroll_to_latest_button.width(),
            top_left.y() - self._scroll_to_latest_button.height() - 6,
        )
        self._scroll_to_latest_button.raise_()

    def _position_footer(self) -> None:
        if not hasattr(self, "_footer"):
            return
        margins = self._main_layout.contentsMargins()
        footer_height = max(
            self._footer.minimumHeight(),
            self._footer.sizeHint().height(),
        )
        footer_bottom = self.height() - margins.bottom()
        self._footer.setGeometry(
            margins.left(),
            footer_bottom - footer_height,
            max(0, self.width() - margins.left() - margins.right()),
            footer_height,
        )
        scroll_viewport = self._scroll_area.viewport()
        viewport_bottom = scroll_viewport.mapTo(
            self,
            QPoint(0, scroll_viewport.height()),
        ).y()
        footer_top = self._footer.mapTo(self, QPoint(0, 0)).y()
        overlap = (
            max(0, viewport_bottom - footer_top)
            if self._body_stack.currentWidget() is self._scroll_area
            else 0
        )
        margins = self._message_layout.contentsMargins()
        bottom_margin = 8 + overlap
        if margins.bottom() != bottom_margin:
            scrollbar = self._scroll_area.verticalScrollBar()
            should_scroll_to_bottom = (
                self._pending_scroll_to_bottom
                or scrollbar.value() >= scrollbar.maximum()
            )
            self._message_layout.setContentsMargins(
                margins.left(),
                margins.top(),
                margins.right(),
                bottom_margin,
            )
            self._scroll_area.widget().updateGeometry()
            if should_scroll_to_bottom:
                self._pending_scroll_to_bottom = True
                self._scroll_area.updateGeometry()
                self._scroll_to_bottom()
        input_top = self._input_frame.mapTo(self, QPoint(0, 0)).y()
        backdrop_top = input_top + self._input_frame.height() // 2
        self._composer_backdrop.setGeometry(
            1,
            backdrop_top,
            max(0, self.width() - 2),
            max(0, self.height() - backdrop_top - 1),
        )
        if self._body_stack.currentWidget() is self._scroll_area:
            self._footer.raise_()
            self._composer_backdrop.show()
            self._composer_backdrop.stackUnder(self._footer)
        else:
            self._composer_backdrop.hide()

    def _update_scroll_to_latest_button(self, *_args: object) -> None:
        if (
            self._body_stack.currentWidget() is not self._scroll_area
            or not self._footer.isVisible()
        ):
            self._scroll_to_latest_button.hide()
            return
        scrollbar = self._scroll_area.verticalScrollBar()
        if scrollbar.maximum() > scrollbar.value():
            self._position_scroll_to_latest_button()
            self._scroll_to_latest_button.show()
            self._scroll_to_latest_button.raise_()
        else:
            self._scroll_to_latest_button.hide()

    def _jump_to_latest(self) -> None:
        self._cancel_pending_scroll()
        self._scroll_to_bottom()

    def _create_attachment_menu(self) -> None:
        self._attachment_menu = QMenu(self)
        self._attachment_menu.setObjectName("attachmentMenu")
        self._attachment_menu.setStyleSheet(
            "QMenu { color: #e5e7eb; background-color: #2b2f36; "
            "border: 1px solid #59616b; border-radius: 8px; padding: 4px; }"
            "QMenu::item { padding: 7px 22px 7px 8px; border-radius: 5px; }"
            "QMenu::item:selected { background-color: #3a3f46; }"
        )
        self._camera_action = self._attachment_menu.addAction(
            self._attachment_menu_icon("camera"),
            "Camera",
        )
        self._camera_action.setObjectName("cameraAttachmentAction")
        self._screenshot_action = self._attachment_menu.addAction(
            self._attachment_menu_icon("screenshot"),
            "Screenshot",
        )
        self._screenshot_action.setObjectName("screenshotAttachmentAction")
        self._media_action = self._attachment_menu.addAction(
            self._attachment_menu_icon("photo"),
            "Photo",
        )
        self._media_action.setObjectName("mediaAttachmentAction")
        self._file_action = self._attachment_menu.addAction(
            self._attachment_menu_icon("file"),
            "File",
        )
        self._file_action.setObjectName("fileAttachmentAction")
        self._media_action.triggered.connect(self._select_media_attachments)
        self._file_action.triggered.connect(self._select_attachments)
        self._attachment_menu.aboutToShow.connect(
            lambda: self._attach_button.set_menu_open(True)
        )
        self._attachment_menu.aboutToHide.connect(
            lambda: self._attach_button.set_menu_open(False)
        )

    @staticmethod
    def _attachment_menu_icon(name: str) -> QIcon:
        drawings = {
            "camera": (
                '<path d="M4 8h4l2-2h4l2 2h4v11H4z"/>'
                '<circle cx="12" cy="13" r="3"/>'
            ),
            "screenshot": (
                '<rect x="3" y="4" width="18" height="14" rx="2"/>'
                '<path d="M8 21h8M12 18v3"/>'
            ),
            "photo": (
                '<rect x="3" y="4" width="18" height="16" rx="2"/>'
                '<circle cx="9" cy="10" r="1.5"/>'
                '<path d="m4 17 5-5 3 3 3-4 5 6"/>'
            ),
            "file": (
                '<path d="M6 3h8l4 4v14H6z"/>'
                '<path d="M14 3v5h5M9 13h6M9 17h6"/>'
            ),
        }
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            'viewBox="0 0 24 24" fill="none" stroke="#aeb4bd" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            f"{drawings[name]}</svg>"
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)

    def _toggle_attachment_menu(self) -> None:
        if self._attachment_menu.isVisible():
            self._attachment_menu.hide()
            return
        self._attachment_menu.adjustSize()
        menu_position = self._attach_button.mapToGlobal(
            QPoint(0, -self._attachment_menu.sizeHint().height() - 4)
        )
        self._attachment_menu.popup(menu_position)

    def _send_icon(self, color: str) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor(color),
                2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        path = QPainterPath()
        path.moveTo(5, 12)
        path.lineTo(19, 12)
        path.moveTo(13, 6)
        path.lineTo(19, 12)
        path.lineTo(13, 18)
        painter.drawPath(path)
        painter.end()
        return QIcon(pixmap)

    def _update_submit_button(self) -> None:
        text = self._input.toPlainText()
        has_content = bool(text.strip()) or bool(self._attachments)
        self._submit_button.setEnabled(has_content and not self._is_sending)
        color = "#f3f4f6" if has_content and not self._is_sending else "#777f8a"
        self._submit_button.setIcon(self._send_icon(color))

    def _load_history(self) -> None:
        for message in self._database.load_messages():
            self._add_message_row(message)

    def _prepare_message_attachments(
        self,
        message: StoredMessage,
    ) -> tuple[_AttachedFile, ...]:
        attachments: list[_AttachedFile] = []
        for stored_attachment in message.attachments:
            media_type = (
                "image"
                if stored_attachment.mime_type.startswith("image/")
                else "video"
                if stored_attachment.mime_type.startswith("video/")
                else None
            )
            path = Path(stored_attachment.name)
            expired = not stored_attachment.available
            if media_type and not expired:
                active_attachment = self._database.read_active_attachment(
                    stored_attachment.id
                )
                if active_attachment is None or active_attachment.data is None:
                    expired = True
                else:
                    safe_name = Path(stored_attachment.name).name or "attachment"
                    path = (
                        self._attachment_directory
                        / f"message-{message.id}-{stored_attachment.id}--{safe_name}"
                    )
                    try:
                        path.write_bytes(active_attachment.data)
                    except OSError:
                        LOGGER.exception(
                            "Gagal menyiapkan pratinjau lampiran %s",
                            stored_attachment.name,
                        )
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            LOGGER.exception(
                                "Gagal membersihkan pratinjau lampiran %s",
                                path,
                            )
                        expired = True
                    else:
                        preview_attachment = _AttachedFile(
                            name=stored_attachment.name,
                            stored_path=path,
                            media_type=media_type,
                            attached_at_utc=stored_attachment.attached_at_utc,
                            attachment_id=stored_attachment.id,
                        )
                        self._message_media_files[path] = preview_attachment
            attachments.append(
                _AttachedFile(
                    name=stored_attachment.name,
                    stored_path=path,
                    media_type=media_type,
                    attached_at_utc=stored_attachment.attached_at_utc,
                    attachment_id=stored_attachment.id,
                    expired=expired,
                )
            )
        return tuple(attachments)

    def _add_message_row(self, message: StoredMessage) -> None:
        if message.role == "user":
            attachments = self._prepare_message_attachments(message)
            row = _UserMessageRow(
                message,
                self._scroll_area.widget(),
                attachments,
            )
            row.message_copied.connect(self._show_copy_toast)
            row.media_activated.connect(self._show_media_preview)
        else:
            row = _ConversationMessageRow(message, self._scroll_area.widget())
        self._pending_scroll_to_bottom = True
        self._message_layout.addWidget(row)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _handle_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._pending_scroll_to_bottom:
            self._scroll_area.verticalScrollBar().setValue(maximum)

    def _cancel_pending_scroll(self, *_args: object) -> None:
        self._pending_scroll_to_bottom = False

    def _show_copy_toast(self) -> None:
        self._copy_toast.setFixedWidth(
            self._copy_toast.fontMetrics().horizontalAdvance(
                self._copy_toast.text()
            )
            + 28
        )
        self._copy_toast.move(
            (self.width() - self._copy_toast.width()) // 2,
            max(8, self._footer.y() - self._copy_toast.height() - 8),
        )
        self._copy_toast.raise_()
        self._copy_toast.show()
        self._copy_toast_timer.start()

    def _submit_message(self) -> None:
        if self._is_sending:
            return
        content = self._input.toPlainText()
        if not content.strip() and not self._attachments:
            return
        if not self._api_keys:
            QMessageBox.warning(
                self,
                "API key Gemini tidak ditemukan",
                "Isi GEMINI_API_KEYS di file .env sebelum mengirim pesan.",
            )
            return

        pending_attachments = tuple(
            PendingAttachment(
                name=attachment.name,
                path=attachment.stored_path,
                attached_at_utc=attachment.attached_at_utc,
            )
            for attachment in self._attachments
        )
        try:
            message = self._database.append_user_message(
                content,
                pending_attachments,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(
                self,
                "Pesan gagal disimpan",
                f"Pesan tidak dikirim karena gagal disimpan ke database:\n{error}",
            )
            return

        cleanup_errors: list[str] = []
        for attachment in self._attachments:
            try:
                attachment.stored_path.unlink(missing_ok=True)
            except OSError as error:
                LOGGER.exception(
                    "Pesan tersimpan, tetapi cache lampiran gagal dihapus %s",
                    attachment.stored_path,
                )
                cleanup_errors.append(f"{attachment.name}: {error}")
        self._attachments.clear()
        self._input.clear()
        self._update_attachment_preview()
        self._add_message_row(message)
        if cleanup_errors:
            QMessageBox.warning(
                self,
                "Cache lampiran belum terhapus",
                "\n".join(cleanup_errors),
            )

        self._is_sending = True
        self._update_submit_button()
        worker = ChatResponseWorker(self._api_keys, self._database)
        worker.reply_received.connect(self._handle_chat_reply)
        worker.error_occurred.connect(self._handle_chat_error)
        worker.finished.connect(self._on_chat_worker_finished)
        self._chat_worker = worker
        worker.start()

    def _handle_chat_reply(self, content: str) -> None:
        try:
            message = self._database.append_model_message(content)
        except (ValueError, sqlite3.Error) as error:
            QMessageBox.warning(
                self,
                "Balasan gagal disimpan",
                f"Gemini membalas, tetapi gagal menyimpan balasan:\n{error}",
            )
            return
        self._add_message_row(message)

    def _handle_chat_error(self, message: str) -> None:
        QMessageBox.warning(self, "Chat Gemini gagal", message)

    def _on_chat_worker_finished(self) -> None:
        worker = self._chat_worker
        self._chat_worker = None
        self._is_sending = False
        self._update_submit_button()
        if worker is not None:
            worker.deleteLater()

    def _input_line_height(self) -> int:
        return self._input.fontMetrics().lineSpacing()

    def _schedule_input_height(self) -> None:
        QTimer.singleShot(0, self._adjust_input_height)

    def _adjust_input_height(self) -> None:
        line_height = self._input_line_height()
        minimum_height = line_height + 8
        maximum_height = line_height * 4 + 8
        document_lines = self._input.document().documentLayout().documentSize().height()
        content_height = max(
            line_height,
            int(document_lines + 0.5) * line_height,
        )
        input_height = min(
            maximum_height,
            max(minimum_height, content_height + 8),
        )
        self._input.setFixedHeight(input_height)
        attachment_extra = (
            self._attachment_preview.height() + 4
            if self._attachment_preview.isVisible()
            else 0
        )
        self._input_frame.setFixedHeight(input_height + 56 + attachment_extra)
        footer = self._input_frame.parentWidget()
        footer.setMinimumHeight(input_height + 56 + attachment_extra)
        self.layout().activate()
        self._position_footer()
        self._position_scroll_to_latest_button()
        self.update()

    def _select_attachments(self) -> None:
        selected_files, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Pilih file lampiran",
        )
        self._add_selected_attachments(selected_files)

    def _select_media_attachments(self) -> None:
        selected_files, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Pilih foto atau video",
            "",
            "Media (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.mp4 *.mov *.mkv "
            "*.webm *.avi *.m4v *.3gp);;All files (*)",
        )
        self._add_selected_attachments(selected_files)

    def _add_selected_attachments(self, selected_files: list[str]) -> None:
        if not selected_files:
            return

        self._copy_local_attachments(
            [Path(selected_file) for selected_file in selected_files]
        )
        self._update_attachment_preview()

    def _copy_local_attachments(self, sources: list[Path]) -> bool:
        changed = False
        warnings: list[str] = []
        for source in sources:
            if len(self._attachments) >= MAX_ATTACHMENTS_PER_MESSAGE:
                warnings.append(
                    f"Maksimal {MAX_ATTACHMENTS_PER_MESSAGE} lampiran per pesan."
                )
                break

            stored_file: Path | None = None
            try:
                if source.stat().st_size > MAX_ATTACHMENT_SIZE_BYTES:
                    warnings.append(f"{source.name}: ukuran melebihi 18 MB.")
                    continue
                stored_file = copy_attachment(
                    source,
                    self._attachment_directory,
                )
                stored_size = stored_file.stat().st_size
                content_sha256 = self._file_sha256(stored_file)
            except OSError as error:
                if stored_file is not None:
                    try:
                        stored_file.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.exception(
                            "Gagal membersihkan lampiran yang ditolak %s",
                            stored_file,
                        )
                warnings.append(f"{source.name}: tidak dapat disalin ({error}).")
                continue
            if stored_size > MAX_ATTACHMENT_SIZE_BYTES:
                try:
                    stored_file.unlink()
                except OSError as error:
                    LOGGER.exception(
                        "Gagal menghapus salinan lampiran yang terlalu besar %s",
                        stored_file,
                    )
                    warnings.append(
                        f"{source.name}: ukuran melebihi 18 MB dan salinannya "
                        f"gagal dihapus ({error})."
                    )
                else:
                    warnings.append(f"{source.name}: ukuran melebihi 18 MB.")
                continue
            if self._has_duplicate_attachment(content_sha256):
                try:
                    stored_file.unlink()
                except OSError as error:
                    LOGGER.exception(
                        "Gagal menghapus salinan lampiran duplikat %s",
                        stored_file,
                    )
                    warnings.append(
                        f"{source.name}: duplikat, tetapi salinan sementara gagal "
                        f"dihapus ({error})."
                    )
                else:
                    warnings.append(f"{source.name}: duplikat, dilewati.")
                continue
            media_type = mimetypes.guess_type(source.name)[0] or ""
            if media_type.startswith("image/"):
                media_kind = "image"
            elif media_type.startswith("video/"):
                media_kind = "video"
            else:
                media_kind = None
            self._attachments.append(
                _AttachedFile(
                    source.name,
                    stored_file,
                    media_kind,
                    utc_now_iso(),
                    content_sha256,
                )
            )
            changed = True
        if warnings:
            QMessageBox.warning(
                self,
                "Lampiran tidak ditambahkan",
                "\n".join(warnings),
            )
        return changed

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as attachment_file:
            for chunk in iter(lambda: attachment_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _has_duplicate_attachment(self, content_sha256: str) -> bool:
        return any(
            attachment.content_sha256 == content_sha256
            for attachment in self._attachments
            if attachment.content_sha256
        )

    def _attach_pasted_image(self, image: QImage) -> None:
        if self._store_image_attachment(image, "pasted-image.png"):
            self._update_attachment_preview()

    def _store_image_attachment(self, image: QImage, name: str) -> bool:
        if len(self._attachments) >= MAX_ATTACHMENTS_PER_MESSAGE:
            QMessageBox.warning(
                self,
                "Batas lampiran",
                f"Maksimal {MAX_ATTACHMENTS_PER_MESSAGE} lampiran per pesan.",
            )
            return False
        stored_file = self._attachment_directory / f"{uuid4().hex}--{name}"
        try:
            self._attachment_directory.mkdir(parents=True, exist_ok=True)
            if not image.save(str(stored_file), "PNG"):
                raise OSError("Format PNG tidak dapat disimpan.")
            if stored_file.stat().st_size > MAX_ATTACHMENT_SIZE_BYTES:
                raise OSError("Ukuran gambar melebihi 18 MB.")
            content_sha256 = self._file_sha256(stored_file)
        except OSError as error:
            try:
                stored_file.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Gagal membersihkan gambar lampiran %s", stored_file)
            QMessageBox.warning(
                self,
                "Lampiran gagal",
                f"Tidak dapat menyimpan {name}:\n{error}",
            )
            return False
        if self._has_duplicate_attachment(content_sha256):
            try:
                stored_file.unlink()
            except OSError as error:
                LOGGER.exception(
                    "Gagal menghapus salinan gambar duplikat %s",
                    stored_file,
                )
                QMessageBox.warning(
                    self,
                    "Lampiran duplikat",
                    f"{name} sudah dilampirkan, tetapi salinan sementara gagal "
                    f"dihapus:\n{error}",
                )
            else:
                QMessageBox.information(
                    self,
                    "Lampiran duplikat",
                    f"{name} sudah ada di composer.",
                )
            return False
        self._attachments.append(
            _AttachedFile(
                name,
                stored_file,
                "image",
                utc_now_iso(),
                content_sha256,
            )
        )
        return True

    @staticmethod
    def _image_from_mime_data(mime_data: QMimeData) -> QImage:
        image_data = mime_data.imageData()
        if isinstance(image_data, QPixmap):
            return image_data.toImage()
        if isinstance(image_data, QImage):
            return image_data
        return QImage()

    def _update_attachment_preview(self) -> None:
        while self._attachment_preview_layout.count():
            item = self._attachment_preview_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                if isinstance(widget, _AttachmentPreviewItem):
                    widget.stop_thumbnail()
                widget.setParent(None)
                widget.deleteLater()

        if not self._attachments:
            self._attachment_preview.hide()
            self._adjust_input_height()
            self._update_submit_button()
            return

        for attachment in self._attachments:
            preview_item = self._create_attachment_preview(attachment)
            if len(self._attachments) > 1 and not attachment.media_type:
                preview_item.setFixedWidth(180)
            self._attachment_preview_layout.addWidget(preview_item)
        self._attachment_preview.show()
        self._input_frame.layout().activate()
        self._refresh_attachment_preview_size()
        self._adjust_input_height()
        self._update_submit_button()

    def _refresh_attachment_preview_size(self) -> None:
        if (
            not self._attachments
            or self._attachment_preview_layout.count() == 0
        ):
            return
        available_width = max(1, self._attachment_preview.viewport().width())
        if len(self._attachments) == 1 and not self._attachments[0].media_type:
            preview_item = self._attachment_preview_layout.itemAt(0).widget()
            preview_item.setFixedWidth(available_width)
        natural_width = sum(
            self._attachment_preview_layout.itemAt(index).widget().width()
            for index in range(self._attachment_preview_layout.count())
        )
        natural_width += self._attachment_preview_layout.spacing() * max(
            0,
            self._attachment_preview_layout.count() - 1,
        )
        has_overflow = natural_width > available_width
        self._attachment_preview_content.setFixedSize(
            max(available_width, natural_width),
            _AttachmentPreviewItem._MEDIA_SIZE,
        )
        scrollbar_height = (
            self._attachment_preview.horizontalScrollBar().sizeHint().height()
            if has_overflow
            else 0
        )
        self._attachment_preview.setFixedHeight(
            _AttachmentPreviewItem._MEDIA_SIZE + scrollbar_height
        )

    def _create_attachment_preview(
        self,
        attachment: _AttachedFile,
    ) -> _AttachmentPreviewItem:
        item = _AttachmentPreviewItem(attachment, self._attachment_preview)
        item.media_activated.connect(self._show_media_preview)
        item.remove_requested.connect(self._remove_attachment)
        return item

    def _show_media_preview(self, stored_path: str) -> None:
        path = Path(stored_path)
        attachment = next(
            (
                item
                for item in self._attachments
                if item.stored_path == path
            ),
            self._message_media_files.get(path),
        )
        if attachment is None:
            return
        self._media_preview_page.set_media(attachment)
        self._body_stack.setCurrentWidget(self._media_preview_page)
        self._header.hide()
        self._footer.hide()
        self._composer_backdrop.hide()
        self._scroll_to_latest_button.hide()
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.activate()

    def _close_media_preview(self) -> None:
        self._media_preview_page.stop_media()
        self._body_stack.setCurrentWidget(self._scroll_area)
        self._header.show()
        self._footer.show()
        self._main_layout.setContentsMargins(8, 8, 8, 12)
        self._main_layout.activate()
        self._position_footer()
        self._update_scroll_to_latest_button()

    def _remove_attachment(self, stored_path: str) -> None:
        path = Path(stored_path)
        try:
            path.unlink()
        except OSError as error:
            QMessageBox.warning(
                self,
                "Lampiran gagal dihapus",
                f"Tidak dapat menghapus {path.name}:\n{error}",
            )
            return
        if self._media_preview_page.media_path == path:
            self._close_media_preview()
        self._attachments = [
            attachment
            for attachment in self._attachments
            if attachment.stored_path != path
        ]
        self._update_attachment_preview()

    def _expire_attachments(self) -> None:
        expired_count = self._database.expire_attachments()
        if not expired_count or not self._message_layout.count():
            return
        if self._media_preview_page.media_path in self._message_media_files:
            self._close_media_preview()
        for index in range(self._message_layout.count()):
            row = self._message_layout.itemAt(index).widget()
            if row is not None:
                for tile in row.findChildren(_AttachmentPreviewItem):
                    tile.stop_thumbnail()
        for path in self._message_media_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Gagal membersihkan pratinjau lampiran %s", path)
        self._message_media_files.clear()
        while self._message_layout.count():
            item = self._message_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._load_history()

    def show_panel(self) -> None:
        if not self._has_been_positioned:
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                geometry = screen.availableGeometry()
                self.move(
                    geometry.center().x() - self.width() // 2,
                    geometry.center().y() - self.height() // 2,
                )
            self._has_been_positioned = True
        self.show()
        self.raise_()
        self.activateWindow()

    def shutdown(self) -> None:
        self._attachment_cleanup_timer.stop()
        if self._chat_worker and self._chat_worker.isRunning():
            self._chat_worker.wait()
        self._media_preview_page.stop_media()
        for tile in self.findChildren(_AttachmentPreviewItem):
            tile.stop_thumbnail()
        self._draft_cache.cleanup()
        if self._owns_database:
            self._database.close()

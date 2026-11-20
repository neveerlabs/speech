from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import mimetypes
from pathlib import Path
import re
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
    QObject,
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
    QFontDatabase,
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
    QTextBlockFormat,
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
    QTextEdit,
)
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.token import Comment, Error, Generic, Keyword, Name, Number
from pygments.token import Operator, Punctuation, String
from pygments.util import ClassNotFound

from chat_attachments import copy_attachment
from chat_database import (
    ChatDatabase,
    MAX_ATTACHMENT_SIZE_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    PendingAttachment,
    StoredAttachment,
    StoredMessage,
    format_wib_timestamp,
    utc_now_iso,
)
from chat_service import ChatResponseWorker
from text_to_speech import SoundOfTextWorker

LOGGER = logging.getLogger(__name__)
PANEL_SNAP_DISTANCE = 20
STREAMING_FRAME_INTERVAL_MS = 20
STREAMING_CHARACTERS_PER_FRAME = 32
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
_FENCED_CODE_PATTERN = re.compile(
    r"(?m)^[ \t]*```([^\n]*)\n(.*?)^[ \t]*```[ \t]*(?=\n|$)",
    re.DOTALL,
)


@dataclass(frozen=True)
class _MarkdownCodeBlock:
    placeholder: str
    language: str
    source: str
    height: int


@dataclass
class _ChatNotification:
    title: str
    description: str
    details: str
    created_at: datetime
    is_read: bool = False


def _extract_markdown_code_blocks(
    text: str,
) -> tuple[str, tuple[_MarkdownCodeBlock, ...]]:
    code_blocks: list[_MarkdownCodeBlock] = []
    code_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    line_height = max(1, QFontMetrics(code_font).lineSpacing())

    def replace_fence(match: re.Match[str]) -> str:
        source = match.group(2)
        lines = source.rstrip("\n").splitlines() or [""]
        visible_lines = min(10, len(lines))
        editor_padding = 16 if len(lines) <= 10 else 7
        editor_height = line_height * visible_lines + editor_padding
        placeholder = f"SPEECHCODEBLOCK{len(code_blocks)}END"
        language = match.group(1).strip().split(maxsplit=1)
        code_blocks.append(
            _MarkdownCodeBlock(
                placeholder=placeholder,
                language=language[0] if language else "",
                source=source,
                height=editor_height + 28,
            )
        )
        return f"\n\n{placeholder}\n\n"

    markdown = _FENCED_CODE_PATTERN.sub(replace_fence, text)
    return markdown, tuple(code_blocks)


def _reserve_markdown_code_blocks(
    document: QTextDocument,
    code_blocks: tuple[_MarkdownCodeBlock, ...],
) -> None:
    blocks_by_placeholder = {
        code_block.placeholder: code_block for code_block in code_blocks
    }
    block = document.begin()
    while block.isValid():
        code_block = blocks_by_placeholder.get(block.text())
        if code_block is not None:
            block_format = block.blockFormat()
            block_format.setLeftMargin(0)
            block_format.setRightMargin(0)
            block_format.setTopMargin(2)
            block_format.setBottomMargin(2)
            block_format.setLineHeight(
                code_block.height,
                QTextBlockFormat.LineHeightTypes.FixedHeight.value,
            )
            cursor = QTextCursor(block)
            cursor.setBlockFormat(block_format)
            hidden = QTextCharFormat()
            hidden.setForeground(QColor(0, 0, 0, 0))
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.mergeCharFormat(hidden)
        block = block.next()


def _apply_code_syntax_highlighting(
    editor: QPlainTextEdit,
    source: str,
    language: str,
) -> None:
    try:
        lexer_name = {
            "shell": "bash",
            "shellscript": "bash",
            "zsh": "bash",
        }.get(language.lower(), language)
        try:
            lexer = get_lexer_by_name(
                lexer_name,
                stripnl=False,
                ensurenl=False,
            )
        except ClassNotFound:
            lexer = guess_lexer(source, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return

    utf16_offsets = [0]
    for character in source:
        utf16_offsets.append(
            utf16_offsets[-1] + (2 if ord(character) > 0xFFFF else 1)
        )

    cursor = QTextCursor(editor.document())
    for offset, token_type, value in lexer.get_tokens_unprocessed(source):
        if not value:
            continue
        token_format = QTextCharFormat()
        if token_type in Error:
            token_format.setForeground(QColor("#ff7b72"))
        elif token_type in Comment:
            token_format.setForeground(QColor("#8b949e"))
            token_format.setFontItalic(True)
        elif token_type in Keyword:
            token_format.setForeground(QColor("#ff7b72"))
        elif token_type in Name.Function or token_type in Name.Decorator:
            token_format.setForeground(QColor("#d2a8ff"))
        elif token_type in Name.Class:
            token_format.setForeground(QColor("#f0883e"))
        elif token_type in Name.Builtin or token_type in Name.Attribute:
            token_format.setForeground(QColor("#79c0ff"))
        elif token_type in String:
            token_format.setForeground(QColor("#a5d6ff"))
        elif token_type in Number:
            token_format.setForeground(QColor("#79c0ff"))
        elif token_type in Operator or token_type in Punctuation:
            token_format.setForeground(QColor("#ff7b72"))
        elif token_type in Generic.Heading:
            token_format.setForeground(QColor("#d2a8ff"))
        else:
            continue

        cursor.setPosition(utf16_offsets[offset])
        cursor.setPosition(
            utf16_offsets[offset + len(value)],
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.mergeCharFormat(token_format)


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
            nesting_level = max(1, list_format.indent())
            if list_format.indent() != 0:
                list_format.setIndent(0)
                markdown_list.setFormat(list_format)
            block_format.setLeftMargin(12 + (nesting_level - 1) * 16)
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
                if original_format.fontWeight() >= QFont.Weight.Bold:
                    format_style.setFontWeight(QFont.Weight.DemiBold)
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


@dataclass
class _PendingChatTurn:
    content: str
    attachments: tuple[PendingAttachment, ...]
    row: _UserMessageRow
    message_id: int | None = None
    unsaved_message: StoredMessage | None = None
    reply_content: str | None = None
    reply_row: _ConversationMessageRow | None = None
    retry_mode: str = "gemini"


class _UserMessageBubble(QWidget):
    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._text = text
        self.setMinimumWidth(0)
        self.setMaximumWidth(380)
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
    code_copied = Signal()
    media_activated = Signal(str)
    retry_requested = Signal()

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
        if self._bubble is not None:
            self._bubble._label.code_copied.connect(self.code_copied.emit)
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

        self._retry_button = QToolButton(self)
        self._retry_button.setObjectName("userMessageRetryButton")
        self._retry_button.setAccessibleName("Coba kirim ulang pesan")
        self._retry_button.setFixedSize(20, 20)
        self._retry_button.setIcon(
            _ConversationMessageRow._action_icon("regenerate")
        )
        self._retry_button.setIconSize(QSize(17, 17))
        self._retry_button.setStyleSheet(
            "QToolButton { color: #aeb4bd; background: transparent; "
            "border: none; padding: 0; }"
        )
        self._retry_button.clicked.connect(self.retry_requested.emit)
        self._retry_button.hide()

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
            prefix_width = QFontMetrics(
                self._bubble._label.font()
            ).horizontalAdvance("Ada ")
            self._bubble.setMaximumWidth(
                max(
                    self._bubble.minimumWidth(),
                    min(self.width(), 420) - prefix_width,
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
        self._position_retry_button()

    def set_retry_visible(self, visible: bool) -> None:
        self._retry_button.setVisible(visible)
        self._position_retry_button()

    def _position_retry_button(self) -> None:
        target = self._bubble or self._attachment_grid
        if target is None:
            self._retry_button.move(0, self._rest_y)
            return
        self._retry_button.move(
            max(0, target.x() - self._retry_button.width() - 3),
            target.y() + target.height() - self._retry_button.height(),
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
    message_copied = Signal()
    code_copied = Signal()
    speech_requested = Signal(object)

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
        "check": '<path d="m5 12 4 4L19 6"/>',
    }
    _FILLED_REACTION_ICONS = {
        "like": (
            '<path d="M1 21h4V9H1v12zM23 10c0-1.1-.9-2-2-2h-6.31l.95-4.57'
            ".03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 "
            "7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-"
            "7.05c.09-.23.14-.48.14-.73v-1z\"/>"
        ),
        "dislike": (
            '<g transform="rotate(180 12 12)"><path d="M1 21h4V9H1v12z'
            "M23 10c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79"
            "-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1."
            "9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.48"
            ".14-.73v-1z\"/></g>"
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
        self._content_label = self._add_message_content(
            message,
            row_layout,
            self,
        )
        if self._content_label is not None:
            self._content_label.code_copied.connect(self.code_copied.emit)
        self._actions = QWidget(self)
        self._actions.setObjectName("modelMessageActions")
        actions_layout = QHBoxLayout(self._actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        self._like_button = self._create_action("like", "Suka")
        self._dislike_button = self._create_action("dislike", "Tidak suka")
        self._like_button.setCheckable(True)
        self._dislike_button.setCheckable(True)
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
        self._text = message.content
        self._copy_icon_state = "copy"
        self._speech_state = "idle"
        self._speech_spinner_angle = 0
        self._like_button.clicked.connect(
            lambda checked: self._set_reaction("like", checked)
        )
        self._dislike_button.clicked.connect(
            lambda checked: self._set_reaction("dislike", checked)
        )
        self._copy_button.clicked.connect(self._copy_message)
        self._speech_button.clicked.connect(
            lambda: self.speech_requested.emit(self)
        )
        self._speech_button.setEnabled(bool(self._text.strip()))
        self._speech_spinner_timer = QTimer(self)
        self._speech_spinner_timer.setInterval(60)
        self._speech_spinner_timer.timeout.connect(self._advance_speech_spinner)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(2200)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)

    def _copy_message(self) -> None:
        QApplication.clipboard().setText(self._text)
        self._copy_icon_state = "check"
        self._copy_button.setIcon(self._action_icon("check"))
        self._copy_feedback_timer.start()
        self.message_copied.emit()

    def _reset_copy_feedback(self) -> None:
        self._copy_icon_state = "copy"
        self._copy_button.setIcon(self._action_icon("copy"))

    def _set_reaction(self, reaction: str, checked: bool) -> None:
        selected_button = (
            self._like_button if reaction == "like" else self._dislike_button
        )
        if not checked:
            selected_button.setIcon(self._action_icon(reaction))
            return

        other_button = (
            self._dislike_button if reaction == "like" else self._like_button
        )
        other_button.setChecked(False)
        other_reaction = "dislike" if reaction == "like" else "like"
        other_button.setIcon(self._action_icon(other_reaction))
        selected_button.setIcon(self._action_icon(reaction, filled=True))

    def start_streaming(self) -> None:
        self._speech_button.setEnabled(False)
        self.set_speech_state("idle")
        if self._content_label is None:
            self._content_label = self._create_content_label("", self)
            self.layout().insertWidget(0, self._content_label)
            self._content_label.code_copied.connect(self.code_copied.emit)
        self._text = ""
        self._content_label.start_streaming()

    def append_streaming_text(self, text: str) -> None:
        if self._content_label is None:
            raise RuntimeError("Pesan streaming belum dimulai.")
        self._text += text
        self._content_label.append_streaming_text(text)
        self.updateGeometry()

    def finish_streaming(self, text: str) -> None:
        if self._content_label is None:
            raise RuntimeError("Pesan streaming belum dimulai.")
        self._text = text
        self._content_label.finish_streaming(text)
        self._speech_button.setEnabled(bool(text.strip()))
        self.updateGeometry()

    def speech_text(self) -> str:
        if self._content_label is None:
            return self._text.strip()
        markdown, code_blocks = _extract_markdown_code_blocks(
            self._content_label.text()
        )
        document = QTextDocument()
        document.setMarkdown(markdown)
        text = document.toPlainText()
        for code_block in code_blocks:
            text = text.replace(code_block.placeholder, code_block.source.rstrip())
        return text.strip()

    def set_speech_state(self, state: str) -> None:
        self._speech_state = state
        self._speech_spinner_timer.stop()
        if state == "loading":
            self._speech_button.setAccessibleName("Menyiapkan suara")
            self._speech_spinner_timer.start()
            self._advance_speech_spinner()
        elif state == "playing":
            self._speech_button.setAccessibleName("Hentikan pembacaan")
            self._speech_button.setIcon(
                self._action_icon("speech", "#ffffff")
            )
        else:
            self._speech_button.setAccessibleName("Bacakan")
            self._speech_button.setIcon(self._action_icon("speech"))

    def _advance_speech_spinner(self) -> None:
        if self._speech_state != "loading":
            return
        self._speech_spinner_angle = (self._speech_spinner_angle + 30) % 360
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor("#aeb4bd"),
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawArc(
            QRectF(4.5, 4.5, 15, 15),
            self._speech_spinner_angle * 16,
            260 * 16,
        )
        painter.end()
        self._speech_button.setIcon(QIcon(pixmap))

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
    def _action_icon(
        cls,
        name: str,
        color: str = "#aeb4bd",
        filled: bool = False,
    ) -> QIcon:
        path = cls._ACTION_ICONS[name]
        fill = "none"
        stroke = color
        if filled and name in cls._FILLED_REACTION_ICONS:
            transform = (
                "matrix(0.909 0 0 1.05 2.08 0.95)"
                if name == "like"
                else "matrix(0.952 0 0 1.05 0.048 -2.15)"
            )
            path = (
                f'<g transform="{transform}">'
                f"{cls._FILLED_REACTION_ICONS[name]}</g>"
            )
            fill = color
            stroke = "none"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            f'viewBox="0 0 24 24" fill="{fill}" stroke="{stroke}" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            f"{path}</svg>"
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
    ) -> _WrappedMessageLabel | None:
        content_label = None
        if message.content:
            content_label = _ConversationMessageRow._create_content_label(
                message.content,
                parent,
            )
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
        return content_label

    @staticmethod
    def _create_content_label(
        text: str,
        parent: QWidget,
    ) -> _WrappedMessageLabel:
        content_label = _WrappedMessageLabel(text, parent)
        content_label.setObjectName("messageContent")
        size_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        size_policy.setHeightForWidth(True)
        content_label.setSizePolicy(size_policy)
        content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        content_label.setStyleSheet("color: #f3f4f6; background: transparent;")
        return content_label


class _CodeLineNumberArea(QWidget):
    def __init__(self, editor: _CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._editor.paint_line_number_area(event)


class _CodeEditor(QPlainTextEdit):
    def __init__(self, source: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("markdownCodeEditor")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setContentsMargins(0, 0, 0, 0)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setPlainText(source.rstrip("\n"))
        self.setStyleSheet(
            "QPlainTextEdit { color: #e6edf3; background: #0d1117; "
            "border: none; padding: 4px 0; selection-background-color: #264f78; }"
            "QScrollBar:vertical, QScrollBar:horizontal { "
            "background: #0d1117; border: none; }"
            "QScrollBar::handle:vertical, QScrollBar::handle:horizontal { "
            "background: #3d444d; border-radius: 4px; min-height: 18px; "
            "min-width: 18px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { "
            "width: 0; height: 0; border: none; }"
        )
        self._line_number_area = _CodeLineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._line_number_area.update)
        self._update_line_number_area_width(0)

    def line_number_area_width(self) -> int:
        digits = max(1, len(str(max(1, self.blockCount()))))
        return self.fontMetrics().horizontalAdvance("9" * digits) + 4

    def _update_line_number_area_width(self, _block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0,
                rect.y(),
                self._line_number_area.width(),
                rect.height(),
            )
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def paint_line_number_area(self, event: QPaintEvent) -> None:
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), QColor("#161b22"))
        painter.setPen(QColor("#7d8590"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        while block.isValid() and top <= event.rect().bottom():
            bottom = top + round(self.blockBoundingRect(block).height())
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0,
                    top,
                    self._line_number_area.width(),
                    self.fontMetrics().lineSpacing(),
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    str(block_number + 1),
                )
            block = block.next()
            top = bottom
            block_number += 1

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._line_number_area.setGeometry(
            QRect(
                self.contentsRect().left(),
                self.contentsRect().top(),
                self.line_number_area_width(),
                self.contentsRect().height(),
            )
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#30363d"), 1))
        block = self.firstVisibleBlock()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        space_width = self.fontMetrics().horizontalAdvance(" ")
        horizontal_offset = self.horizontalScrollBar().value()
        while block.isValid() and top < self.viewport().height():
            line_height = round(self.blockBoundingRect(block).height())
            indentation = len(block.text().expandtabs(4)) - len(
                block.text().expandtabs(4).lstrip(" ")
            )
            for column in range(4, indentation + 1, 4):
                x = column * space_width - horizontal_offset
                painter.drawLine(x, top, x, top + line_height)
            top += line_height
            block = block.next()


class _CodeBlockWidget(QWidget):
    code_copied = Signal()

    def __init__(
        self,
        code_block: _MarkdownCodeBlock,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("markdownCodeBlock")
        self.setFixedHeight(code_block.height)
        self.setMouseTracking(True)
        self.setStyleSheet(
            "QWidget#markdownCodeBlock { background: #0d1117; "
            "border: 1px solid #3d444d; border-radius: 7px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("markdownCodeHeader")
        header.setFixedHeight(27)
        header.setStyleSheet(
            "QWidget#markdownCodeHeader { background: #161b22; "
            "border: none; border-top-left-radius: 6px; "
            "border-top-right-radius: 6px; }"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(9, 0, 5, 0)
        language_label = QLabel(code_block.language, header)
        language_label.setStyleSheet(
            "color: #8b949e; background: transparent; border: none; "
            "font-size: 11px;"
        )
        header_layout.addWidget(language_label)
        header_layout.addStretch()
        self._copy_button = QToolButton(header)
        self._copy_button.setObjectName("copyCodeBlockButton")
        self._copy_button.setAccessibleName("Salin kode")
        self._copy_button.setFixedSize(24, 22)
        self._copy_icon_state = "copy"
        self._copy_button.setIcon(_ConversationMessageRow._action_icon("copy"))
        self._copy_button.setIconSize(QSize(15, 15))
        self._copy_button.setStyleSheet(
            "QToolButton { color: #c9d1d9; background: transparent; "
            "border: none; padding: 2px; }"
            "QToolButton:hover { background: #30363d; border: none; }"
        )
        header_layout.addWidget(self._copy_button)
        layout.addWidget(header)

        self._editor = _CodeEditor(code_block.source, self)
        self._editor.setFixedHeight(code_block.height - header.height())
        _apply_code_syntax_highlighting(
            self._editor,
            code_block.source.rstrip("\n"),
            code_block.language,
        )
        layout.addWidget(self._editor)
        self._source = code_block.source.rstrip("\n")
        self._copy_button.clicked.connect(self._copy_source)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1600)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)

    def _copy_source(self) -> None:
        QApplication.clipboard().setText(self._source)
        self._copy_icon_state = "check"
        self._copy_button.setIcon(_ConversationMessageRow._action_icon("check"))
        self._copy_feedback_timer.start()
        self.code_copied.emit()

    def _reset_copy_feedback(self) -> None:
        self._copy_icon_state = "copy"
        self._copy_button.setIcon(_ConversationMessageRow._action_icon("copy"))


class _MarkdownTextView(QTextBrowser):
    code_copied = Signal()

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._markdown_source = ""
        self._streaming_text_parts: list[str] | None = None
        self._code_blocks: tuple[_MarkdownCodeBlock, ...] = ()
        self._code_widgets: dict[str, _CodeBlockWidget] = {}
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
        self.verticalScrollBar().valueChanged.connect(
            self._position_code_widgets
        )
        self.horizontalScrollBar().valueChanged.connect(
            self._position_code_widgets
        )
        self._set_markdown(text)

    def text(self) -> str:
        if self._streaming_text_parts is not None:
            return "".join(self._streaming_text_parts)
        return self._markdown_source

    def textFormat(self) -> Qt.TextFormat:
        return Qt.TextFormat.MarkdownText

    def sizeHint(self) -> QSize:
        document = (
            self.document()
            if self._streaming_text_parts is not None
            else _markdown_document(self.text(), self.font())
        )
        width = max(1, min(400, int(document.idealWidth() + 0.999)))
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().lineSpacing())

    def setText(self, text: str) -> None:
        self._set_markdown(text)

    def start_streaming(self) -> None:
        self._streaming_text_parts = []
        self._render_markdown("")

    def append_streaming_text(self, text: str) -> None:
        if self._streaming_text_parts is None:
            raise RuntimeError("Renderer Markdown belum memasuki mode streaming.")
        if not text:
            return
        self._streaming_text_parts.append(text)
        self._render_markdown("".join(self._streaming_text_parts))

    def finish_streaming(self, text: str) -> None:
        self._set_markdown(text)

    def _set_markdown(self, text: str) -> None:
        self._streaming_text_parts = None
        self._render_markdown(text)

    def _render_markdown(self, text: str) -> None:
        self._markdown_source = text
        previous_blocks = {
            code_block.placeholder: code_block for code_block in self._code_blocks
        }
        previous_widgets = dict(self._code_widgets)
        for widget in previous_widgets.values():
            widget.hide()
        markdown, code_blocks = _extract_markdown_code_blocks(text)
        self._code_blocks = code_blocks
        self._code_widgets = {}
        self.document().clear()
        self.document().setDefaultStyleSheet(MARKDOWN_STYLESHEET)
        super().setMarkdown(markdown)
        _style_markdown_document(self.document())
        _reserve_markdown_code_blocks(self.document(), self._code_blocks)
        for code_block in self._code_blocks:
            widget = previous_widgets.pop(code_block.placeholder, None)
            if previous_blocks.get(code_block.placeholder) != code_block:
                if widget is not None:
                    widget.deleteLater()
                widget = None
            if widget is None:
                widget = _CodeBlockWidget(code_block, self.viewport())
                widget.code_copied.connect(self.code_copied.emit)
            self._code_widgets[code_block.placeholder] = widget
        for widget in previous_widgets.values():
            widget.deleteLater()
        self.updateGeometry()
        self._position_code_widgets()

    def _position_code_widgets(self, *_args: object) -> None:
        if not self._code_widgets:
            return
        block = self.document().begin()
        vertical_offset = self.verticalScrollBar().value()
        horizontal_offset = self.horizontalScrollBar().value()
        while block.isValid():
            widget = self._code_widgets.get(block.text())
            if widget is not None:
                bounds = self.document().documentLayout().blockBoundingRect(block)
                x = round(bounds.x()) - horizontal_offset
                y = round(bounds.y()) - vertical_offset
                widget.setGeometry(
                    x,
                    y,
                    max(0, self.viewport().width() - x),
                    widget.height(),
                )
                widget.show()
                widget.raise_()
            block = block.next()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_code_widgets()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self._position_code_widgets()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        document = (
            self.document()
            if self._streaming_text_parts is not None
            else _markdown_document(self.text(), self.font())
        )
        document.setTextWidth(max(1, width))
        return max(1, int(document.size().height() + 0.999))


class _WrappedMessageLabel(_MarkdownTextView):
    pass


def _markdown_document(text: str, font: QFont) -> QTextDocument:
    document = QTextDocument()
    document.setDefaultFont(font)
    document.setDocumentMargin(0)
    document.setDefaultStyleSheet(MARKDOWN_STYLESHEET)
    markdown, code_blocks = _extract_markdown_code_blocks(text)
    document.setMarkdown(markdown)
    _style_markdown_document(document)
    _reserve_markdown_code_blocks(document, code_blocks)
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


class _NotificationRow(QWidget):
    activated = Signal()

    def __init__(
        self,
        notification: _ChatNotification,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._notification = notification
        self.setObjectName("notificationRow")
        self.setFixedHeight(46)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(
            "QWidget#notificationRow { background: transparent; "
            "border-radius: 6px; }"
            "QWidget#notificationRow:hover { background: #2b3036; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(1)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(6)
        self._title_label = QLabel(self)
        self._title_label.setStyleSheet(
            "color: #e5e7eb; background: transparent; font-size: 11px;"
        )
        self._date_label = QLabel(
            notification.created_at.strftime("%d/%m/%Y, %H:%M"),
            self,
        )
        self._date_label.setFixedWidth(106)
        self._date_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._date_label.setStyleSheet(
            "color: #8b949e; background: transparent; font-size: 9px;"
        )
        self._unread_indicator = QLabel("●", self)
        self._unread_indicator.setFixedWidth(10)
        self._unread_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._unread_indicator.setStyleSheet(
            "color: #58a6ff; background: transparent; font-size: 9px;"
        )
        heading.addWidget(self._title_label, 1)
        heading.addWidget(self._date_label)
        heading.addWidget(self._unread_indicator)
        layout.addLayout(heading)

        self._description_label = QLabel(self)
        self._description_label.setStyleSheet(
            "color: #aeb4bd; background: transparent; font-size: 10px;"
        )
        layout.addWidget(self._description_label)
        self._refresh_elided_text()

    def _refresh_elided_text(self) -> None:
        metrics = self._title_label.fontMetrics()
        title_width = max(
            0,
            self.width() - 8 - 8 - self._date_label.width() - 10 - 12,
        )
        self._title_label.setText(
            metrics.elidedText(
                self._notification.title,
                Qt.TextElideMode.ElideRight,
                title_width,
            )
        )
        self._description_label.setText(
            self._description_label.fontMetrics().elidedText(
                self._notification.description,
                Qt.TextElideMode.ElideRight,
                max(0, self.width() - 24),
            )
        )
        self._unread_indicator.setVisible(not self._notification.is_read)
        self._title_label.setStyleSheet(
            "color: #f3f4f6; background: transparent; font-size: 11px; "
            f"font-weight: {'600' if not self._notification.is_read else '400'};"
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_elided_text()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _NotificationDismissFilter(QObject):
    def __init__(
        self,
        popup: QWidget,
        toggle_button: QWidget,
        dismiss: Callable[[], None],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._popup = popup
        self._toggle_button = toggle_button
        self._dismiss = dismiss

    def eventFilter(self, watched, event) -> bool:
        if event.type() not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
        ):
            return False
        if not self._popup.isVisible():
            return False
        position = event.globalPosition().toPoint()
        popup_rect = QRect(self._popup.mapToGlobal(QPoint(0, 0)), self._popup.size())
        toggle_rect = QRect(
            self._toggle_button.mapToGlobal(QPoint(0, 0)),
            self._toggle_button.size(),
        )
        if not popup_rect.contains(position) and not toggle_rect.contains(position):
            self._dismiss()
        return False


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
        self._speech_worker: SoundOfTextWorker | None = None
        self._speech_row: _ConversationMessageRow | None = None
        self._pending_speech_row: _ConversationMessageRow | None = None
        self._is_sending = False
        self._active_turn: _PendingChatTurn | None = None
        self._pending_turns: dict[int, _PendingChatTurn] = {}
        self._pending_scroll_to_bottom = False
        self._reply_chunks: deque[str] = deque()
        self._reply_chunk_offset = 0
        self._streaming_row: _ConversationMessageRow | None = None
        self._stream_final_text: str | None = None
        self._stream_worker_finished = False
        self._notifications: list[_ChatNotification] = []
        self._notification_rows: list[_NotificationRow] = []
        self._selected_notification: _ChatNotification | None = None
        self._notification_dismiss_filter: _NotificationDismissFilter | None = None
        self._notification_filter_installed = False
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(STREAMING_FRAME_INTERVAL_MS)
        self._stream_timer.timeout.connect(self._display_next_reply_character)
        self._speech_audio_output = QAudioOutput(self)
        self._speech_audio_output.setVolume(1.0)
        self._speech_player = QMediaPlayer(self)
        self._speech_player.setAudioOutput(self._speech_audio_output)
        self._speech_player.playbackStateChanged.connect(
            self._handle_speech_playback_state
        )
        self._speech_player.errorOccurred.connect(
            self._handle_speech_playback_error
        )
        self._speech_start_timeout = QTimer(self)
        self._speech_start_timeout.setSingleShot(True)
        self._speech_start_timeout.setInterval(20_000)
        self._speech_start_timeout.timeout.connect(
            self._handle_speech_start_timeout
        )

        self._build_ui()
        self._expire_attachments()
        self._load_history()
        self._install_drag_filters(self._header)
        self._install_attachment_drop_targets()
        application = QApplication.instance()
        if application is not None:
            application.applicationStateChanged.connect(
                self._handle_application_state_changed
            )
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
        if hasattr(self, "_header") and not self._header.isHidden():
            header_height = self._header.height()
            header_path = QPainterPath()
            header_path.moveTo(0, header_height)
            header_path.lineTo(0, 16)
            header_path.quadTo(0, 0, 16, 0)
            header_path.lineTo(self.width() - 16, 0)
            header_path.quadTo(self.width(), 0, self.width(), 16)
            header_path.lineTo(self.width(), header_height)
            header_path.closeSubpath()
            painter.fillPath(header_path, QColor("#24272d"))
            border_y = header_height - 1
            painter.fillRect(
                QRect(0, border_y, self.width(), 1),
                QColor("#3a3f46"),
            )
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
        self._hide_notifications()
        self._stop_speech()
        self.visibility_changed.emit(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_footer"):
            self.layout().activate()
            self._position_footer()
            self._position_scroll_to_latest_button()
        if hasattr(self, "_drop_overlay"):
            self._drop_overlay.setGeometry(self.rect().adjusted(6, 6, -6, -6))
        if (
            hasattr(self, "_notification_popup")
            and self._notification_popup.isVisible()
        ):
            self._position_notification_popup()

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
            self._notification_popup.isVisible()
            and event.type()
            in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel)
        ):
            position = event.globalPosition().toPoint()
            popup_rect = QRect(
                self._notification_popup.mapToGlobal(QPoint(0, 0)),
                self._notification_popup.size(),
            )
            toggle_rect = QRect(
                self._notification_button.mapToGlobal(QPoint(0, 0)),
                self._notification_button.size(),
            )
            is_toggle_click = (
                event.type() == QEvent.Type.MouseButtonPress
                and toggle_rect.contains(position)
            )
            if not popup_rect.contains(position) and not is_toggle_click:
                self._hide_notifications()
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
        layout.setContentsMargins(8, 0, 8, 12)
        layout.setSpacing(0)

        header = QWidget(self)
        self._header = header
        header.setObjectName("chatHeader")
        header.setStyleSheet(
            "QWidget#chatHeader { background: transparent; border: none; }"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 3, 0, 3)
        header_layout.setSpacing(2)
        title = QLabel("Chat Speech AI", header)
        title.setStyleSheet("color: #f3f4f6; font-size: 15px; font-weight: 600;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self._settings_button = self._create_header_icon_button(
            "settings",
            "settingsButton",
            "Pengaturan",
        )
        self._notification_button = self._create_header_icon_button(
            "notification",
            "notificationButton",
            "Notifikasi",
        )
        self._github_button = self._create_header_icon_button(
            "github",
            "githubButton",
            "GitHub",
        )
        header_layout.addWidget(self._notification_button)
        header_layout.addWidget(self._github_button)
        header_layout.addWidget(self._settings_button)

        self._close_button = QToolButton(header)
        self._close_button.setObjectName("closePanelButton")
        self._close_button.setAccessibleName("Tutup panel")
        self._close_button.setFixedSize(28, 28)
        self._close_button.setIcon(self._header_icon("close"))
        self._close_button.setIconSize(QSize(20, 20))
        self._close_button.setStyleSheet(
            "QToolButton { color: #e5e7eb; background-color: transparent; "
            "border: none; border-radius: 14px; }"
            "QToolButton:hover { background-color: #3b414a; "
            "border: none; border-radius: 14px; }"
        )
        self._close_button.clicked.connect(self.hide)
        header_layout.addWidget(self._close_button)
        header.setFixedHeight(34)
        layout.addWidget(header)

        self._notification_badge = QLabel("0", self._notification_button)
        self._notification_badge.setObjectName("notificationUnreadBadge")
        self._notification_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._notification_badge.setFixedSize(16, 16)
        self._notification_badge.move(16, 0)
        self._notification_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._notification_badge.setStyleSheet(
            "QLabel#notificationUnreadBadge { color: #ffffff; "
            "background: #f85149; border: 1px solid #24272d; "
            "border-radius: 8px; font-size: 9px; font-weight: 600; }"
        )
        self._notification_badge.hide()
        self._notification_button.clicked.connect(self._toggle_notifications)
        self._build_notification_popup()

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setHorizontalScrollBarPolicy(
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
            "border-radius: 12px; padding: 0 8px; }"
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

    @staticmethod
    def _create_header_icon_button(
        icon_name: str,
        object_name: str,
        accessible_name: str,
    ) -> QToolButton:
        button = QToolButton()
        button.setObjectName(object_name)
        button.setAccessibleName(accessible_name)
        button.setFixedSize(28, 28)
        button.setIcon(ChatPanel._header_icon(icon_name))
        button.setIconSize(QSize(20, 20))
        button.setStyleSheet(
            "QToolButton { color: #cbd5e1; background: transparent; "
            "border: none; border-radius: 14px; }"
            "QToolButton:hover { background: #3b414a; "
            "border: none; border-radius: 14px; }"
        )
        return button

    @staticmethod
    def _header_icon(icon_name: str) -> QIcon:
        paths = {
            "settings": (
                '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 '
                "2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.88-.34 "
                "1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 "
                "1.7 0 0 0-1.11-1.57 1.7 1.7 0 0 0-1.88.34l-.06.06a2 "
                "2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.88 "
                "1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 "
                "1.7 0 0 0 1.57-1.11 1.7 1.7 0 0 0-.34-1.88l-.06-.06a2 "
                "2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.88.34h.08A1.7 "
                "1.7 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 "
                "1.11 1.57 1.7 1.7 0 0 0 1.88-.34l.06-.06a2 2 0 1 1 "
                "2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.88v.08a1.7 1.7 "
                "0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0 "
                '-1.57 1.11z"/>'
            ),
            "close": '<path d="M4 4 20 20M20 4 4 20"/>',
            "notification": (
                '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/>'
                '<path d="M10 21h4"/>'
            ),
            "check_check": (
                '<path d="m2 12 4 4 9-9"/>'
                '<path d="m9 12 4 4 9-9"/>'
            ),
            "check": '<path d="m5 12 4 4L19 6"/>',
            "trash": (
                '<path d="M3 6h18"/>'
                '<path d="M8 6V4h8v2"/>'
                '<path d="m19 6-1 14H6L5 6"/>'
                '<path d="M10 11v5M14 11v5"/>'
            ),
            "back": '<path d="M19 12H5m7 7-7-7 7-7"/>',
            "copy": (
                '<rect x="8" y="8" width="13" height="13" rx="2"/>'
                '<path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 '
                '2 0 0 0 2 2h3"/>'
            ),
            "github": (
                # Simple Icons GitHub mark, released under CC0 1.0.
                '<path fill="#cbd5e1" stroke="none" d="M12 .297c-6.63 '
                "0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 "
                "11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-"
                "3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 "
                "17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 "
                "1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 "
                "1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-"
                "5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-"
                ".135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 "
                "1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 "
                "3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 "
                "2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-"
                "2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 "
                "0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 "
                '22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/>'
            ),
        }
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            'viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            f"{paths[icon_name]}</svg>"
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
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

    def _add_message_row(self, message: StoredMessage) -> QWidget:
        if message.role == "user":
            attachments = self._prepare_message_attachments(message)
            row = _UserMessageRow(
                message,
                self._scroll_area.widget(),
                attachments,
            )
            row.media_activated.connect(self._show_media_preview)
        else:
            row = _ConversationMessageRow(message, self._scroll_area.widget())
            row.speech_requested.connect(self._toggle_speech)
        row.message_copied.connect(self._show_copy_toast)
        row.code_copied.connect(self._show_code_copy_toast)
        self._pending_scroll_to_bottom = True
        self._message_layout.addWidget(row)
        QTimer.singleShot(0, self._scroll_to_bottom)
        return row

    def _toggle_speech(self, row: _ConversationMessageRow) -> None:
        worker = self._speech_worker
        if worker is not None and worker.isRunning():
            if self._speech_row is row or self._pending_speech_row is row:
                pending_row = self._pending_speech_row
                self._pending_speech_row = None
                if pending_row is not None:
                    pending_row.set_speech_state("idle")
                worker.requestInterruption()
                self._stop_speech_playback()
            else:
                pending_row = self._pending_speech_row
                if pending_row is not None:
                    pending_row.set_speech_state("idle")
                self._pending_speech_row = row
                row.set_speech_state("loading")
                worker.requestInterruption()
                self._stop_speech_playback()
            return

        if self._speech_row is row:
            self._stop_speech_playback()
            return

        self._stop_speech_playback()
        self._begin_speech_request(row)

    def _begin_speech_request(self, row: _ConversationMessageRow) -> None:
        if not row.isVisible() or not row._text.strip():
            return
        text = row.speech_text()
        if not text:
            return

        worker = SoundOfTextWorker(text)
        worker.audio_ready.connect(
            lambda audio_url, source_worker=worker: self._handle_speech_audio_ready(
                source_worker,
                audio_url,
            )
        )
        worker.error_occurred.connect(
            lambda error, source_worker=worker: self._handle_speech_error(
                source_worker,
                error,
            )
        )
        worker.finished.connect(
            lambda source_worker=worker: self._handle_speech_worker_finished(
                source_worker
            )
        )
        self._speech_worker = worker
        self._speech_row = row
        row.set_speech_state("loading")
        worker.start()

    def _handle_speech_audio_ready(
        self,
        worker: SoundOfTextWorker,
        audio_url: str,
    ) -> None:
        if worker is not self._speech_worker or self._speech_row is None:
            return
        self._speech_start_timeout.start()
        self._speech_player.setSource(QUrl(audio_url))
        self._speech_player.play()

    def _handle_speech_error(
        self,
        worker: SoundOfTextWorker,
        error: str,
    ) -> None:
        if worker is not self._speech_worker:
            return
        self._stop_speech_playback()
        self._show_error_toast("Pembacaan pesan gagal", error)

    def _handle_speech_worker_finished(
        self,
        worker: SoundOfTextWorker,
    ) -> None:
        if worker is not self._speech_worker:
            worker.deleteLater()
            return
        self._speech_worker = None
        worker.deleteLater()
        pending_row = self._pending_speech_row
        self._pending_speech_row = None
        if pending_row is not None and self.isVisible() and pending_row.isVisible():
            self._begin_speech_request(pending_row)

    def _handle_speech_playback_state(
        self,
        state: QMediaPlayer.PlaybackState,
    ) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._speech_start_timeout.stop()
            if self._speech_row is not None:
                self._speech_row.set_speech_state("playing")
        elif (
            state == QMediaPlayer.PlaybackState.StoppedState
            and self._speech_row is not None
            and self._speech_row._speech_state == "playing"
        ):
            self._stop_speech_playback()

    def _handle_speech_playback_error(
        self,
        error: QMediaPlayer.Error,
        error_string: str,
    ) -> None:
        if error == QMediaPlayer.Error.NoError or self._speech_row is None:
            return
        self._stop_speech_playback()
        self._show_error_toast(
            "Pemutaran suara gagal",
            error_string or "Audio tidak dapat diputar.",
        )

    def _handle_speech_start_timeout(self) -> None:
        if self._speech_row is None:
            return
        self._stop_speech_playback()
        self._show_error_toast(
            "Pemutaran suara gagal",
            "Audio tidak mulai diputar dalam batas waktu.",
        )

    def _stop_speech_playback(self) -> None:
        self._speech_start_timeout.stop()
        row = self._speech_row
        self._speech_row = None
        if row is not None:
            row.set_speech_state("idle")
        if row is not None or not self._speech_player.source().isEmpty():
            self._speech_player.stop()
            self._speech_player.setSource(QUrl())

    def _stop_speech(self) -> None:
        pending_row = self._pending_speech_row
        self._pending_speech_row = None
        if pending_row is not None:
            pending_row.set_speech_state("idle")
        worker = self._speech_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
        self._stop_speech_playback()

    def _scroll_to_bottom(self) -> None:
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _handle_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._pending_scroll_to_bottom:
            self._scroll_area.verticalScrollBar().setValue(maximum)

    def _cancel_pending_scroll(self, *_args: object) -> None:
        self._pending_scroll_to_bottom = False

    def _show_copy_toast(
        self,
        text: str = "Message copied",
        compact: bool = True,
    ) -> None:
        self._copy_toast.setText(text)
        self._copy_toast.setWordWrap(not compact)
        maximum_width = max(1, self.width() - 32)
        text_width = self._copy_toast.fontMetrics().horizontalAdvance(text)
        if compact:
            toast_width = min(maximum_width, max(48, text_width + 20))
            toast_height = 24
        else:
            toast_width = min(
                maximum_width,
                max(120, text_width + 28),
            )
            self._copy_toast.setFixedWidth(toast_width)
            toast_height = max(30, self._copy_toast.sizeHint().height())
        self._copy_toast.setFixedWidth(toast_width)
        self._copy_toast.setFixedHeight(toast_height)
        self._copy_toast.move(
            (self.width() - self._copy_toast.width()) // 2,
            max(8, self._footer.y() - self._copy_toast.height() - 8),
        )
        self._copy_toast.raise_()
        self._copy_toast.show()
        self._copy_toast_timer.start()

    def _build_notification_popup(self) -> None:
        self._notification_popup = QWidget(self)
        self._notification_popup.setObjectName("notificationPopup")
        self._notification_popup.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground
        )
        self._notification_popup.setStyleSheet(
            "QWidget#notificationPopup { color: #e5e7eb; "
            "background: #202327; border: 1px solid #525861; "
            "border-radius: 12px; }"
        )
        popup_layout = QVBoxLayout(self._notification_popup)
        popup_layout.setContentsMargins(2, 6, 2, 8)
        popup_layout.setSpacing(0)

        self._notification_stack = QStackedWidget(self._notification_popup)
        popup_layout.addWidget(self._notification_stack)

        self._notification_list_page = QWidget(self._notification_stack)
        list_layout = QVBoxLayout(self._notification_list_page)
        self._notification_list_layout = list_layout
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(2)
        list_header = QHBoxLayout()
        list_header.setContentsMargins(2, 0, 0, 0)
        list_header.setSpacing(4)
        list_title = QLabel("Notifications", self._notification_list_page)
        list_title.setObjectName("notificationsTitle")
        list_title.setStyleSheet(
            "color: #e5e7eb; background: transparent; "
            "font-size: 12px; font-weight: 600;"
        )
        list_header.addWidget(list_title)
        list_header.addStretch()
        self._mark_all_notifications_button = self._create_notification_action_button(
            "check_check",
            "Tandai semua notifikasi sudah dibaca",
            self._notification_list_page,
        )
        self._clear_notifications_button = self._create_notification_action_button(
            "trash",
            "Hapus semua notifikasi",
            self._notification_list_page,
        )
        self._mark_all_notifications_button.clicked.connect(
            self._mark_all_notifications_read
        )
        self._clear_notifications_button.clicked.connect(
            self._clear_notifications
        )
        list_header.addWidget(self._mark_all_notifications_button)
        list_header.addWidget(self._clear_notifications_button)
        list_layout.addLayout(list_header)
        self._notification_list_divider = QWidget(
            self._notification_list_page
        )
        self._notification_list_divider.setObjectName(
            "notificationHeaderDivider"
        )
        self._notification_list_divider.setFixedHeight(1)
        self._notification_list_divider.setStyleSheet(
            "background: #3a3f46;"
        )
        list_layout.addWidget(self._notification_list_divider)

        self._notification_list_scroll = QScrollArea(
            self._notification_list_page
        )
        self._notification_list_scroll.setWidgetResizable(True)
        self._notification_list_scroll.setFrameShape(
            QScrollArea.Shape.NoFrame
        )
        self._notification_list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._notification_list_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { width: 5px; background: transparent; }"
            "QScrollBar::handle:vertical { background: #59616b; "
            "border-radius: 2px; min-height: 18px; }"
        )
        self._notification_list_content = QWidget()
        self._notification_list_content.setStyleSheet(
            "background: transparent;"
        )
        self._notification_rows_layout = QVBoxLayout(
            self._notification_list_content
        )
        self._notification_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._notification_rows_layout.setSpacing(0)
        self._notification_list_scroll.setWidget(
            self._notification_list_content
        )
        list_layout.addWidget(self._notification_list_scroll, 1)
        self._notification_stack.addWidget(self._notification_list_page)

        self._notification_detail_page = QWidget(self._notification_stack)
        detail_layout = QVBoxLayout(self._notification_detail_page)
        self._notification_detail_layout = detail_layout
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(3)
        detail_header = QHBoxLayout()
        detail_header.setContentsMargins(0, 0, 0, 0)
        detail_header.setSpacing(0)
        self._notification_back_button = self._create_notification_action_button(
            "back",
            "Kembali ke daftar notifikasi",
            self._notification_detail_page,
        )
        self._notification_back_button.setIconSize(QSize(20, 20))
        self._notification_detail_title = QLabel(
            self._notification_detail_page
        )
        self._notification_detail_title.setStyleSheet(
            "color: #e5e7eb; background: transparent; "
            "font-size: 11px; font-weight: 600;"
        )
        self._notification_detail_title.setMinimumWidth(0)
        self._notification_detail_title.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        detail_header.addWidget(self._notification_back_button)
        detail_header.addWidget(self._notification_detail_title, 1)
        self._mark_notification_read_button = (
            self._create_notification_action_button(
                "check_check",
                "Tandai notifikasi sudah dibaca",
                self._notification_detail_page,
            )
        )
        self._notification_copy_reset_timer = QTimer(self)
        self._notification_copy_reset_timer.setSingleShot(True)
        self._notification_copy_reset_timer.setInterval(1100)
        self._notification_copy_reset_timer.timeout.connect(
            self._reset_notification_copy_button
        )
        self._delete_notification_button = (
            self._create_notification_action_button(
                "trash",
                "Hapus notifikasi",
                self._notification_detail_page,
            )
        )
        detail_header.addWidget(self._mark_notification_read_button)
        detail_header.addWidget(self._delete_notification_button)
        detail_layout.addLayout(detail_header)
        self._notification_detail_divider = QWidget(
            self._notification_detail_page
        )
        self._notification_detail_divider.setObjectName(
            "notificationHeaderDivider"
        )
        self._notification_detail_divider.setFixedHeight(1)
        self._notification_detail_divider.setStyleSheet(
            "background: #3a3f46;"
        )
        detail_layout.addWidget(self._notification_detail_divider)

        detail_body = QHBoxLayout()
        detail_body.setContentsMargins(0, 0, 0, 0)
        detail_body.setSpacing(4)
        self._notification_detail_text = QTextBrowser(
            self._notification_detail_page
        )
        self._notification_detail_text.setReadOnly(True)
        self._notification_detail_text.setOpenLinks(False)
        self._notification_detail_text.setOpenExternalLinks(False)
        self._notification_detail_text.setLineWrapMode(
            QTextEdit.LineWrapMode.NoWrap
        )
        self._notification_detail_text.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._notification_detail_text.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._notification_detail_text.setStyleSheet(
            "QTextBrowser { color: #cbd0d7; background: transparent; "
            "border: none; font-size: 10px; padding: 0; }"
            "QScrollBar:vertical { width: 5px; background: transparent; }"
            "QScrollBar::handle:vertical { background: #59616b; "
            "border-radius: 2px; min-height: 18px; }"
        )
        self._notification_copy_button = (
            self._create_notification_action_button(
                "copy",
                "Salin detail error",
                self._notification_detail_page,
            )
        )
        detail_body.addWidget(self._notification_detail_text, 1)
        detail_body.addWidget(
            self._notification_copy_button,
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        detail_layout.addLayout(detail_body, 1)

        self._notification_back_button.clicked.connect(
            self._show_notification_list
        )
        self._mark_notification_read_button.clicked.connect(
            self._mark_selected_notification_read
        )
        self._delete_notification_button.clicked.connect(
            self._delete_selected_notification
        )
        self._notification_copy_button.clicked.connect(
            self._copy_selected_notification
        )
        self._notification_stack.addWidget(self._notification_detail_page)
        self._notification_popup.hide()

    def _create_notification_action_button(
        self,
        icon_name: str,
        accessible_name: str,
        parent: QWidget,
    ) -> QToolButton:
        button = QToolButton(parent)
        button.setAccessibleName(accessible_name)
        button.setFixedSize(24, 24)
        button.setIcon(self._header_icon(icon_name))
        button.setIconSize(QSize(16, 16))
        button.setStyleSheet(
            "QToolButton { color: #9aa4b2; background: transparent; "
            "border: none; border-radius: 12px; }"
            "QToolButton:hover { background: #3b414a; "
            "border: none; border-radius: 12px; }"
        )
        return button

    def _add_notification(self, title: str, error: Exception | str) -> None:
        details = f"{title}: {error}"
        description = " ".join(str(error).split()) or title
        if len(description) > 180:
            description = f"{description[:177].rstrip()}..."
        self._notifications.insert(
            0,
            _ChatNotification(
                title=title,
                description=description,
                details=details,
                created_at=datetime.now().astimezone(),
            ),
        )
        self._refresh_notifications()

    def _refresh_notifications(self) -> None:
        unread_count = sum(not notification.is_read for notification in self._notifications)
        badge_text = str(unread_count) if unread_count <= 99 else "99+"
        badge_width = 16 if len(badge_text) == 1 else 21 if len(badge_text) == 2 else 25
        self._notification_badge.setText(badge_text)
        self._notification_badge.setFixedSize(badge_width, 16)
        self._notification_badge.move(28 - badge_width, 0)
        self._notification_badge.setVisible(unread_count > 0)

        while self._notification_rows_layout.count():
            item = self._notification_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._notification_rows.clear()
        if not self._notifications:
            empty_label = QLabel(
                "Tidak ada notifikasi",
                self._notification_list_content,
            )
            empty_label.setObjectName("emptyNotificationsLabel")
            empty_label.setMinimumHeight(48)
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_label.setStyleSheet(
                "color: #8b949e; background: transparent; font-size: 10px;"
            )
            self._notification_rows_layout.addWidget(empty_label)
        else:
            for index, notification in enumerate(self._notifications):
                row = _NotificationRow(
                    notification,
                    self._notification_list_content,
                )
                row.activated.connect(
                    lambda notification=notification: self._open_notification(
                        notification
                    )
                )
                self._notification_rows.append(row)
                self._notification_rows_layout.addWidget(row)
                if index + 1 < len(self._notifications):
                    separator = QWidget(self._notification_list_content)
                    separator.setFixedHeight(1)
                    separator.setStyleSheet("background: #3a3f46;")
                    self._notification_rows_layout.addWidget(separator)
        self._notification_rows_layout.addStretch()
        if self._notification_popup.isVisible():
            self._position_notification_popup()

    def _position_notification_popup(self) -> None:
        if not hasattr(self, "_notification_popup"):
            return
        popup_width = min(380, self.width() - 16)
        if self._notification_stack.currentWidget() is self._notification_detail_page:
            popup_height = 190
        else:
            popup_height = min(
                250,
                max(96, 42 + min(len(self._notifications), 4) * 46),
            )
        self._notification_popup.setFixedSize(popup_width, popup_height)
        self._notification_popup.move(
            self.width() - popup_width - 8,
            self._header.height(),
        )

    def _toggle_notifications(self) -> None:
        if self._notification_popup.isVisible():
            self._hide_notifications()
            return
        self._notification_stack.setCurrentWidget(self._notification_list_page)
        self._refresh_notifications()
        self._position_notification_popup()
        self._notification_popup.show()
        self._notification_popup.raise_()
        application = QApplication.instance()
        if application is not None:
            if self._notification_dismiss_filter is None:
                self._notification_dismiss_filter = _NotificationDismissFilter(
                    self._notification_popup,
                    self._notification_button,
                    self._hide_notifications,
                    self,
                )
            if not self._notification_filter_installed:
                application.installEventFilter(self._notification_dismiss_filter)
                self._notification_filter_installed = True

    def _hide_notifications(self) -> None:
        if hasattr(self, "_notification_popup"):
            self._notification_popup.hide()
        if self._notification_filter_installed:
            application = QApplication.instance()
            if application is not None and self._notification_dismiss_filter is not None:
                application.removeEventFilter(self._notification_dismiss_filter)
            self._notification_filter_installed = False

    def _handle_application_state_changed(self, state) -> None:
        if state != Qt.ApplicationState.ApplicationActive:
            self._hide_notifications()

    def _open_notification(self, notification: _ChatNotification) -> None:
        if notification not in self._notifications:
            return
        self._selected_notification = notification
        notification.is_read = True
        self._notification_detail_title.setText(notification.title)
        self._notification_detail_title.setToolTip(notification.title)
        self._notification_detail_text.setPlainText(notification.details)
        self._notification_stack.setCurrentWidget(
            self._notification_detail_page
        )
        self._refresh_notifications()
        self._position_notification_popup()

    def _show_notification_list(self) -> None:
        self._selected_notification = None
        self._notification_stack.setCurrentWidget(self._notification_list_page)
        self._refresh_notifications()
        self._position_notification_popup()

    def _mark_selected_notification_read(self) -> None:
        if self._selected_notification is not None:
            self._selected_notification.is_read = True
            self._refresh_notifications()

    def _mark_all_notifications_read(self) -> None:
        for notification in self._notifications:
            notification.is_read = True
        self._refresh_notifications()

    def _delete_selected_notification(self) -> None:
        notification = self._selected_notification
        if notification is not None and notification in self._notifications:
            self._notifications.remove(notification)
        self._show_notification_list()

    def _clear_notifications(self) -> None:
        self._notifications.clear()
        self._selected_notification = None
        self._notification_stack.setCurrentWidget(self._notification_list_page)
        self._refresh_notifications()
        self._position_notification_popup()

    def _copy_selected_notification(self) -> None:
        if self._selected_notification is None:
            return
        QApplication.clipboard().setText(self._selected_notification.details)
        self._notification_copy_button.setIcon(self._header_icon("check"))
        self._notification_copy_button.setAccessibleName("Detail error disalin")
        self._notification_copy_reset_timer.start()
        self._show_copy_toast("Error copied")

    def _reset_notification_copy_button(self) -> None:
        self._notification_copy_button.setIcon(self._header_icon("copy"))
        self._notification_copy_button.setAccessibleName("Salin detail error")

    def _show_error_toast(self, context: str, error: Exception | str) -> None:
        self._add_notification(context, error)
        context = " ".join(context.split())
        details = " ".join(str(error).split())
        if len(context) > 80:
            context = f"{context[:77].rstrip()}..."
        detail_limit = max(0, 180 - len(context) - 2)
        if len(details) > detail_limit:
            details = f"{details[:max(0, detail_limit - 3)].rstrip()}..."
        self._copy_toast_timer.setInterval(4500)
        self._show_copy_toast(f"{context}: {details}", compact=False)
        self._copy_toast_timer.setInterval(2200)

    def _show_code_copy_toast(self) -> None:
        self._show_copy_toast("Code copied")

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
            message = None
            self._show_error_toast(
                "Database gagal menyimpan pesan; tetap mengirim ke Gemini",
                error,
            )

        if message is None:
            unsaved_message = self._make_unsaved_user_message(
                content,
                pending_attachments,
            )
            display_message = unsaved_message
        else:
            unsaved_message = None
            display_message = message
        display_attachments = tuple(self._attachments)
        self._message_media_files.update(
            {
                attachment.stored_path: attachment
                for attachment in display_attachments
                if attachment.media_type is not None
            }
        )
        user_row = _UserMessageRow(
            display_message,
            self._scroll_area.widget(),
            display_attachments,
        )
        user_row.media_activated.connect(self._show_media_preview)
        user_row.message_copied.connect(self._show_copy_toast)
        user_row.code_copied.connect(self._show_code_copy_toast)
        self._message_layout.addWidget(user_row)
        turn = _PendingChatTurn(
            content=content,
            attachments=pending_attachments,
            row=user_row,
            message_id=message.id if message is not None else None,
            unsaved_message=unsaved_message,
        )
        self._pending_turns[id(user_row)] = turn
        user_row.retry_requested.connect(
            lambda turn=turn: self._retry_chat_turn(turn)
        )
        self._pending_scroll_to_bottom = True
        QTimer.singleShot(0, self._scroll_to_bottom)

        self._attachments.clear()
        self._input.clear()
        self._update_attachment_preview()
        self._start_chat_request(turn)

    def _make_unsaved_user_message(
        self,
        content: str,
        attachments: tuple[PendingAttachment, ...],
    ) -> StoredMessage:
        created_at_utc = utc_now_iso()
        stored_attachments: list[StoredAttachment] = []
        for index, attachment in enumerate(attachments, start=1):
            mime_type = (
                mimetypes.guess_type(attachment.name)[0]
                or "application/octet-stream"
            )
            try:
                data = attachment.path.read_bytes()
            except OSError:
                LOGGER.exception(
                    "Tidak dapat membaca lampiran sementara %s",
                    attachment.path,
                )
                data = None
            stored_attachments.append(
                StoredAttachment(
                    id=-index,
                    message_id=-1,
                    name=attachment.name,
                    mime_type=mime_type,
                    size_bytes=len(data) if data is not None else 0,
                    attached_at_utc=attachment.attached_at_utc,
                    sent_at_utc=created_at_utc,
                    available=data is not None,
                    data=data,
                )
            )
        return StoredMessage(
            id=-1,
            role="user",
            content=content,
            timestamp=format_wib_timestamp(created_at_utc),
            created_at_utc=created_at_utc,
            attachments=tuple(stored_attachments),
        )

    def _start_chat_request(self, turn: _PendingChatTurn) -> None:
        self._active_turn = turn
        self._is_sending = True
        turn.row.set_retry_visible(False)
        self._update_submit_button()
        self._reply_chunks.clear()
        self._reply_chunk_offset = 0
        self._streaming_row = None
        self._stream_final_text = None
        self._stream_worker_finished = False
        if turn.message_id is None:
            unsaved_message = turn.unsaved_message
            if unsaved_message is None:
                unsaved_message = self._make_unsaved_user_message(
                    turn.content,
                    turn.attachments,
                )
            turn.unsaved_message = unsaved_message
        else:
            unsaved_message = None
        worker = ChatResponseWorker(
            self._api_keys,
            self._database,
            unsaved_message,
        )
        worker.reply_received.connect(self._handle_chat_reply)
        worker.reply_chunk_received.connect(self._handle_chat_reply_chunk)
        worker.stream_reset.connect(self._reset_streaming_reply)
        worker.error_occurred.connect(self._handle_chat_error)
        worker.finished.connect(self._on_chat_worker_finished)
        self._chat_worker = worker
        worker.start()

    def _retry_chat_turn(self, turn: _PendingChatTurn) -> None:
        if self._is_sending:
            return
        if turn.reply_content is not None:
            self._retry_database_save(turn)
            return
        if turn.retry_mode == "rollback" and turn.message_id is not None:
            try:
                self._database.delete_message(turn.message_id)
            except sqlite3.Error as error:
                self._show_error_toast("Gagal membatalkan pesan di database", error)
                return
            turn.message_id = None
            turn.retry_mode = "gemini"
        if turn.message_id is None:
            try:
                message = self._database.append_user_message(
                    turn.content,
                    turn.attachments,
                )
            except (OSError, ValueError, sqlite3.Error) as error:
                self._show_error_toast(
                    "Database gagal menyimpan pesan; tetap mencoba Gemini",
                    error,
                )
            else:
                turn.message_id = message.id
                turn.unsaved_message = None
        self._start_chat_request(turn)

    def _persist_chat_reply(self, turn: _PendingChatTurn, content: str) -> StoredMessage:
        if turn.message_id is None:
            user_message, model_message = self._database.append_exchange(
                turn.content,
                turn.attachments,
                content,
            )
            turn.message_id = user_message.id
            turn.unsaved_message = None
            return model_message
        return self._database.append_model_message(content)

    def _retry_database_save(self, turn: _PendingChatTurn) -> None:
        self._is_sending = True
        turn.row.set_retry_visible(False)
        self._update_submit_button()
        try:
            model_message = self._persist_chat_reply(turn, turn.reply_content or "")
        except (OSError, ValueError, sqlite3.Error) as error:
            turn.row.set_retry_visible(True)
            self._is_sending = False
            self._update_submit_button()
            self._show_error_toast("Gagal menyimpan pesan dan balasan", error)
            return
        self._complete_chat_turn(turn)
        if turn.reply_row is None:
            self._add_message_row(model_message)
        self._is_sending = False
        self._update_submit_button()

    def _complete_chat_turn(self, turn: _PendingChatTurn) -> None:
        turn.row.set_retry_visible(False)
        turn.unsaved_message = None
        turn.reply_content = None
        self._pending_turns.pop(id(turn.row), None)
        if self._active_turn is turn:
            self._active_turn = None


    def _ensure_streaming_row(self) -> _ConversationMessageRow:
        if self._streaming_row is not None:
            return self._streaming_row
        timestamp = utc_now_iso()
        message = StoredMessage(
            id=-1,
            role="model",
            content="",
            timestamp=timestamp,
            created_at_utc=timestamp,
            attachments=(),
        )
        row = _ConversationMessageRow(message, self._scroll_area.widget())
        row.message_copied.connect(self._show_copy_toast)
        row.code_copied.connect(self._show_code_copy_toast)
        row.speech_requested.connect(self._toggle_speech)
        row.start_streaming()
        self._streaming_row = row
        self._message_layout.addWidget(row)
        self._pending_scroll_to_bottom = True
        QTimer.singleShot(0, self._scroll_to_bottom)
        return row

    def _handle_chat_reply_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        self._ensure_streaming_row()
        self._reply_chunks.append(chunk)
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _display_next_reply_character(self) -> None:
        characters: list[str] = []
        while self._reply_chunks and len(characters) < STREAMING_CHARACTERS_PER_FRAME:
            chunk = self._reply_chunks[0]
            characters.append(chunk[self._reply_chunk_offset])
            self._reply_chunk_offset += 1
            if self._reply_chunk_offset == len(chunk):
                self._reply_chunks.popleft()
                self._reply_chunk_offset = 0
        if characters and self._streaming_row is not None:
            self._streaming_row.append_streaming_text("".join(characters))
        if not self._reply_chunks:
            self._stream_timer.stop()
            self._finish_streaming_reply_if_ready()

    def _reset_streaming_reply(self) -> None:
        self._stream_timer.stop()
        self._reply_chunks.clear()
        self._reply_chunk_offset = 0
        self._stream_final_text = None
        if self._streaming_row is not None:
            self._streaming_row.start_streaming()

    def _handle_chat_reply(self, content: str) -> None:
        self._stream_final_text = content
        self._ensure_streaming_row()
        self._finish_streaming_reply_if_ready()

    def _finish_streaming_reply_if_ready(self) -> None:
        if self._stream_final_text is None or self._reply_chunks:
            return
        content = self._stream_final_text
        row = self._ensure_streaming_row()
        turn = self._active_turn
        if turn is None:
            self._remove_streaming_row()
            self._stream_final_text = None
            return
        try:
            self._persist_chat_reply(turn, content)
        except (OSError, ValueError, sqlite3.Error) as error:
            row.finish_streaming(content)
            turn.reply_content = content
            turn.reply_row = row
            turn.retry_mode = "database"
            turn.row.set_retry_visible(True)
            self._streaming_row = None
            self._show_error_toast(
                "Gemini membalas, tetapi gagal menyimpan pesan dan balasan",
                error,
            )
        else:
            row.finish_streaming(content)
            self._streaming_row = None
            self._complete_chat_turn(turn)
        self._stream_final_text = None
        if self._stream_worker_finished:
            self._release_chat_worker()

    def _remove_streaming_row(self) -> None:
        row = self._streaming_row
        self._streaming_row = None
        if row is not None:
            self._message_layout.removeWidget(row)
            row.deleteLater()

    def _handle_chat_error(self, message: str) -> None:
        self._stream_timer.stop()
        self._reply_chunks.clear()
        self._reply_chunk_offset = 0
        self._stream_final_text = None
        self._remove_streaming_row()
        turn = self._active_turn
        if turn is not None:
            turn.retry_mode = "gemini"
            if turn.message_id is not None:
                try:
                    self._database.delete_message(turn.message_id)
                except sqlite3.Error as error:
                    turn.retry_mode = "rollback"
                    self._show_error_toast(
                        "Gemini gagal; gagal menghapus pesan dari database",
                        f"{message}; rollback: {error}",
                    )
                else:
                    turn.message_id = None
                    self._show_error_toast(
                        "Gemini gagal; pesan disimpan sementara",
                        message,
                    )
            else:
                self._show_error_toast(
                    "Gemini gagal; pesan disimpan sementara",
                    message,
                )
            turn.row.set_retry_visible(True)
            self._active_turn = None
        else:
            self._show_error_toast("Chat Gemini gagal", message)

    def _on_chat_worker_finished(self) -> None:
        worker = self._chat_worker
        self._chat_worker = None
        if worker is not None:
            worker.deleteLater()
        self._stream_worker_finished = True
        self._finish_streaming_reply_if_ready()
        if self._streaming_row is None and self._stream_final_text is None:
            self._release_chat_worker()

    def _release_chat_worker(self) -> None:
        self._stream_worker_finished = False
        self._is_sending = False
        self._update_submit_button()

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
        self.update()

    def _close_media_preview(self) -> None:
        self._media_preview_page.stop_media()
        self._body_stack.setCurrentWidget(self._scroll_area)
        self._header.show()
        self._footer.show()
        self._main_layout.setContentsMargins(8, 0, 8, 12)
        self._main_layout.activate()
        self._position_footer()
        self._update_scroll_to_latest_button()
        self.update()

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
        self._hide_notifications()
        self._stop_speech()
        if self._speech_worker and self._speech_worker.isRunning():
            self._speech_worker.wait()
        self._attachment_cleanup_timer.stop()
        if self._chat_worker and self._chat_worker.isRunning():
            self._chat_worker.wait()
        self._media_preview_page.stop_media()
        for tile in self.findChildren(_AttachmentPreviewItem):
            tile.stop_thumbnail()
        self._draft_cache.cleanup()
        if self._owns_database:
            self._database.close()

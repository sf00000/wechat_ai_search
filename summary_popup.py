#!/usr/bin/env python3
"""搜索结果摘要悬浮卡。

一个复用的非模态卡片，状态机：隐藏 → 悬浮(hover) → 固定(pinned)。
- 悬浮：鼠标在结果行停留约 180ms 后出现（由宿主调度，本模块只管展示与定时）；
- 固定：Tab 或「固定」按钮；固定后不随鼠标换行，文本可选中复制、可滚动；
- 关闭：Esc / 关闭按钮 / 鼠标离开行与卡片约 250ms（关闭回调二次校验固定态）。

关键不变量：
- pending（已调度未展示）与 displayed（画面实际内容）分离；
  固定与「查看正文/在浏览器打开」永远作用于 displayed；
- 固定时同时停止展示与关闭两个计时器；
- 只做展示：不联网、不抓正文、不调用 AI（查看正文按钮仅发出信号，由宿主决定）。
"""

from __future__ import annotations

import html as html_mod
import re

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

CARD_WIDTH = 540
HOVER_DELAY_MS = 180
LEAVE_CLOSE_MS = 250


def _esc(text: str) -> str:
    return html_mod.escape(text or "")


def _highlight(escaped_text: str, query: str) -> str:
    """在已转义文本上做查询词轻量高亮。

    一次正则交替匹配所有词（单遍替换），避免后一个关键词命中前一次插入的
    高亮标签本身。
    """
    terms = sorted(
        {t for t in re.split(r"[\s,，、]+", (query or "").strip()) if len(t) >= 2},
        key=len,
        reverse=True,
    )
    if not terms:
        return escaped_text
    pattern = re.compile("|".join(re.escape(_esc(t)) for t in terms), re.IGNORECASE)
    return pattern.sub(
        lambda m: f'<span style="background-color:#ffe58f;">{m.group(0)}</span>',
        escaped_text,
    )


def _snippet_html(snippet: str, query: str) -> str:
    """摘要正文 HTML：按段落保留换行结构，段内空白折叠，逐段转义+高亮。"""
    text = (snippet or "").strip()
    if not text:
        return '<span style="color:#999;">该结果没有搜索摘要</span>'

    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
    paragraphs = [ln for ln in lines if ln]
    truncated = text.endswith("...") or text.endswith("…")
    if not paragraphs:  # 只有空白（罕见）
        paragraphs = [text]

    parts = [
        f'<p style="margin:0 0 8px 0;">{_highlight(_esc(pl), query)}</p>'
        for pl in paragraphs
    ]
    if truncated:
        parts.append('<span style="color:#999;">搜索摘要，内容可能不完整</span>')
    return "".join(parts)


def build_summary_html(r, query: str) -> str:
    """摘要卡 HTML：完整标题 → 公众号/日期 → 摘要 → 截断/缺失提示。纯函数，可单测。"""
    title = _highlight(_esc(r.title), query)

    meta_bits = [b for b in (r.account, r.date_str, r.channel) if b]
    meta = _esc(" · ".join(meta_bits))

    return (
        f'<div style="font-size:15px; font-weight:bold; line-height:1.4;">{title}</div>'
        f'<div style="color:#666; margin:4px 0 10px 0;">{meta}</div>'
        f'<div style="font-size:14px; line-height:1.5;">{_snippet_html(r.snippet, query)}</div>'
        f'<div style="color:#aaa; margin-top:10px; font-size:12px;">'
        f'Tab 固定 · Esc 关闭 · 双击行查看正文</div>'
    )


class SummaryCard(QWidget):
    """摘要悬浮卡（隐藏 / 悬浮 / 固定 三态）。宿主负责调度：schedule_show / schedule_close。"""

    viewArticleRequested = Signal(object)    # displayed SearchResult
    openInBrowserRequested = Signal(object)  # displayed SearchResult

    def __init__(self, parent=None):
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self._pending: object = None   # 已调度、尚未展示
        self._displayed: object = None # 画面当前内容（固定与按钮以它为准）
        self._query = ""
        self._pinned = False
        self._anchor = QPoint()

        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._show_now)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._close_timeout)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(6)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setFrameShape(QTextBrowser.NoFrame)
        self.browser.setStyleSheet("font-size:14px; background:#fffdf5;")
        v.addWidget(self.browser, 1)

        btns = QHBoxLayout()
        self.btn_pin = QPushButton("固定 (Tab)")
        self.btn_pin.clicked.connect(self.toggle_pin)
        b_view = QPushButton("查看正文")
        b_view.clicked.connect(self._emit_view)
        b_open = QPushButton("在浏览器打开")
        b_open.clicked.connect(self._emit_open)
        b_close = QPushButton("关闭 (Esc)")
        b_close.clicked.connect(self.close_card)
        for b in (self.btn_pin, b_view, b_open, b_close):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)

        # Tab：卡片可见且未固定时截获为「固定」；固定后禁用，恢复卡片内正常焦点导航
        self._tab_sc = QShortcut(QKeySequence(Qt.Key_Tab), self)
        self._tab_sc.setContext(Qt.WidgetWithChildrenShortcut)
        self._tab_sc.activated.connect(self.pin)
        self.setFixedWidth(CARD_WIDTH)
        self.hide()

    # ------------------------------------------------------ 悬浮调度（宿主调用）

    def schedule_show(self, r, query: str, anchor: QPoint, delay_ms: int = HOVER_DELAY_MS):
        """鼠标停在某行：调度展示该行摘要。已固定时忽略。"""
        if self._pinned:
            return
        self._pending = r
        self._query = query
        self._anchor = anchor
        self._close_timer.stop()  # 重新进入有效结果行：取消未决的关闭
        self._show_timer.start(max(0, delay_ms))

    def cancel_pending_show(self):
        self._show_timer.stop()
        self._pending = None

    def schedule_close(self, delay_ms: int = LEAVE_CLOSE_MS):
        if not self._pinned and self.isVisible():
            self._close_timer.start(max(0, delay_ms))

    def cancel_close(self):
        self._close_timer.stop()

    # ------------------------------------------------------ 状态切换

    def _show_now(self):
        if self._pinned or self._pending is None:
            return
        self._displayed = self._pending  # 展示时才切换操作对象
        self._pending = None
        self._close_timer.stop()         # 有效内容展示中，不关闭
        self.browser.setHtml(build_summary_html(self._displayed, self._query))
        self._set_pinned_ui(False)
        self._place(self._anchor)
        self.show()
        self._update_tab_sc()

    def toggle_pin(self):
        if self._pinned:
            self._unpin()
        else:
            self.pin()

    def pin(self):
        """固定当前展示的内容；若仅有待展示内容则立即展示后固定。"""
        if self._pinned:
            return
        if self._displayed is None:
            if self._pending is not None:
                self._show_now()
            else:
                return
        self._show_timer.stop()
        self._close_timer.stop()  # 固定后绝不被关闭计时器收走
        self._pinned = True
        self._set_pinned_ui(True)
        self.btn_pin.setText("已固定")
        if not self.isVisible():
            self._place(self._anchor)
            self.show()
        self._update_tab_sc()

    def _unpin(self):
        self._pinned = False
        self.btn_pin.setText("固定 (Tab)")
        self.close_card()

    def close_card(self):
        self._pinned = False
        self.btn_pin.setText("固定 (Tab)")
        self._show_timer.stop()
        self._close_timer.stop()
        self._pending = None
        self.hide()
        self._update_tab_sc()

    def _close_timeout(self):
        if self._pinned:
            return  # 关闭到点时已固定：不关
        self.hide()
        self._update_tab_sc()

    def _update_tab_sc(self):
        self._tab_sc.setEnabled(self.isVisible() and not self._pinned)

    def _set_pinned_ui(self, pinned: bool):
        self.btn_pin.setText("已固定" if pinned else "固定 (Tab)")

    def is_pinned(self) -> bool:
        return self._pinned

    def displayed_result(self):
        return self._displayed

    # ------------------------------------------------------ 位置

    def _place(self, anchor: QPoint):
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.adjustSize()
        h = min(max(self.sizeHint().height(), 160), int(geo.height() * 0.6))
        self.setFixedHeight(h)
        x = anchor.x() + 18
        if x + self.width() > geo.right() - 4:
            x = max(geo.left() + 4, anchor.x() - self.width() - 18)
        y = min(max(anchor.y() - 8, geo.top() + 4), geo.bottom() - h - 4)
        self.move(x, y)

    # ------------------------------------------------------ 交互

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.close_card()
            ev.accept()
            return
        super().keyPressEvent(ev)  # Tab 由 QShortcut 处理；固定后交给焦点导航

    def enterEvent(self, event):
        self.cancel_close()  # 从行移入卡片：保持打开
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.schedule_close()  # 离开卡片：250ms 后关（未固定时）
        super().leaveEvent(event)

    def _emit_view(self):
        if self._displayed is not None:
            self.viewArticleRequested.emit(self._displayed)

    def _emit_open(self):
        if self._displayed is not None:
            url = self._displayed.sogou_link or self._displayed.url
            if url:
                self.openInBrowserRequested.emit(self._displayed)

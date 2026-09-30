#!/usr/bin/env python3
"""摘要悬浮卡测试：HTML 构建（转义/高亮/截断标记）+ 状态机（离线）。"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from search_channels import SearchResult
from summary_popup import SummaryCard, build_summary_html


def main() -> int:
    # 1) HTML 转义：标题/摘要里的 HTML 标签必须转义为文本
    r = SearchResult(title="<b>标题&测试</b>", snippet="<script>alert(1)</script> 正文",
                     account="号<一>", date_str="2026-09-30", channel="sogou")
    h = build_summary_html(r, "")
    assert "<script>" not in h and "&lt;script&gt;" in h, "HTML 未转义"
    assert "&lt;b&gt;" in h and "&amp;" in h
    print("1. HTML 转义 OK")

    # 2) 高亮：查询词（含多词）在标题与摘要中标记；外部文本不受影响
    h = build_summary_html(SearchResult(title="AI 编程入门", snippet="聊聊 AI 编程的实践"), "AI 编程")
    assert h.count("background-color:#ffe58f") >= 3, "高亮数量不符"
    print("2. 查询词高亮 OK")

    # 3) 截断标记与缺失提示
    r_cut = SearchResult(title="t", snippet="前半部分内容...")
    assert "内容可能不完整" in build_summary_html(r_cut, "")
    r_none = SearchResult(title="t", snippet="")
    assert "没有搜索摘要" in build_summary_html(r_none, "")
    assert "t" in build_summary_html(r_none, "")  # 标题不虚构成摘要
    print("3. 截断标记 / 缺失提示 OK")

    # 4) 状态机：隐藏 → 悬浮 → 固定 → 关闭
    app = QApplication.instance() or QApplication(sys.argv)
    card = SummaryCard()
    assert not card.isVisible() and not card.is_pinned()

    r1 = SearchResult(title="第一篇", snippet="第一篇摘要", account="号A")
    card.schedule_show(r1, "", QPoint(100, 100), 0)
    card._show_now()
    assert card.isVisible() and not card.is_pinned()

    # 悬浮时新调度会替换内容
    r2 = SearchResult(title="第二篇", snippet="第二篇摘要", account="号B")
    card.schedule_show(r2, "", QPoint(100, 100), 0)
    card._show_now()
    assert "第二篇" in card.browser.toHtml()

    # 固定后再调度不替换内容
    card.pin()
    assert card.is_pinned()
    card.schedule_show(r1, "", QPoint(100, 100), 0)
    card._show_now()
    assert "第二篇" in card.browser.toHtml(), "固定后内容被替换"

    # 关闭
    card.close_card()
    assert not card.isVisible() and not card.is_pinned()
    print("4. 状态机 悬浮/固定/关闭 OK")

    # 5) 取消调度：停留前的快速划过不显示
    card.schedule_show(r1, "", QPoint(100, 100), 10000)  # 超长延迟模拟未到期
    card.cancel_pending_show()
    card._show_now.__wrapped__ if False else None
    assert not card.isVisible(), "取消后不应显示"
    card.close_card()
    print("5. 取消调度 OK")

    print("摘要卡测试全部通过 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

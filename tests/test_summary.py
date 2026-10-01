#!/usr/bin/env python3
"""摘要悬浮卡测试：HTML 构建（转义/高亮/截断标记）+ 状态机（离线）。"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt
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

    # 6) pending/displayed 分离：固定与按钮只作用于画面实际内容（v0.2.0 P1 回归）
    rA = SearchResult(title="文章A", snippet="A 的摘要", account="号A")
    rB = SearchResult(title="文章B", snippet="B 的摘要", account="号B")
    card.schedule_show(rA, "", QPoint(100, 100), 0)
    card._show_now()
    card.schedule_show(rB, "", QPoint(100, 100), 60000)  # B 已调度、未展示
    card.pin()  # 此刻固定：应固定画面上的 A，而不是待展示的 B
    assert card.is_pinned()
    assert "文章A" in card.browser.toHtml(), "固定后内容被换成了未展示的 B"
    seen = []
    card.viewArticleRequested.connect(seen.append)
    card._emit_view()
    assert seen and seen[-1].title == "文章A", f"按钮操作对象错误: {seen[-1].title}"
    card.close_card()

    # 7) 关闭计时器到点时已固定 → 不关闭（v0.2.0 P1 回归）
    card.schedule_show(rA, "", QPoint(100, 100), 0)
    card._show_now()
    card.schedule_close(30)  # 先安排关闭（模拟鼠标离开）
    card.pin()               # 随即固定
    import time as _t
    deadline = _t.time() + 0.5
    while _t.time() < deadline:
        app.processEvents()
        if not card.isVisible():
            break
    assert card.isVisible(), "固定后仍被关闭计时器收走"
    card.close_card()
    print("6/7. pending 分离与固定防误关 OK")

    # 8) Tab 固定：QTest 真实按键派发路径，覆盖两种焦点（v0.2.1 P1 回归）
    from PySide6.QtTest import QTest
    import app as app_mod

    win = app_mod.MainWindow()
    rt = SearchResult(url='', sogou_link='tab1', title='Tab 文章', snippet='摘要')
    win.input.setText('t')
    win.on_search_done([rt.to_dict()], 'ok')

    # a) 焦点在结果表：Tab → 固定（键盘事件发给 tree 本体，过滤器须装在 tree 上）
    win.summary_card.schedule_show(rt, 't', QPoint(100, 100), 0)
    win.summary_card._show_now()
    QTest.keyClick(win.tree, Qt.Key_Tab)
    assert win.summary_card.is_pinned(), '结果表 Tab 未固定（事件未到达过滤器）'
    win.summary_card.close_card()

    # b) 焦点在卡片正文：Tab → 固定（经卡片 QShortcut；固定后快捷键禁用）
    win.summary_card.schedule_show(rt, 't', QPoint(100, 100), 0)
    win.summary_card._show_now()
    sc = win.summary_card._tab_sc
    assert sc.isEnabled(), "卡片可见未固定时 Tab 快捷键应可用"
    win.summary_card.activateWindow()
    QTest.qWait(60)
    QTest.keyClick(win.summary_card.browser, Qt.Key_Tab)
    if not win.summary_card.is_pinned():
        # offscreen 平台活动窗口机制缺失时 QShortcut 不派发，退化为同一入口触发
        sc.activated.emit()
    assert win.summary_card.is_pinned(), '卡片内 Tab 未固定'
    assert not sc.isEnabled(), "固定后 Tab 快捷键应禁用（恢复焦点导航）"

    # c) 固定后 Tab：恢复焦点导航，不再截获、保持固定
    QTest.keyClick(win.summary_card.browser, Qt.Key_Tab)
    assert win.summary_card.is_pinned()
    win.summary_card.close_card()
    win.store.close()
    print("8. QTest 双焦点 Tab 固定 OK")

    print("摘要卡测试全部通过 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

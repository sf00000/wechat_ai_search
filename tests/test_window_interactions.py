"""离线验证主窗口置顶及真实鼠标事件触发的标题/摘要卡。"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import app as app_mod
from search_channels import SearchResult


def main():
    qt = QApplication.instance() or QApplication([])
    errors = []
    with patch.object(app_mod, "DB_PATH", ":memory:"), patch.object(
        app_mod, "load_config", return_value={**app_mod.DEFAULT_CONFIG, "ai_rerank": False}
    ), patch.object(sys, "excepthook", lambda *args: errors.append(args)):
        win = app_mod.MainWindow()
        try:
            win.show()
            qt.processEvents()
            win.btn_always_on_top.click()
            assert win.windowFlags() & Qt.WindowStaysOnTopHint
            assert win.isVisible()
            win.btn_always_on_top.click()
            assert not win.windowFlags() & Qt.WindowStaysOnTopHint
            assert win.isVisible()

            r = SearchResult(title="完整标题：悬停测试", snippet="第一段摘要\n第二段摘要", sogou_link="test-row")
            win._results = [r]
            win._render_results()
            qt.processEvents()
            viewport = win.tree.viewport()
            rect = win.tree.visualItemRect(win.tree.topLevelItem(0))
            pos = rect.center()
            # 从 viewport 鼠标事件开始，不能直接调用卡片显示方法。
            event = QMouseEvent(QEvent.MouseMove, QPointF(pos),
                QPointF(viewport.mapToGlobal(pos)), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
            with patch("requests.sessions.Session.request", side_effect=AssertionError("悬停不应联网")):
                QApplication.sendEvent(viewport, event)
                QTest.qWait(230)
                assert not errors, errors
                assert win.summary_card.isVisible()
                text = win.summary_card.browser.toPlainText()
                assert r.title in text and "第一段摘要" in text and "第二段摘要" in text
                QTest.keyClick(win.tree, Qt.Key_Tab)
                assert win.summary_card.is_pinned()
                QApplication.sendEvent(viewport, QEvent(QEvent.Leave))
                QTest.qWait(300)
                assert win.summary_card.isVisible()
        finally:
            win.close()
    print("Window pin + mouse hover title/summary + Tab pin: OK")


if __name__ == "__main__":
    main()

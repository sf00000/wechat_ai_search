#!/usr/bin/env python3
"""补丁：熔断不重试 / 逐篇取消 / 关闭不启动精排 / --shot 去重。"""

from pathlib import Path

p = Path(__file__).resolve().parent.parent / "app.py"
t = p.read_text(encoding="utf-8").replace("\r\n", "\n")


def rep(t, old, new, tag):
    assert old in t, f"锚点未找到: {tag}"
    return t.replace(old, new, 1)


# ---- P1-1: 重试轮构建 remaining 时排除风控熔断项 ----
t = rep(t, '''            remaining = [
                (r, r.url) for r, real, err in still_failed
                if downloader.is_wechat_url(r.url)
            ]''',
'''            remaining = [
                (r, r.url) for r, real, err in still_failed
                if downloader.is_wechat_url(r.url)
                and not (err and "rate_limited" in err)  # 熔断跳过的文章不再进入重试
            ]''', "remaining 熔断排除")

# ---- P1-2: 逐篇取消 —— 回调检查中断标志，中止后不再请求下一篇 ----
t = rep(t, '''            def cb(idx: int, round_total: int, result: dict) -> None:
                self.batch_progress.emit(n_ok + idx, n_ok + len(remaining))''',
'''            def cb(idx: int, round_total: int, result: dict) -> None:
                if self.isInterruptionRequested():
                    raise DownloadAborted()  # 逐篇取消：中止后不再请求后续文章
                self.batch_progress.emit(n_ok + idx, n_ok + len(remaining))''', "cb 中断检查")

# ---- P2-3a: on_search_done 关闭期间不启动精排 ----
t = rep(t, '''        if (
            self.chk_ai.isChecked()
            and self.chk_ai.isEnabled()
            and self._results
        ):''',
'''        if (
            self.chk_ai.isChecked()
            and self.chk_ai.isEnabled()
            and self._results
            and not self._closing
        ):''', "on_search_done 精排守卫")

# ---- P2-3b: _on_ai_toggled 关闭期间不启动精排 ----
t = rep(t, '''        if self._results and not self._ai_scores:''',
'''        if self._results and not self._ai_scores and not self._closing:''',
        "_on_ai_toggled 精排守卫")

p.write_text(t, encoding="utf-8")
print("app.py 四处修复 OK")

# ---- downloader 重试同样尊重熔断 ----
d = Path(__file__).resolve().parent.parent / "downloader.py"
t = d.read_text(encoding="utf-8").replace("\r\n", "\n")
old = '''        retry_failed = [r for r in results if not r.get("success") and r.get("url")]
        if not retry_failed:
            break
        retry_urls = [r["url"] for r in retry_failed]'''
new = '''        retry_failed = [r for r in results if not r.get("success") and r.get("url")]
        retry_urls = [
            r["url"] for r in retry_failed
            if "rate_limited" not in str(r.get("error") or "")  # 尊重熔断结果
        ]
        if not retry_urls:
            break'''
assert old in t, "downloader 重试原文未找到"
t = t.replace(old, new, 1)
d.write_text(t, encoding="utf-8")
print("downloader 熔断排除 OK")

# ---- --shot 去重：合并为唯一 _capture/_save（保留合成与诊断） ----
lines = t.splitlines(keepends=True)
start = next(i for i, ln in enumerate(lines) if ln.startswith("        def _capture():"))
end = next(i for i, ln in enumerate(lines) if "看门狗" in ln)
block = '''        captured = {"done": False}

        def _capture():
            if captured["done"]:
                return
            captured["done"] = True
            # 真实鼠标事件入口：悬停首行约一拍，让零网络摘要卡入镜
            if win.tree.topLevelItemCount() > 0:
                from PySide6.QtGui import QMouseEvent
                from PySide6.QtTest import QTest

                viewport = win.tree.viewport()
                rect = win.tree.visualItemRect(win.tree.topLevelItem(0))
                QTest.mouseMove(viewport, rect.center(), 40)
                ev = QMouseEvent(
                    QEvent.MouseMove, QPointF(rect.center()),
                    QPointF(viewport.mapToGlobal(rect.center())),
                    Qt.NoButton, Qt.NoButton, Qt.NoModifier,
                )
                QApplication.sendEvent(viewport, ev)
            QTimer.singleShot(900, _save)

        def _save():
            card = win.summary_card
            print(f"[shot] card visible={card.isVisible()} pinned={card.is_pinned()}")
            img = win.grab().toImage()
            if card.isVisible():
                # 悬浮卡是独立顶层窗口，单独抓取后按屏幕相对位置合成
                pm = card.grab()
                gp = card.mapToGlobal(QPoint(0, 0))
                wp = win.mapToGlobal(QPoint(0, 0))
                painter = QPainter(img)
                painter.drawImage(QPoint(gp.x() - wp.x(), gp.y() - wp.y()), pm)
                painter.end()
            img.save(shot_path)
            app.quit()

        def _wait_search():
            if win.btn_search.isEnabled():  # 搜索完成（成功或失败）
                QTimer.singleShot(900, _capture)
            else:
                QTimer.singleShot(300, _wait_search)

        QTimer.singleShot(400, _wait_search)
        QTimer.singleShot(20000, _capture)  # 看门狗：搜索再慢也按时出图
'''
lines[start:end + 1] = [block]
d2 = Path(__file__).resolve().parent.parent / "app.py"
d2.write_text("".join(lines), encoding="utf-8")
print("--shot 去重 OK")

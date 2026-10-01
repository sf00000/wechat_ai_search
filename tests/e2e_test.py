#!/usr/bin/env python3
"""端到端自动化测试（offscreen，不走人工交互）：

搜索真实话题 → 缓存 → 勾选前 2 条 → 下载 → 校验落盘文件存在。

运行：python e2e_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import MainWindow


def main() -> int:
    import time as _t
    query = sys.argv[1] if len(sys.argv) > 1 else "AI 编程"
    topic = f"{query}_e2e{_t.strftime('%H%M%S')}"  # 仅下载目录带后缀，搜索词保持干净
    app = QApplication(sys.argv)
    # offscreen 模式下模态弹窗无人点击会永久阻塞事件循环——覆盖搜索失败
    # （验证码提示）与下载完成两条路径
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.exec = staticmethod(lambda *a, **k: 0)
    win = MainWindow()
    from PySide6.QtCore import QTimer
    _wd = {"fired": False}

    def _watchdog():
        _wd["fired"] = True
        print("E2E 看门狗：6 分钟未完成，强制退出（可能卡在模态弹窗或网络）")
        app.quit()

    QTimer.singleShot(360_000, _watchdog)
    win.input.setText(query)
    win.chk_force.setChecked(True)  # 强制联网，验证真实链路

    win.do_search()
    # 等待完成标志：on_search_done / on_search_failed 都会恢复搜索按钮。
    # 注意不能只等 isRunning()==False——线程结束与排队信号投递之间存在竞态，
    # 需要持续泵事件直到 UI 状态真正变化（真实 GUI 的事件循环不存在此问题）。
    import time

    deadline = time.time() + 120
    while time.time() < deadline:
        app.processEvents()
        if win.btn_search.isEnabled():  # 搜索开始时被禁用，完成/失败时恢复
            break
        time.sleep(0.05)
    if win.tree.topLevelItemCount() == 0:
        print(f"E2E 失败：搜索无结果（可能是搜狗验证码冷却中）状态栏：{win.lbl_status.text()}")
        return 2
    print(f"① 搜索 OK：{win.tree.topLevelItemCount()} 条 | {win.lbl_status.text()}")

    # 勾选前 2 条
    for i in range(min(2, win.tree.topLevelItemCount())):
        win.tree.topLevelItem(i).setCheckState(0, Qt.CheckState.Checked)
    print(f"② 勾选完成：{win.lbl_selected.text()}")

    win.input.setText(topic)  # 下载目录用隔离话题名（不影响已完成的搜索）
    win.do_download()
    deadline = time.time() + 300
    while time.time() < deadline:
        app.processEvents()
        if win.btn_search.isEnabled():  # on_download_done 完成时恢复
            break
        time.sleep(0.05)
    status = win.lbl_status.text()
    print(f"③ 下载结束：{status}")

    # 校验落盘
    topic_dir = Path(win.cfg["base_dir"]) / topic
    mds = list(topic_dir.glob("*.md")) if topic_dir.is_dir() else []
    print(f"④ 落盘校验：{topic_dir} 下有 {len(mds)} 个 md")
    for md in mds[:3]:
        print("   -", md.name[:60])
    win.store.close()

    if mds:
        print("E2E 通过 ✅")
        return 0
    print("E2E 失败 ❌")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

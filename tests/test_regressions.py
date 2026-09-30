#!/usr/bin/env python3
"""回归测试（离线）：针对 v0.1.2 代码审查发现的三个缺陷。

1. 单篇抓取失败回调不得抛 UnboundLocalError（v0.1.1 的 P0）
2. Bing/DDG 兜底结果（真实 URL、无搜狗链接）直接下载，不走搜狗解析
3. 上一查询的 AI 精排结果不得污染当前查询（generation 防护）
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

os_ok = True
try:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
except Exception:  # pragma: no cover
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from search_channels import SearchResult
import downloader


class FakeSogou:
    """resolve 一被调用就断言失败的假会话（兜底结果不应触发解析）。"""

    def __init__(self):
        self.resolve_called = False

    def resolve(self, r):
        self.resolve_called = True
        raise AssertionError("兜底结果不应调用搜狗解析")


class FakeStore:
    def record_download(self, *a, **k):
        pass


def test_fail_callback_no_unboundlocal() -> int:
    """爬虫回调返回失败时，旧版因闭包 n_fail 缺 nonlocal 抛 UnboundLocalError。"""
    import app as app_mod

    r = SearchResult(url="https://mp.weixin.qq.com/s?__biz=x", resolved=True)
    w = app_mod.DownloadWorker(FakeSogou(), FakeStore(), [r], "t", "." , 0)

    def fake_download(urls, topic, base_dir, delay=0, progress_cb=None, **k):
        # 模拟爬虫：逐篇回调，其中一篇失败
        progress_cb(1, 2, {"url": urls[0], "action": "download", "success": False, "error": "content_empty"})
        progress_cb(2, 2, {"url": urls[0], "action": "download", "success": True, "error": ""})
        return {"topic_dir": ".", "ok": [], "failed": [{"url": urls[0], "title": "t", "error": "x"}],
                "written": 0, "skipped": 0}

    orig = app_mod.downloader.download_articles
    app_mod.downloader.download_articles = fake_download
    try:
        w.run()  # 旧版在此处抛 UnboundLocalError
    finally:
        app_mod.downloader.download_articles = orig
    print("1. 失败回调无 UnboundLocalError OK")
    return 0


def test_fallback_result_downloads_without_resolve() -> int:
    """Bing/DDG 结果（真实 URL、resolved=False、无搜狗链接）应直接下载。"""
    import app as app_mod

    real_mp = "https://mp.weixin.qq.com/s?__biz=MzIzODI1NjkyMQ==&mid=1&idx=1&sn=abc"
    r = SearchResult(url=real_mp, resolved=False, sogou_link="", title="兜底文章")
    assert not r.resolved  # 确认走的是"看似需要解析"的分支
    w = app_mod.DownloadWorker(FakeSogou(), FakeStore(), [r], "t", ".", 0)

    captured = {}

    def fake_download(urls, topic, base_dir, delay=0, progress_cb=None, **k):
        captured["urls"] = list(urls)
        progress_cb(1, 1, {"url": urls[0], "action": "download", "success": True, "error": ""})
        return {"topic_dir": ".", "ok": [{"url": urls[0], "title": "兜底文章", "author": "a",
                                          "md_path": "x.md"}],
                "failed": [], "written": 1, "skipped": 0}

    orig = app_mod.downloader.download_articles
    app_mod.downloader.download_articles = fake_download
    try:
        w.run()
    finally:
        app_mod.downloader.download_articles = orig

    assert captured["urls"] == [real_mp], captured
    assert not w.sogou.resolve_called, "兜底结果不应触发搜狗解析"
    print("2. Bing/DDG 兜底结果直接下载 OK")
    return 0


def test_stale_rerank_discarded() -> int:
    """上一查询的 AI 结果（旧 generation）不得应用到当前查询。"""
    import app as app_mod

    app = QApplication.instance() or QApplication(sys.argv)
    win = app_mod.MainWindow()
    win._search_gen = 2          # 当前是第 2 代查询
    win._results = [SearchResult(title="B 查询结果", sogou_link="k1")]
    win._ai_scores = {}

    stale = types.SimpleNamespace(gen=1)  # 第 1 代查询的精排 worker
    win.rerank_worker = stale
    assert not win._rerank_is_current(stale), "旧代数应被判定为过期"
    assert win._apply_rerank_result(stale, {"k1": 9}, "旧结果") is False  # 旧代数结果喂进去
    assert win._ai_scores == {}, "旧代数分数不得写入"

    # 当前代数才被接受
    fresh = types.SimpleNamespace(gen=2)
    win.rerank_worker = fresh
    assert win._rerank_is_current(fresh)
    assert win._apply_rerank_result(fresh, {"k1": 8}, "新结果") is True
    assert win._ai_scores == {"k1": 8}
    win.store.close()
    print("3. AI 跨查询 generation 防护 OK")
    return 0


def test_date_two_state_and_status_by_id() -> int:
    """日期表头两态（最新↔最早）；下载状态按稳定 ID 保留，重建列表不丢。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    import app as app_mod

    win = app_mod.MainWindow()
    url = "https://mp.weixin.qq.com/s?__biz=MzIzODI1NjkyMQ==&mid=1&idx=1&sn=abc"
    r = SearchResult(url=url, resolved=True, title="状态保留文章", account="号")
    win.input.setText("测试")
    win.on_search_done([r.to_dict()], "自测")

    # 日期两态：相关度 → desc → asc → desc（永远两态，不再三态循环）
    win.tree.header().sectionClicked.emit(3)
    assert win._sort_mode == "date_desc", win._sort_mode
    win.tree.header().sectionClicked.emit(3)
    assert win._sort_mode == "date_asc", win._sort_mode
    win.tree.header().sectionClicked.emit(3)
    assert win._sort_mode == "date_desc", "日期排序应为两态循环"

    # 下载状态按 ID 记录，表格重建后仍在
    win.on_item_status(url, "✓ 已抓取", False)
    win._render_results()
    item = win.tree.topLevelItem(0)
    assert item.text(5) == "✓ 已抓取", f"状态列丢失: {item.text(5)!r}"
    win.store.close()
    print("4. 日期两态 + 下载状态按 ID 保留 OK")
    return 0


def main() -> int:
    test_fail_callback_no_unboundlocal()
    test_fallback_result_downloads_without_resolve()
    test_stale_rerank_discarded()
    test_date_two_state_and_status_by_id()
    print("回归测试全部通过 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

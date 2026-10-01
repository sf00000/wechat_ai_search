#!/usr/bin/env python3
"""回归测试（离线）：针对 v0.1.2 代码审查发现的三个缺陷。

1. 单篇抓取失败回调不得抛 UnboundLocalError（v0.1.1 的 P0）
2. Bing/DDG 兜底结果（真实 URL、无搜狗链接）直接下载，不走搜狗解析
3. 上一查询的 AI 精排结果不得污染当前查询（generation 防护）
"""

from __future__ import annotations

import os
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

from PySide6.QtCore import Qt
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


def test_fuse_no_retry() -> int:
    """被风控熔断（rate_limited）的文章不得进入重试轮。"""
    import downloader

    class FakeScraper:
        """真实混合形态：第 1 篇 content_empty 触发熔断，其余 rate_limited_skip。"""

        def __init__(self):
            self.calls = []

        def scrape_wechat(self, urls, delay=0, images_dir="", account_dir="",
                          progress_callback=None):
            self.calls.append(list(urls))
            out = []
            for i, u in enumerate(urls):
                if i == 0:
                    out.append({"url": u, "success": False, "error": "content_empty",
                                "_batch_circuit_break": True})
                else:
                    out.append({"url": u, "success": False, "error": "rate_limited_skip"})
            return out

        def _sanitize_filename_part(self, s, max_len=60):
            return (s or "nodate").strip() or "nodate"

        def _is_wechat_host(self, url):
            return "mp.weixin.qq.com" in url

    fake = FakeScraper()
    downloader._scraper = fake
    try:
        s = downloader.download_articles(
            ["https://mp.weixin.qq.com/s?__biz=a", "https://mp.weixin.qq.com/s?__biz=b"],
            "熔断自测", os.path.join(os.environ["TEMP"], "wts_fuse"), retries=1)
    finally:
        downloader._scraper = None
    assert len(fake.calls) == 1, f"熔断批次不应触发重试，实际调用 {len(fake.calls)} 次"
    assert s.get("circuit_broken") is True, "熔断标记应暴露给上层"
    assert len(s["failed"]) == 2 and s["written"] == 0
    print("5. 熔断批次不重试 OK（真实混合形态：content_empty 触发 + rate_limited 跳过）")


def test_per_article_cancel() -> int:
    """窗口关闭（中断请求）后，当前篇回调即中止，不再请求后续文章。"""
    import app as app_mod

    r1 = SearchResult(url="https://mp.weixin.qq.com/s?__biz=a", resolved=True)
    r2 = SearchResult(url="https://mp.weixin.qq.com/s?__biz=b", resolved=True)

    class FakeSogou:
        def resolve(self, r):
            return r.url

    class FakeStore:
        def record_download(self, *a, **k):
            pass

    w = app_mod.DownloadWorker(FakeSogou(), FakeStore(), [r1, r2], "t", ".", 0)
    attempted = []

    def fake_download(urls, topic, base_dir, delay=0, retries=0, progress_cb=None, **k):
        for u in urls:  # 模拟爬虫逐篇抓取；cb 抛 DownloadAborted 即中断循环
            attempted.append(u)
            w.requestInterruption()  # 模拟第 1 篇抓取期间窗口关闭
            progress_cb(len(attempted), len(urls),
                        {"url": u, "action": "download", "success": False,
                         "error": "content_empty"})
        return {"topic_dir": ".", "ok": [], "failed": [], "written": 0, "skipped": 0}

    orig = app_mod.downloader.download_articles
    app_mod.downloader.download_articles = fake_download
    done = []
    w.finished_all.connect(lambda ok, fail, d: done.append((ok, fail, d)))
    import time

    app = QApplication.instance() or QApplication(sys.argv)
    try:
        w.start()  # 真实启动线程：requestInterruption 只对运行中的线程生效
        deadline = time.time() + 15
        while time.time() < deadline:
            app.processEvents()
            if w.isFinished():
                break
            time.sleep(0.02)
        for _ in range(5):  # 冲刷排队中的 finished_all 信号
            app.processEvents()
            time.sleep(0.02)
        assert attempted == [r1.url], f"应只尝试第 1 篇，实际 {len(attempted)} 篇: {attempted}"
        assert done and done[0][0] == 0 and done[0][1] == 0, done
        print(f"6. 逐篇取消 OK（仅尝试 {len(attempted)}/2 篇即中止）")
    finally:
        app_mod.downloader.download_articles = orig  # 恢复最初保存的函数
    return 0


def test_closing_blocks_new_tasks() -> int:
    """_closing 后：搜索/下载/预览都不再创建后台任务。"""
    import app as app_mod

    app = QApplication.instance() or QApplication(sys.argv)
    win = app_mod.MainWindow()
    r = SearchResult(url="https://mp.weixin.qq.com/s?__biz=x", resolved=True, title="t")
    win._closing = True
    win.input.setText("t")
    win.do_search()
    assert win.search_worker is None, "关闭后不应启动搜索"
    win._results = [r]
    win._render_results()
    win.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    win.do_download()
    assert win.download_worker is None, "关闭后不应启动下载"
    win.start_preview(r)
    assert win.preview_worker is None, "关闭后不应启动预览"
    win.store.close()
    print("7. 关闭期间不启动新任务 OK")
    return 0


def test_mixed_circuit_break_stops_batch() -> int:
    """真实混合批次：前几篇 content_empty 触发熔断、后续 rate_limited_skip。

    熔断标记（_batch_circuit_break）必须传递到 worker：整批停止自动重试，
    失败项不再重新解析（refresh 不得被调用）。
    """
    import app as app_mod

    r1 = SearchResult(url="https://mp.weixin.qq.com/s?__biz=a", resolved=True)
    r2 = SearchResult(url="https://mp.weixin.qq.com/s?__biz=b", resolved=True)

    class FakeSogou:
        def __init__(self):
            self.refresh_n = 0

        def resolve(self, r, refresh=False):
            if refresh:
                self.refresh_n += 1
            return r.url

    class FakeStore:
        def record_download(self, *a, **k):
            pass

    win = app_mod.MainWindow()
    fake_sogou = FakeSogou()
    win.sogou = fake_sogou  # 注入假会话以统计 refresh 调用
    win.input.setText("t")
    win.on_search_done([r1.to_dict(), r2.to_dict()], "自测")
    for i in range(2):
        win.tree.topLevelItem(i).setCheckState(0, Qt.CheckState.Checked)

    calls = {"n": 0}

    def fake_download(urls, topic, base_dir, delay=0, retries=0, progress_cb=None, **k):
        calls["n"] += 1
        # 真实混合形态：第 1 篇 content_empty（触发熔断），第 2 篇 rate_limited_skip
        return {
            "topic_dir": ".",
            "ok": [],
            "failed": [
                {"url": urls[0], "title": "t1", "error": "content_empty"},
                {"url": urls[1], "title": "t2", "error": "rate_limited_skip"},
            ],
            "written": 0,
            "skipped": 0,
            "circuit_broken": True,
        }

    from PySide6.QtWidgets import QMessageBox
    orig_box = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: None)  # offscreen 弹窗无人点击会卡死
    app = QApplication.instance() or QApplication(sys.argv)
    orig = app_mod.downloader.download_articles
    app_mod.downloader.download_articles = fake_download
    try:
        win.do_download()
        import time

        deadline = time.time() + 20
        while time.time() < deadline:
            app.processEvents()
            if win.btn_search.isEnabled():
                break
            time.sleep(0.05)
        # 确保 worker 已结束（关闭路径依赖线程收尾，测试同样要收干净）
        deadline2 = time.time() + 10
        while win.download_worker is not None and time.time() < deadline2:
            app.processEvents()
            time.sleep(0.05)
    finally:
        app_mod.downloader.download_articles = orig
        QMessageBox.information = orig_box  # 恢复全局弹窗函数，不污染后续测试

    assert calls["n"] == 1, f"熔断后应停止整批重试，实际调用 {calls['n']} 次"
    assert fake_sogou.refresh_n == 0, "熔断后不应重新解析任何文章"
    # 两篇都应标记为失败（✗）
    statuses = [win.tree.topLevelItem(i).text(5) for i in range(2)]
    assert all("✗" in s for s in statuses), statuses
    win.store.close()
    print(f"8. 混合熔断整批停止重试 OK（download 调用 {calls['n']} 次、refresh 0 次）")
    return 0


def main() -> int:
    test_fail_callback_no_unboundlocal()
    test_fallback_result_downloads_without_resolve()
    test_stale_rerank_discarded()
    test_date_two_state_and_status_by_id()
    test_fuse_no_retry()
    test_per_article_cancel()
    test_closing_blocks_new_tasks()
    test_mixed_circuit_break_stops_batch()
    print("回归测试全部通过 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

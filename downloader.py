#!/usr/bin/env python3
"""下载执行层：进程内复用爬虫核心（vendor/wechat_scraper_v2.py）。

爬虫核心来自 wechat-link-downloads skill，已内置于本项目 vendor/ 目录，
因此本项目自包含、可独立编译。如设置了环境变量 WECHAT_SKILL_DIR 指向
本机 skill 目录，则优先使用（便于跟随 skill 更新）。

落盘约定（与 wechat-link-downloads skill 保持一致的扁平结构，按话题分目录）：
    <base_dir>/<话题名>/<发布时间>｜<公众号>｜<标题>.md
    <base_dir>/<话题名>/images/<公众号>/...
    <base_dir>/<话题名>/.scraper_cache/...      # 增量去重缓存（同话题重下自动跳过）

与 skill CLI 版的差异（有意为之）：
- 不触发「合集自动订阅」——话题搜索偶然带出的合集不应混进每日同步清单；
- 每个话题一个独立缓存目录，互不干扰。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

import mdflatten

APP_DIR = Path(__file__).resolve().parent
DEFAULT_SKILL_DIR = Path(r"C:\Users\Administrator\.zcode\skills\wechat-link-downloads")

_scraper = None  # 懒加载的 wechat_scraper_v2 模块
_logger_inst = None


def _load_scraper():
    global _scraper
    if _scraper is not None:
        return _scraper
    env_dir = os.environ.get("WECHAT_SKILL_DIR")
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(APP_DIR / "vendor")  # 内置爬虫核心
    for d in candidates:
        if d.is_dir():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            import wechat_scraper_v2  # noqa: E402

            _scraper = wechat_scraper_v2
            return _scraper
    raise RuntimeError(
        "未找到爬虫核心 wechat_scraper_v2.py（vendor/ 目录缺失，"
        "或环境变量 WECHAT_SKILL_DIR 指向的目录不存在）"
    )


def _logger():
    global _logger_inst
    if _logger_inst is None:
        _logger_inst = mdflatten.make_logger(APP_DIR / "log")
    return _logger_inst


def sanitize_topic(name: str) -> str:
    return _load_scraper()._sanitize_filename_part(name, max_len=60)


def is_wechat_url(url: str) -> bool:
    return _load_scraper()._is_wechat_host(url)


def topic_dir(base_dir: str | os.PathLike, topic: str) -> Path:
    return Path(base_dir).expanduser().resolve() / sanitize_topic(topic)


def download_articles(
    urls: list[str],
    topic: str,
    base_dir: str,
    delay: float = 1.0,
    resolve_link: Optional[Callable[[str], str]] = None,
    progress_cb: Optional[Callable[[int, int, dict], None]] = None,
) -> dict:
    """下载一批公众号文章到 <base_dir>/<话题名>/，返回汇总。

    Args:
        urls: 文章链接（真实 mp 链接）
        topic: 话题名（作为子目录）
        base_dir: 话题下载根目录
        delay: 文章间请求间隔秒数（对微信保持礼貌）
        resolve_link: 可选，把临时链接解析为真实地址的回调
        progress_cb: (已完成数, 总数, 当篇结果 dict) —— 爬虫原生逐篇回调

    Returns:
        {"topic_dir": str, "ok": [...], "failed": [...], "written": n, "skipped": n}
    """
    scraper = _load_scraper()
    logger = _logger()

    root = topic_dir(base_dir, topic)
    cache_root = root / mdflatten.CACHE_DIR_NAME
    images_dir = root / "images"
    for p in (root, cache_root, images_dir):
        os.makedirs(p, exist_ok=True)

    # 预先把临时链接解析成真实地址；失败的单篇记为失败
    real_urls: list[str] = []
    failed: list[dict] = []
    for u in urls:
        if is_wechat_url(u):
            real_urls.append(u)
            continue
        if resolve_link is None:
            failed.append({"url": u, "title": "(临时链接)", "error": "未提供解析会话"})
            continue
        try:
            real = resolve_link(u)
            if is_wechat_url(real):
                real_urls.append(real)
            else:
                failed.append({"url": u, "title": "(临时链接)", "error": "解析结果不是微信链接"})
        except Exception as e:
            failed.append({"url": u, "title": "(临时链接)", "error": f"解析失败: {e}"})

    # 去重
    seen: set[str] = set()
    urls_final = [u for u in real_urls if not (u in seen or seen.add(u))]

    logger.info("话题下载开始 | topic=%s | 待下=%d | 解析失败=%d", topic, len(urls_final), len(failed))

    # 爬虫核心：单线程顺序抓取 + 逐篇回调（对微信礼貌，避免并发写冲突/限流）
    results = scraper.scrape_wechat(
        urls=urls_final,
        delay=delay,
        images_dir=str(images_dir),
        account_dir=str(cache_root),
        progress_callback=progress_cb,
    )

    # 缓存 → 扁平落盘（补缺 + 更新覆盖），与 skill CLI 行为一致
    mdflatten.flatten_cache(cache_root, root, logger)
    mdflatten.overwrite_updated_flat(root, results, logger)
    mdflatten.reconcile_download_log(cache_root, logger)
    mdflatten.sync_download_log(cache_root, root, logger)

    ok: list[dict] = []
    for r in results:
        if r.get("success"):
            md_cache = r.get("md_path") or ""
            flat = str(root / Path(md_cache).name) if md_cache else ""
            if flat and not os.path.exists(flat):  # 兜底：扁平文件缺失就承认缓存路径
                flat = md_cache
            ok.append(
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "author": r.get("author", ""),
                    "md_path": flat,
                    "action": r.get("action") or ("skip" if r.get("skip") else "download"),
                }
            )
        else:
            failed.append(
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "error": r.get("error") or "抓取失败",
                }
            )

    written = sum(1 for r in results if r.get("success") and not r.get("skip"))
    skipped = sum(1 for r in results if r.get("skip"))
    logger.info(
        "话题下载完成 | topic=%s | 新写入=%d 跳过(已存在)=%d 失败=%d | 目录=%s",
        topic, written, skipped, len(failed), root,
    )
    return {
        "topic_dir": str(root),
        "ok": ok,
        "failed": failed,
        "written": written,
        "skipped": skipped,
    }

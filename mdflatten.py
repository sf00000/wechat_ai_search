#!/usr/bin/env python3
"""Markdown 落盘辅助：缓存平铺、更新覆盖、增量日志对账。

这些函数原本位于 wechat-link-downloads skill 的 download_articles.py，
为使本项目自包含而内联（逻辑保持一致，仅去掉对 skill 其它模块的依赖）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

CACHE_DIR_NAME = ".scraper_cache"
_IMG_LINK_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)")


def make_logger(log_dir: Path, log_file_name: str = "topic_searcher.log") -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("topic_searcher")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_dir / log_file_name, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _rewrite_image_paths(text: str, src_md_dir: Path, dst_md_dir: Path) -> str:
    """把 markdown 中的相对图片路径，重写成"从 dst_md_dir 出发的相对路径"。"""

    def _repl(m: re.Match[str]) -> str:
        alt, rel, title = m.group(1), m.group(2), m.group(3) or ""
        if rel.startswith(("http://", "https://", "data:", "/")):
            return m.group(0)
        try:
            abs_target = (src_md_dir / rel).resolve()
            new_rel = os.path.relpath(abs_target, dst_md_dir)
        except (OSError, ValueError):
            return m.group(0)
        new_rel = new_rel.replace(os.sep, "/")
        return f"![{alt}]({new_rel}{title})"

    return _IMG_LINK_RE.sub(_repl, text)


def flatten_cache(cache_root: Path, final_root: Path, logger: logging.Logger) -> tuple[int, int]:
    """把 cache_root/<account>/*.md 平铺到 final_root/<同名>.md（已存在跳过）。

    Returns (written, skipped)
    """
    written = 0
    skipped = 0
    cache_root, final_root = Path(cache_root), Path(final_root)
    if not cache_root.is_dir():
        return 0, 0
    for account_dir in sorted(cache_root.iterdir()):
        if not account_dir.is_dir():
            continue
        for md_path in sorted(account_dir.glob("*.md")):
            target = final_root / md_path.name
            if target.exists():
                skipped += 1
                continue
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("读 %s 失败: %s", md_path, e)
                continue
            new_text = _rewrite_image_paths(text, md_path.parent, final_root)
            try:
                target.write_text(new_text, encoding="utf-8")
            except OSError as e:
                logger.warning("写 %s 失败: %s", target, e)
                continue
            written += 1
    return written, skipped


def _read_md_head_for_log(path: Path, max_lines: int = 30) -> dict:
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                s = line.strip()
                m = re.match(r"^#\s+(.+)$", s)
                if m and "title" not in out:
                    out["title"] = m.group(1).strip()
                    continue
                m = re.match(r"^-\s*链接[:：]\s*(\S+)", s)
                if m:
                    out["url"] = m.group(1).strip()
                    continue
                m = re.match(r"^-\s*作者[:：]\s*(.+)$", s)
                if m:
                    out["author"] = m.group(1).strip()
                    continue
                m = re.match(r"^-\s*发布时间[:：]\s*(.+)$", s)
                if m:
                    out["publish_time"] = m.group(1).strip()
                    continue
                m = re.match(r"^-\s*爬取时间[:：]\s*(\S+)", s)
                if m:
                    out["crawl_time"] = m.group(1).strip()
                    continue
    except OSError:
        pass
    return out


def _filename_to_article_base(filename: str) -> str:
    base = filename[:-3] if filename.lower().endswith(".md") else filename
    base = re.sub(r"_dup$", "", base)
    parts = re.split(r"[|｜]", base, maxsplit=2)
    if len(parts) >= 3:
        return f"{parts[0]}_{parts[2]}"
    if len(parts) == 2:
        return f"{parts[0]}_{parts[1]}"
    return base


def reconcile_download_log(cache_root: Path, logger: logging.Logger) -> tuple[int, int]:
    """让 .scraper_cache/_state/download_log.json 与 cache 下实际 md 保持一致（只补不删）。

    Returns (backfilled, total_now)
    """
    cache_root = Path(cache_root)
    log_path = cache_root / "_state" / "download_log.json"
    if not log_path.is_file():
        return 0, 0
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读 download_log.json 失败，跳过 reconcile: %s", e)
        return 0, 0

    articles = data.get("articles") or []
    logged_basenames = {os.path.basename(str(r.get("md_path") or "")) for r in articles}
    logged_basenames.discard("")

    missing: list[Path] = []
    for root, dirs, files in os.walk(cache_root):
        if "_state" in dirs:
            dirs.remove("_state")
        for fn in files:
            if not fn.endswith(".md") or fn.endswith("_dup.md"):
                continue
            if fn in logged_basenames:
                continue
            missing.append(Path(root) / fn)

    if not missing:
        return 0, len(articles)

    for p in missing:
        head = _read_md_head_for_log(p)
        try:
            with open(p, "rb") as fh:
                content_hash = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            content_hash = ""
        article_base = _filename_to_article_base(p.name)
        articles.append(
            {
                "article_key": article_base,
                "url": head.get("url", ""),
                "title": head.get("title", ""),
                "author": head.get("author", ""),
                "publish_time": head.get("publish_time", ""),
                "content_hash": content_hash,
                "file_prefix": article_base,
                "md_path": str(p),
                "crawl_time": head.get("crawl_time")
                or datetime.fromtimestamp(os.path.getmtime(p)).isoformat(),
                "status": "success",
                "backfilled": True,
            }
        )

    data["generated_at"] = datetime.now().isoformat()
    data["article_count"] = len(articles)
    data["articles"] = articles
    tmp = str(log_path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, log_path)
    except OSError as e:
        logger.warning("写 download_log.json 失败: %s", e)
        return 0, len(articles)
    return len(missing), len(articles)


def overwrite_updated_flat(final_root: Path, results: list, logger: logging.Logger) -> int:
    """把本轮 action=update 的文章新内容强制覆盖到扁平目录，并清理改名前的旧版。"""
    final_root = Path(final_root)
    overwritten = 0
    for r in results:
        if not r.get("success") or r.get("action") != "update":
            continue
        src = Path(r.get("md_path") or "")
        if not src.is_file():
            continue
        target = final_root / src.name
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("读 %s 失败: %s", src, e)
            continue
        new_text = _rewrite_image_paths(text, src.parent, final_root)
        tmp = f"{target}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, target)
        except OSError as e:
            logger.warning("覆盖扁平文件 %s 失败: %s", target, e)
            continue
        overwritten += 1
        parts = src.name.split("｜")
        if len(parts) >= 3:
            prefix = f"{parts[0]}｜{parts[1]}｜"
            for old in final_root.glob(f"{prefix}*.md"):
                if old.name != src.name:
                    try:
                        old.unlink()
                        logger.info("清理更新前的旧版扁平文件: %s", old.name)
                    except OSError as e:
                        logger.warning("清理旧版扁平文件 %s 失败: %s", old, e)
    return overwritten


def sync_download_log(cache_root: Path, final_root: Path, logger: logging.Logger) -> None:
    """把 cache 的 download_log.json 同步到 final_root/_state/ 供下游使用。"""
    src = Path(cache_root) / "_state" / "download_log.json"
    if not src.is_file():
        return
    dst_dir = Path(final_root) / "_state"
    dst = dst_dir / "download_log.json"
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        logger.warning("同步 download_log.json 失败: %s", e)

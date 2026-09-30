#!/usr/bin/env python3
"""本地缓存：搜索结果缓存（重复搜索毫秒级出结果）+ 下载历史（跨话题识别已下载文章）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    query      TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    results    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
    url       TEXT PRIMARY KEY,
    title     TEXT,
    author    TEXT,
    md_path   TEXT,
    topic     TEXT,
    done_at   REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 搜索缓存 -------------------------------------------------------

    def get_cached_search(self, query: str, ttl_seconds: float) -> list[dict] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, results FROM search_cache WHERE query=?", (query,)
            ).fetchone()
        if row is None:
            return None
        fetched_at, results = row
        if time.time() - fetched_at > ttl_seconds:
            return None
        try:
            return json.loads(results)
        except json.JSONDecodeError:
            return None

    def save_search(self, query: str, results: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO search_cache(query, fetched_at, results) VALUES(?,?,?) "
                "ON CONFLICT(query) DO UPDATE SET fetched_at=?, results=?",
                (query, time.time(), json.dumps(results, ensure_ascii=False),
                 time.time(), json.dumps(results, ensure_ascii=False)),
            )
            self._conn.commit()

    # -- 下载历史 -------------------------------------------------------

    def record_download(self, url: str, title: str, author: str, md_path: str, topic: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO downloads(url, title, author, md_path, topic, done_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET title=?, author=?, md_path=?, topic=?, done_at=?",
                (url, title, author, md_path, topic, time.time(),
                 title, author, md_path, topic, time.time()),
            )
            self._conn.commit()

    def get_download(self, url: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT title, author, md_path, topic, done_at FROM downloads WHERE url=?",
                (url,),
            ).fetchone()
        if row is None:
            return None
        return {"title": row[0], "author": row[1], "md_path": row[2], "topic": row[3], "done_at": row[4]}

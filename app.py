#!/usr/bin/env python3
"""微信话题搜索下载器 —— PySide6 桌面应用。

交互流：输入话题 → 搜索（搜狗微信，结果带勾选框）→ 勾选 → 下载到
<base_dir>/<话题名>/（Markdown + 图片，复用 wechat-link-downloads 爬虫核心）。

速度设计：
- 搜索结果落 SQLite 缓存（TTL 内重复搜索毫秒级出结果）；
- 勾选/全选是原生控件即时操作；快捷键：Ctrl+F 聚焦搜索、Ctrl+A 全选、
  空格勾选、回车下载选中、双击行预览原文、F5 强制刷新；
- 点「下载」立即入队返回，后台线程顺序抓取，逐篇实时回填状态列。

运行：python app.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import webbrowser
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from search_channels import SearchResult, SogouCaptchaError, SogouWeixin, search_all
import downloader
import rerank
import version
from cache_store import Store

APP_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    # onefile 打包后 __file__ 在临时解压目录：配置/数据放 exe 旁（便携式），
    # 只读资源（图标）在解压目录
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    RESOURCE_DIR = APP_DIR
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "cache.db"

DEFAULT_CONFIG = {
    "base_dir": str(Path.home() / "Documents" / "wechat-topic-search"),
    "search_pages": 2,       # 每页 10 条；页数越多对搜狗请求越多
    "download_delay": 1.0,   # 文章间间隔秒数
    "cache_ttl_minutes": 30, # 搜索缓存有效期
    "ai_rerank": True,       # AI 语义精排（标题+摘要发模型网关打分）
    "rerank_model": "",      # 留空则用环境变量 WECHAT_CLASSIFY_MODEL 或默认模型
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


class SearchWorker(QThread):
    """后台搜索：缓存优先，未命中走网络。"""

    done = Signal(list, str)      # (结果 dict 列表, 提示信息)
    failed = Signal(str)

    def __init__(self, sogou: SogouWeixin, store: Store, query: str,
                 pages: int, ttl_min: float, force: bool, parent=None):
        super().__init__(parent)
        self.sogou, self.store, self.query = sogou, store, query
        self.pages, self.ttl_min, self.force = pages, ttl_min, force

    def run(self):
        cache_key = f"{self.query}#p{self.pages}"  # 缓存键包含页数，配置变化不串缓存
        # 缓存命中 → 毫秒级返回
        if not self.force:
            cached = self.store.get_cached_search(cache_key, self.ttl_min * 60)
            if cached:
                self.done.emit(cached, f"缓存结果（{self.ttl_min:.0f} 分钟内，点「强制刷新」重新联网搜）")
                return
        try:
            results = search_all(self.query, limit=50, max_pages=self.pages, sogou=self.sogou)
        except SogouCaptchaError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:
            self.failed.emit(f"搜索失败：{e}")
            return
        self.store.save_search(cache_key, [r.to_dict() for r in results])
        channel = results[0].channel if results else ""
        self.done.emit([r.to_dict() for r in results], f"联网搜索完成（通道：{channel}，共 {len(results)} 条）")


class DownloadWorker(QThread):
    """后台下载：先解析搜狗临时链接，再复用 skill 爬虫逐篇抓取。"""

    item_status = Signal(str, str, bool)   # (url, 状态文本, 是否失败)
    batch_progress = Signal(int, int)      # (已完成, 总数)
    finished_all = Signal(int, int, str)   # (成功, 失败, 话题目录)

    def __init__(self, sogou: SogouWeixin, store: Store, items: list[SearchResult],
                 topic: str, base_dir: str, delay: float, parent=None):
        super().__init__(parent)
        self.sogou, self.store, self.items = sogou, store, items
        self.topic, self.base_dir, self.delay = topic, base_dir, delay

    def run(self):
        # 阶段 1：真实地址直用（Bing/DDG 兜底结果），搜狗临时链接才解析
        resolved: list[tuple[SearchResult, str]] = []
        n_fail = 0
        for r in self.items:
            if downloader.is_wechat_url(r.url):
                r.resolved = True
                resolved.append((r, r.url))
                continue
            self.item_status.emit(r.sogou_link or r.url, "解析链接…", False)
            try:
                real = self.sogou.resolve(r)
                resolved.append((r, real))
            except Exception as e:
                n_fail += 1
                self.item_status.emit(r.sogou_link or r.url, f"✗ {e}", True)

        if not resolved:
            self.finished_all.emit(0, n_fail, "")
            return

        # 阶段 2：进程内调用爬虫（顺序 + 逐篇回调）
        url2item = {real: r for r, real in resolved}
        real_urls = [real for _, real in resolved]

        def cb(idx: int, total: int, result: dict) -> None:
            nonlocal n_fail
            self.batch_progress.emit(idx, total)
            u = (result.get("url") or "").split("#", 1)[0]
            item = url2item.get(u) or url2item.get(real_urls[idx - 1] if 0 < idx <= len(real_urls) else "")
            if item is None:
                return
            # 回调字典只有 url/action/success/error
            if result.get("success"):
                if result.get("action") == "skip":
                    self.item_status.emit(item.sogou_link or item.url, "✓ 已存在（增量跳过）", False)
                else:
                    self.item_status.emit(item.sogou_link or item.url, "✓ 已抓取", False)
            else:
                n_fail += 1
                self.item_status.emit(
                    item.sogou_link or item.url, f"✗ {result.get('error') or '抓取失败'}", True
                )

        try:
            summary = downloader.download_articles(
                real_urls, self.topic, self.base_dir,
                delay=self.delay, progress_cb=cb,
            )
        except Exception as e:
            for _, real in resolved:
                self.item_status.emit(url2item[real].sogou_link or url2item[real].url, f"✗ {e}", True)
            self.finished_all.emit(0, len(resolved), "")
            return

        # 阶段 3：落盘结果回填 + 写下载历史
        n_ok = 0
        md_by_url = {}
        for ok_item in summary.get("ok", []):
            md_by_url[ok_item["url"]] = ok_item
        for r, real in resolved:
            key = real.split("#", 1)[0]
            info = md_by_url.get(key) or next(
                (o for o in summary.get("ok", []) if o["url"] == key), None
            )
            if info and info.get("md_path"):
                n_ok += 1
                self.store.record_download(
                    r.url or real, info.get("title") or r.title,
                    info.get("author") or r.account, info["md_path"], self.topic,
                )
                self.item_status.emit(r.sogou_link or r.url, f"✓ 已落盘", False)
        self.finished_all.emit(n_ok, n_fail, summary.get("topic_dir", ""))


class RerankWorker(QThread):
    """后台 AI 精排：标题+摘要发网关打相关性分。失败静默兜底为规则排序。"""

    done = Signal(dict, str)  # ({key: 分数}, 提示信息)

    def __init__(self, query: str, items: list, model: str = "",
                 base: str = "", token: str = "", parent=None):
        super().__init__(parent)
        self.query, self.items, self.model = query, items, model
        self.base, self.token = base, token

    def run(self):
        try:
            scores = rerank.ai_scores(
                self.query, self.items, model=self.model or None,
                base=self.base, token=self.token,
            )
        except Exception as e:
            self.done.emit({}, f"AI 精排失败：{e}")
            return
        if scores is None:
            self.done.emit({}, "AI 精排失败（网关未配置或超时），已按本地规则排序")
        else:
            self.done.emit(scores, "AI 精排完成，已按相关度重排")


class PreviewWorker(QThread):
    """后台抓取单篇文章用于预览（写临时目录，不影响下载缓存）。"""

    done = Signal(dict)  # {title, author, date, md, images:[(占位名, 路径)], tmp, url} 或 {error}

    def __init__(self, sogou: SogouWeixin, r: SearchResult, parent=None):
        super().__init__(parent)
        self.sogou, self.r = sogou, r

    def run(self):
        try:
            r = self.r
            if r.resolved and downloader.is_wechat_url(r.url):
                real = r.url
            else:
                self.sogou.resolve(r)
                real = r.url
            scraper = downloader._load_scraper()
            tmp = Path(tempfile.mkdtemp(prefix="wts_preview_"))
            try:
                results = scraper.scrape_wechat(
                    urls=[real], delay=0,
                    images_dir=str(tmp / "images"), account_dir=str(tmp / "md"),
                )
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            r0 = results[0] if results else {}
            if not r0.get("success"):
                shutil.rmtree(tmp, ignore_errors=True)  # 抓取失败即时清理临时目录
                self.done.emit({"error": r0.get("error") or "抓取失败"})
                return
            md_path = Path(r0.get("md_path") or "")
            if md_path.is_file():
                text = md_path.read_text(encoding="utf-8")
            else:
                text = r0.get("content") or ""
            # 图片相对路径 → 占位符（渲染时用 addResource 注入本地图片）
            images: list[tuple[str, str]] = []
            bases = (md_path.parent, tmp)

            def _repl(m: re.Match) -> str:
                alt, rel = m.group(1), m.group(2)
                for base in bases:
                    p = (base / rel).resolve()
                    if p.is_file():
                        ph = f"previmg{len(images)}.png"
                        images.append((ph, str(p)))
                        return f"![{alt}]({ph})"
                return m.group(0)

            text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)", _repl, text)
            self.done.emit(
                {
                    "title": r0.get("title") or r.title,
                    "author": r0.get("author") or r.account,
                    "date": r0.get("publish_time", ""),
                    "md": text,
                    "images": images,
                    "tmp": str(tmp),
                    "url": real,
                }
            )
        except SogouCaptchaError as e:
            self.done.emit({"error": str(e)})
        except Exception as e:
            self.done.emit({"error": f"预览失败：{e}"})


class PreviewDialog(QDialog):
    """应用内文章预览窗口（非模态，图片本地渲染）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文章预览")
        self.resize(780, 840)
        self.tmp_dir: str | None = None
        v = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        v.addWidget(self.browser)

    def show_article(self, info: dict):
        if "error" in info:
            self.browser.setPlainText(
                f"预览失败：{info['error']}\n\n可稍后重试，或右键结果用浏览器打开原文。"
            )
            self.setWindowTitle("文章预览")
        else:
            doc = self.browser.document()
            doc.clear()
            for ph, path in info.get("images", []):
                pm = QPixmap(path)
                if not pm.isNull():
                    doc.addResource(QTextDocument.ImageResource, QUrl(ph), pm)
            header = (
                f"# {info.get('title', '')}\n\n"
                f"**{info.get('author', '')}**　{info.get('date', '')}\n\n---\n\n"
            )
            doc.setMarkdown(header + info["md"])
            self.setWindowTitle(f"预览：{info.get('title', '')[:32]}")
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def cleanup_tmp(self):
        if self.tmp_dir and Path(self.tmp_dir).exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir = None

    def closeEvent(self, event):
        self.cleanup_tmp()
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.store = Store(DB_PATH)
        self.sogou = SogouWeixin(cookies=str(self.cfg.get("sogou_cookies", "")))
        self.search_worker: SearchWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.rerank_worker: RerankWorker | None = None
        self.preview_worker: PreviewWorker | None = None
        self.preview_dlg = PreviewDialog(self)
        self._results: list[SearchResult] = []   # 最近一次搜索的原始结果
        self._ai_scores: dict[str, int] = {}     # key -> AI 相关性分
        self._init_ui()
        self._init_shortcuts()

    # ------------------------------------------------------------------ UI

    def _init_ui(self):
        self.setWindowTitle(f"{version.APP_NAME} v{version.__version__}")
        icon_path = RESOURCE_DIR / "assets" / "app.ico"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1060, 720)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(8)

        # 搜索行
        bar = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入话题，如：AI 编程   （回车搜索，Ctrl+F 聚焦）")
        self.btn_search = QPushButton("搜 索")
        self.btn_search.setFixedWidth(90)
        self.chk_force = QCheckBox("强制刷新")
        self.chk_force.setToolTip("勾选后跳过本地缓存直接联网搜索（也可按 F5）")
        bar.addWidget(self.input, 1)
        bar.addWidget(self.chk_force)
        bar.addWidget(self.btn_search)
        layout.addLayout(bar)

        # 筛选行（改动即时生效，不重新搜索）
        flt = QHBoxLayout()
        flt.addWidget(QLabel("时间:"))
        self.combo_days = QComboBox()
        self.combo_days.addItems(["全部时间", "近一周", "近一月", "近一年"])
        flt.addWidget(self.combo_days)
        flt.addWidget(QLabel("每号最多:"))
        self.spin_peracct = QSpinBox()
        self.spin_peracct.setRange(0, 20)
        self.spin_peracct.setValue(3)
        self.spin_peracct.setToolTip("每个公众号最多保留几篇，0=不限（防营销号刷屏）")
        flt.addWidget(self.spin_peracct)
        flt.addWidget(QLabel("包含词:"))
        self.input_include = QLineEdit()
        self.input_include.setPlaceholderText("任一命中保留")
        self.input_include.setFixedWidth(110)
        flt.addWidget(self.input_include)
        flt.addWidget(QLabel("排除词:"))
        self.input_exclude = QLineEdit()
        self.input_exclude.setPlaceholderText("任一命中剔除")
        self.input_exclude.setFixedWidth(110)
        flt.addWidget(self.input_exclude)
        self.chk_ai = QCheckBox("AI 精排")
        self.chk_ai.setChecked(bool(self.cfg.get("ai_rerank", True)))
        if not rerank.gateway_available(
            str(self.cfg.get("api_base", "") or ""),
            str(self.cfg.get("api_token", "") or ""),
        ):
            self.chk_ai.setEnabled(False)
            self.chk_ai.setToolTip(
                "未配置模型网关：在 config.json 填 api_base / api_token"
                "（任意 Anthropic 协议网关），或设置环境变量"
                " ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN"
            )
        else:
            self.chk_ai.setToolTip("把标题+摘要发给模型网关打相关性分，完成后自动重排（正文不出本机）")
        flt.addWidget(self.chk_ai)
        flt.addStretch(1)
        layout.addLayout(flt)

        # 结果表
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["#", "标题", "公众号", "日期", "分", "状态", "摘要"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        header = self.tree.header()
        header.setSectionResizeMode(1, QHeaderView.Stretch)      # 标题拉伸
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.tree.setColumnWidth(0, 44)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 90)
        self.tree.setColumnWidth(4, 40)
        self.tree.setColumnWidth(5, 150)
        layout.addWidget(self.tree, 1)

        # 下载行
        bottom = QHBoxLayout()
        self.lbl_selected = QLabel("已选 0 篇")
        self.btn_download = QPushButton("下载选中 (回车)")
        self.btn_download.setEnabled(False)
        self.btn_open_dir = QPushButton("打开下载目录")
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedWidth(240)
        self.lbl_status = QLabel("就绪")
        bottom.addWidget(self.lbl_selected)
        bottom.addWidget(self.btn_download)
        bottom.addWidget(self.btn_open_dir)
        bottom.addWidget(self.progress)
        bottom.addWidget(self.lbl_status, 1)
        layout.addLayout(bottom)

        # 信号
        self.btn_search.clicked.connect(self.do_search)
        self.input.returnPressed.connect(self.do_search)
        self.btn_download.clicked.connect(self.do_download)
        self.btn_open_dir.clicked.connect(self.open_base_dir)
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.itemDoubleClicked.connect(self.on_preview)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        # 筛选条件变化即时重渲染（不重新搜索）
        self.combo_days.currentIndexChanged.connect(self._render_results)
        self.spin_peracct.valueChanged.connect(self._render_results)
        self.input_include.textChanged.connect(self._render_results)
        self.input_exclude.textChanged.connect(self._render_results)
        self.chk_ai.toggled.connect(self._on_ai_toggled)

        hint = QLabel("空格 勾选 · Ctrl+A 全选 · 回车 下载选中 · 双击 预览原文 · F5 强制刷新搜索")
        hint.setStyleSheet("color: #888;")
        layout.addWidget(hint)

    def _init_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.input.setFocus)
        QShortcut(QKeySequence("F5"), self, activated=lambda: (self.chk_force.setChecked(True), self.do_search()))
        # 作用域收窄到结果表，避免劫持搜索框的回车 / 输入框内的 Ctrl+A
        sc_dl = QShortcut(QKeySequence("Return"), self.tree, activated=self.do_download)
        sc_dl.setContext(Qt.WidgetWithChildrenShortcut)
        sc_all = QShortcut(QKeySequence("Ctrl+A"), self.tree, activated=self.check_all)
        sc_all.setContext(Qt.WidgetWithChildrenShortcut)

    # ------------------------------------------------------------------ 搜索

    def do_search(self):
        query = self.input.text().strip()
        if not query:
            self.lbl_status.setText("请输入话题关键词")
            return
        if self.download_worker is not None or self.preview_worker is not None:
            QMessageBox.information(self, "请稍候", "下载或预览进行中，完成后再搜索（共用网络会话）。")
            return
        if self.search_worker is not None:
            return  # 已有搜索在途（结果按 sender 校验，过期自动丢弃）
        self.btn_search.setEnabled(False)
        self.lbl_status.setText("搜索中…")
        self.search_worker = SearchWorker(
            self.sogou, self.store, query,
            pages=int(self.cfg.get("search_pages", 2)),
            ttl_min=float(self.cfg.get("cache_ttl_minutes", 30)),
            force=self.chk_force.isChecked(),
        )
        self.search_worker.done.connect(self.on_search_done)
        self.search_worker.failed.connect(self.on_search_failed)
        self.search_worker.start()

    def on_search_done(self, results: list, note: str):
        if self.sender() is not self.search_worker:
            return  # 过期结果（用户已发起新搜索），丢弃
        self.search_worker = None
        self.btn_search.setEnabled(True)
        self.chk_force.setChecked(False)
        self._results = [
            SearchResult(**{k: v for k, v in d.items() if k in SearchResult.__dataclass_fields__})
            for d in results
        ]
        self._ai_scores = {}
        self._render_results()
        # AI 精排：异步打分，完成后自动重排（不挡搜索/勾选/下载操作）
        if (
            self.chk_ai.isChecked()
            and self.chk_ai.isEnabled()
            and self._results
            and self.rerank_worker is None
        ):
            self.lbl_status.setText(note + "；AI 精排中…")
            self.rerank_worker = RerankWorker(
                self.input.text().strip(), list(self._results),
                model=str(self.cfg.get("rerank_model", "") or ""),
                base=str(self.cfg.get("api_base", "") or ""),
                token=str(self.cfg.get("api_token", "") or ""),
            )
            self.rerank_worker.done.connect(self._on_rerank_done)
            self.rerank_worker.start()
        else:
            self.lbl_status.setText(note)

    # ------------------------------------------------------------------ 渲染/筛选

    def _days_from_combo(self) -> int | None:
        return {0: None, 1: 7, 2: 30, 3: 365}.get(self.combo_days.currentIndex())

    def _render_results(self):
        """按当前筛选条件 + 相关度排序重建表格（保持勾选状态）。"""
        if not self._results:
            return
        # 记住勾选状态（按结果稳定 key）
        checked: set[str] = set()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == Qt.Checked:
                r: SearchResult = it.data(0, Qt.UserRole)
                checked.add(rerank.key(r))

        query = self.input.text().strip()
        items = rerank.apply_filters(
            self._results,
            days=self._days_from_combo(),
            per_account=self.spin_peracct.value(),
            include=self.input_include.text(),
            exclude=self.input_exclude.text(),
        )
        scored = [(r, rerank.local_score(query, r)) for r in items]

        def sort_key(pair):
            r, local = pair
            ai = self._ai_scores.get(rerank.key(r)) if self.chk_ai.isChecked() else None
            return (-ai if ai is not None else 1 << 30, -local)  # 有 AI 分的排前，按分数降序

        scored.sort(key=sort_key)

        self.tree.blockSignals(True)  # 重建期间不触发 itemChanged 刷计数
        self.tree.clear()
        for i, (r, _local) in enumerate(scored, 1):
            item = QTreeWidgetItem([str(i), r.title, r.account, r.date_str, "", "", r.snippet])
            item.setData(0, Qt.UserRole, r)
            item.setCheckState(0, Qt.Checked if rerank.key(r) in checked else Qt.Unchecked)
            item.setToolTip(1, r.title)
            prev = self.store.get_download(r.url) if r.url else None
            if prev:
                item.setText(5, "✓ 此前已下载")
                item.setForeground(5, Qt.gray)
            ai = self._ai_scores.get(rerank.key(r))
            if ai is not None:
                item.setText(4, str(ai))
                if ai >= 7:
                    item.setForeground(4, Qt.darkGreen)
                elif ai <= 3:
                    item.setForeground(4, Qt.red)
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)
        self._update_selected_count()

    def _on_ai_toggled(self, checked: bool):
        if not checked:
            self._render_results()  # 取消勾选：回退到规则排序（分数保留，取消勾选即忽略）
            return
        if self._results and not self._ai_scores and self.rerank_worker is None:
            self.lbl_status.setText("AI 精排中…")
            self.rerank_worker = RerankWorker(
                self.input.text().strip(), list(self._results),
                model=str(self.cfg.get("rerank_model", "") or ""),
                base=str(self.cfg.get("api_base", "") or ""),
                token=str(self.cfg.get("api_token", "") or ""),
            )
            self.rerank_worker.done.connect(self._on_rerank_done)
            self.rerank_worker.start()

    def _on_rerank_done(self, scores: dict, note: str):
        if self.sender() is not self.rerank_worker:
            return  # 过期结果（属于上一次搜索的精排），丢弃
        self.rerank_worker = None
        if scores:
            self._ai_scores.update(scores)
            self._render_results()
        self.lbl_status.setText(note)

    def on_search_failed(self, msg: str):
        if self.sender() is not self.search_worker:
            return  # 过期结果，丢弃
        self.search_worker = None
        self.btn_search.setEnabled(True)
        self.lbl_status.setText(msg)
        if "验证码" in msg:
            box = QMessageBox(self)
            box.setWindowTitle("需要过一次验证")
            box.setText(msg)
            open_btn = box.addButton("打开搜狗验证页", QMessageBox.AcceptRole)
            box.addButton("知道了", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is open_btn:
                webbrowser.open("https://weixin.sogou.com/")

    # ------------------------------------------------------------------ 勾选/下载

    def on_item_changed(self, item: QTreeWidgetItem, col: int):
        if col == 0:
            self._update_selected_count()

    def _update_selected_count(self):
        n = sum(1 for i in range(self.tree.topLevelItemCount())
                if i >= 0 and self.tree.topLevelItem(i).checkState(0) == Qt.Checked)
        self.lbl_selected.setText(f"已选 {n} 篇")
        self.btn_download.setEnabled(n > 0 and self.download_worker is None)

    def check_all(self):
        state = Qt.Checked if any(
            self.tree.topLevelItem(i).checkState(0) != Qt.Checked
            for i in range(self.tree.topLevelItemCount())
        ) else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, state)

    def on_preview(self, item: QTreeWidgetItem, col: int):
        """双击：应用内抓取正文并预览（窗口内渲染，含图片）。"""
        r: SearchResult = item.data(0, Qt.UserRole)
        if r is None:
            return
        if self.preview_worker is not None:
            return  # 已有预览在抓取
        if self.download_worker is not None or self.search_worker is not None:
            QMessageBox.information(self, "请稍候", "搜索或下载进行中，完成后再预览（共用网络会话）。")
            return
        self.lbl_status.setText("预览抓取中…")
        self.preview_worker = PreviewWorker(self.sogou, r)
        self.preview_worker.done.connect(self._on_preview_done)
        self.preview_worker.start()
        self.preview_dlg.browser.setPlainText("正在抓取文章正文…")
        if not self.preview_dlg.isVisible():
            self.preview_dlg.show()
        self.preview_dlg.raise_()

    def _on_preview_done(self, info: dict):
        self.preview_worker = None
        self.preview_dlg.cleanup_tmp()  # 清理上一篇预览的临时目录
        self.preview_dlg.tmp_dir = info.get("tmp")
        self.preview_dlg.show_article(info)
        self.lbl_status.setText("就绪")

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        r: SearchResult = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        act_preview = menu.addAction("预览（应用内打开）")
        act_open = menu.addAction("在浏览器打开原文")
        act_check = menu.addAction("勾选")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is act_preview:
            self.on_preview(item, 0)
        elif chosen is act_open:
            url = (r.sogou_link or r.url) if r else ""
            if url:
                QDesktopServices.openUrl(url)
        elif chosen is act_check:
            item.setCheckState(0, Qt.Checked)

    def do_download(self):
        items = [
            self.tree.topLevelItem(i).data(0, Qt.UserRole)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).checkState(0) == Qt.Checked
        ]
        if not items:
            return
        topic = self.input.text().strip()
        self.btn_download.setEnabled(False)
        self.btn_search.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.progress.setMaximum(len(items))
        self.lbl_status.setText(f"下载中：0/{len(items)} …")
        for r in items:
            self.on_item_status(r.sogou_link or r.url, "排队中…", False)
        self.download_worker = DownloadWorker(
            self.sogou, self.store, items, topic,
            self.cfg["base_dir"], float(self.cfg.get("download_delay", 1.0)),
        )
        self.download_worker.item_status.connect(self.on_item_status)
        self.download_worker.batch_progress.connect(self.on_batch_progress)
        self.download_worker.finished_all.connect(self.on_download_done)
        self.download_worker.start()

    def on_item_status(self, url: str, text: str, error: bool):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            r: SearchResult = item.data(0, Qt.UserRole)
            if r and (r.sogou_link == url or r.url == url):
                item.setText(5, text)
                item.setForeground(5, Qt.red if error else Qt.darkGreen)
                return

    def on_batch_progress(self, done: int, total: int):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.lbl_status.setText(f"下载中：{done}/{total} …")

    def on_download_done(self, ok: int, fail: int, topic_dir: str):
        self.progress.setVisible(False)
        self.download_worker = None
        self.btn_search.setEnabled(True)
        self._update_selected_count()
        base = self.cfg["base_dir"]
        self.lbl_status.setText(
            f"完成：成功 {ok} 篇，失败 {fail} 篇 → {topic_dir or base}"
        )
        if topic_dir and ok > 0:
            QMessageBox.information(
                self, "下载完成",
                f"成功 {ok} 篇，失败 {fail} 篇。\n已保存到：\n{topic_dir}",
            )

    def open_base_dir(self):
        base = self.cfg["base_dir"]
        os.makedirs(base, exist_ok=True)
        os.startfile(base)

    def closeEvent(self, event):
        # 通知后台线程收尾并短暂等待；等待不到就不关 SQLite（避免线程写已关库崩溃，
        # 进程退出时由系统回收）
        workers = [w for w in (
            self.search_worker, self.download_worker,
            self.rerank_worker, self.preview_worker,
        ) if w is not None]
        for w in workers:
            w.requestInterruption()
        for w in workers:
            if w.isRunning():
                w.wait(2000)
        self.preview_dlg.cleanup_tmp()
        if not any(w is not None and w.isRunning() for w in workers):
            self.store.close()
        super().closeEvent(event)


def main():
    # 打包自检：验证爬虫核心在包体内可加载（不启动 GUI、不联网）
    if "--selftest" in sys.argv:
        try:
            scraper = downloader._load_scraper()
            assert hasattr(scraper, "scrape_wechat")
            print("SELFTEST OK:", scraper.__name__)
            return 0
        except Exception as e:
            print("SELFTEST FAIL:", e)
            return 1

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    win = MainWindow()
    win.show()
    # 命令行带话题词则自动搜索：python app.py "AI 编程"
    topic = " ".join(a for a in sys.argv[1:] if not a.startswith("-")).strip()
    if topic:
        win.input.setText(topic)
        win.do_search()
    sys.exit(app.exec())


if __name__ == "__main__":
    sys.exit(main() or 0)

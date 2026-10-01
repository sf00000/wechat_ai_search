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

from PySide6.QtCore import QThread, Signal, Qt, QEvent, QPointF, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QSpinBox, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from search_channels import SearchResult, SogouCaptchaError, SogouWeixin, search_all
import downloader
import rerank
import version
from cache_store import Store
from summary_popup import SummaryCard

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
    "download_retries": 1,   # 瞬时失败自动重试轮数（微信模板摇摆）
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
    # 基本校验：空值回退默认、数值钳位，防止配置文件写坏后功能异常
    if not str(cfg.get("base_dir") or "").strip():
        cfg["base_dir"] = DEFAULT_CONFIG["base_dir"]
    try:
        cfg["search_pages"] = min(10, max(1, int(cfg.get("search_pages", 2))))
    except (TypeError, ValueError):
        cfg["search_pages"] = 2
    try:
        cfg["download_delay"] = min(30.0, max(0.0, float(cfg.get("download_delay", 1.0))))
    except (TypeError, ValueError):
        cfg["download_delay"] = 1.0
    try:
        cfg["cache_ttl_minutes"] = min(24 * 60, max(1.0, float(cfg.get("cache_ttl_minutes", 30))))
    except (TypeError, ValueError):
        cfg["cache_ttl_minutes"] = 30
    cfg["ai_rerank"] = bool(cfg.get("ai_rerank", True))
    return cfg


class DownloadAborted(Exception):
    """窗口关闭触发的下载中止（经 progress_callback 抛出以中断爬虫批次）。"""


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
        if self.isInterruptionRequested():
            return
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
    """后台下载：解析临时链接后逐篇抓取；失败轮换全新签名地址重试。"""

    item_status = Signal(str, str, bool)   # (url, 状态文本, 是否失败)
    batch_progress = Signal(int, int)      # (已完成, 总数)
    finished_all = Signal(int, int, str)   # (成功, 失败, 话题目录)

    def __init__(self, sogou: SogouWeixin, store: Store, items: list[SearchResult],
                 topic: str, base_dir: str, delay: float, retries: int = 1, parent=None):
        super().__init__(parent)
        self.sogou, self.store, self.items = sogou, store, items
        self.topic, self.base_dir, self.delay = topic, base_dir, delay
        self.retries = retries

    def run(self):
        total = len(self.items)
        rounds = 1 + max(0, int(self.retries))
        # remaining: [(SearchResult, 真实地址)] —— 每轮失败的项进入下一轮并重新解析
        remaining: list[tuple[SearchResult, str]] = []
        n_fail = 0

        # ---- 第 0 轮准备：解析搜狗临时链接（真实地址直用） ----
        for r in self.items:
            if self.isInterruptionRequested():
                self.finished_all.emit(0, n_fail, "")
                return
            if downloader.is_wechat_url(r.url):
                remaining.append((r, r.url))
                continue
            self.item_status.emit(r.sogou_link or r.url, "解析链接…", False)
            try:
                real = self.sogou.resolve(r)
                remaining.append((r, real))
            except Exception as e:
                n_fail += 1
                self.item_status.emit(r.sogou_link or r.url, f"\u2717 {e}", True)

        if not remaining:
            self.finished_all.emit(0, n_fail, "")
            return

        # ---- 抓取轮：失败项下一轮换全新签名地址再试 ----
        n_ok = 0
        topic_dir = ""
        for attempt in range(rounds):
            if self.isInterruptionRequested():
                break
            url2item = {real: r for r, real in remaining}
            real_urls = [real for _, real in remaining]

            def cb(idx: int, round_total: int, result: dict) -> None:
                self.batch_progress.emit(n_ok + idx, n_ok + len(remaining))
                u = (result.get("url") or "").split("#", 1)[0]
                item = url2item.get(u)
                if item is None:
                    return
                # 回调字典只有 url/action/success/error
                if result.get("success"):
                    if result.get("action") == "skip":
                        self.item_status.emit(item.sogou_link or item.url, "\u2713 已存在（增量跳过）", False)
                    else:
                        self.item_status.emit(item.sogou_link or item.url, "\u2713 已抓取", False)
                else:
                    self.item_status.emit(
                        item.sogou_link or item.url,
                        f"\u2717 {result.get('error') or '抓取失败'}",
                        True,
                    )

            try:
                summary = downloader.download_articles(
                    real_urls, self.topic, self.base_dir,
                    delay=self.delay, retries=0, progress_cb=cb,
                )
            except DownloadAborted:
                self.finished_all.emit(0, 0, "")
                return
            except Exception as e:
                for _, real in remaining:
                    self.item_status.emit(
                        url2item[real].sogou_link or url2item[real].url, f"\u2717 {e}", True
                    )
                self.finished_all.emit(0, len(remaining), "")
                return

            topic_dir = summary.get("topic_dir", "")
            ok_by_url = {o["url"]: o for o in summary.get("ok", [])}

            still_failed: list[tuple[SearchResult, str, str]] = []
            for r, real in remaining:
                key = real.split("#", 1)[0]
                info = ok_by_url.get(key)
                if info and info.get("md_path"):
                    n_ok += 1
                    self.store.record_download(
                        r.url or real, info.get("title") or r.title,
                        info.get("author") or r.account, info["md_path"], self.topic,
                    )
                    self.item_status.emit(r.sogou_link or r.url, "\u2713 已落盘", False)
                else:
                    err = next(
                        (f.get("error") or "抓取失败" for f in summary.get("failed", [])
                         if f.get("url") == key),
                        "抓取失败",
                    )
                    still_failed.append((r, real, err))

            if not still_failed or attempt == rounds - 1:
                n_fail = len(still_failed)
                for r, real, err in still_failed:
                    self.item_status.emit(r.sogou_link or r.url, f"\u2717 {err}", True)
                break

            # 重新解析失败项：签名地址可能已失效，换全新签名再试。
            # 被风控熔断（rate_limited）的文章尊重熔断结果，不再重新请求。
            for r, real, err in still_failed:
                if self.isInterruptionRequested():
                    break
                if err and "rate_limited" in err:
                    self.item_status.emit(r.sogou_link or r.url, "\u2717 风控熔断跳过（稍后再试）", True)
                    continue
                self.item_status.emit(r.sogou_link or r.url, "重试中（换新链接）…", False)
                try:
                    self.sogou.resolve(r, refresh=True)
                except Exception as e:
                    self.item_status.emit(r.sogou_link or r.url, f"\u2717 {e}", True)
            remaining = [
                (r, r.url) for r, real, err in still_failed
                if downloader.is_wechat_url(r.url)
            ]
            if not remaining:
                break

        self.finished_all.emit(n_ok, total - n_ok, topic_dir)


class RerankWorker(QThread):
    """后台 AI 精排：标题+摘要发网关打相关性分。失败静默兜底为规则排序。

    gen：发起时的搜索代数；结果只在该代数仍是当前查询时才被应用。
    """

    done = Signal(dict, str)  # ({key: 分数}, 提示信息)

    def __init__(self, query: str, items: list, model: str = "",
                 base: str = "", token: str = "", gen: int = 0, parent=None):
        super().__init__(parent)
        self.query, self.items, self.model = query, items, model
        self.base, self.token = base, token
        self.gen = gen

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            scores = rerank.ai_scores(
                self.query, self.items, model=self.model or None,
                base=self.base, token=self.token,
            )
        except Exception as e:
            self.done.emit({}, f"AI 精排失败：{e}")
            return
        if self.isInterruptionRequested():
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
        if self.isInterruptionRequested():
            return
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
            if self.isInterruptionRequested():
                shutil.rmtree(tmp, ignore_errors=True)
                return
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
        self.summary_card = SummaryCard()
        self.summary_card.viewArticleRequested.connect(self.start_preview)
        self.summary_card.openInBrowserRequested.connect(
            lambda r: QDesktopServices.openUrl(QUrl(r.sogou_link or r.url))
        )
        self._results: list[SearchResult] = []   # 最近一次搜索的原始结果
        self._results_by_key: dict[str, SearchResult] = {}  # 稳定 ID → 结果（勾选状态源）
        self._checked_keys: set[str] = set()     # 已勾选的稳定 ID（跨筛选/排序保持）
        self._dl_status: dict[str, tuple[str, bool]] = {}  # 稳定 ID → 下载状态（重建不丢）
        self._ai_scores: dict[str, int] = {}     # key -> AI 相关性分
        self._search_gen = 0                     # 搜索代数：丢弃过期查询的 AI 精排结果
        self._sort_mode = "relevance"            # relevance / date_desc / date_asc
        self._closing = False
        self._hover_key: str | None = None
        self._active_workers: set = set()        # 全部在途后台任务（原生 finished 时移除）
        self._init_ui()
        self._init_shortcuts()

    def _track_worker(self, w: QThread, attr: str):
        """线程原生 finished：清引用 + 关闭流程中触发收尾检查。"""

        self._active_workers.add(w)

        def _finished():
            self._active_workers.discard(w)
            if getattr(self, attr) is w:
                setattr(self, attr, None)
            if self._closing:
                self._try_finish_close()

        w.finished.connect(_finished)

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
        self.btn_always_on_top = QPushButton("窗口置顶")
        self.btn_always_on_top.setCheckable(True)
        self.btn_always_on_top.setToolTip("让主窗口保持在其他普通窗口前面；再次点击取消")
        self.btn_always_on_top.toggled.connect(self._set_always_on_top)
        bar.addWidget(self.btn_always_on_top)
        layout.addLayout(bar)

        # 选择行：全选（三态显示）/ 清空选择，只作用于当前展示的结果
        sel = QHBoxLayout()
        self.chk_select_all = QCheckBox("全选当前结果")
        self.chk_select_all.setTristate(True)  # 三态仅用于显示部分选中
        self.chk_select_all.clicked.connect(self._on_select_all_clicked)
        sel.addWidget(self.chk_select_all)
        self.btn_clear_sel = QPushButton("清空选择")
        self.btn_clear_sel.clicked.connect(self._on_clear_selection)
        sel.addWidget(self.btn_clear_sel)
        sel.addWidget(QLabel("时间:"))
        self.radio_days: dict[int, QRadioButton] = {}
        for value, label in ((0, "全部时间"), (7, "近一周"), (30, "近一月"), (365, "近一年")):
            rb = QRadioButton(label)
            rb.setChecked(value == 0)
            rb.toggled.connect(self._render_results)
            self.radio_days[value] = rb
            sel.addWidget(rb)
        sel.addWidget(QLabel("每号最多:"))
        self.spin_peracct = QSpinBox()
        self.spin_peracct.setRange(0, 20)
        self.spin_peracct.setValue(3)
        self.spin_peracct.setToolTip("每个公众号最多保留几篇，0=不限（防营销号刷屏）")
        sel.addWidget(self.spin_peracct)
        sel.addWidget(QLabel("包含词:"))
        self.input_include = QLineEdit()
        self.input_include.setPlaceholderText("任一命中保留")
        self.input_include.setFixedWidth(110)
        sel.addWidget(self.input_include)
        sel.addWidget(QLabel("排除词:"))
        self.input_exclude = QLineEdit()
        self.input_exclude.setPlaceholderText("任一命中剔除")
        self.input_exclude.setFixedWidth(110)
        sel.addWidget(self.input_exclude)
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
        sel.addWidget(self.chk_ai)
        sel.addStretch(1)
        layout.addLayout(sel)

        # 结果表：勾选 / 标题 / 公众号 / 日期 / 相关度 / 下载状态（摘要进悬浮卡）
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["勾选", "标题", "公众号", "日期", "相关度", "下载状态"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.tree.setStyleSheet("QTreeWidget::item { height: 44px; }")
        self.tree.setMouseTracking(True)
        self.tree.viewport().setMouseTracking(True)
        self.tree.viewport().setAttribute(Qt.WA_Hover, True)
        self.tree.viewport().installEventFilter(self)   # 鼠标/悬停事件发给 viewport
        self.tree.installEventFilter(self)              # 键盘事件发给有焦点的 tree 本体
        header = self.tree.header()
        header.setSectionResizeMode(1, QHeaderView.Stretch)      # 标题独占剩余宽度
        self.tree.setColumnWidth(0, 44)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 90)
        self.tree.setColumnWidth(4, 64)
        self.tree.setColumnWidth(5, 150)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(4, Qt.DescendingOrder)  # 默认相关度排序
        header.sectionClicked.connect(self._on_header_clicked)
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
        # 筛选条件变化即时重渲染（不重新搜索）；时间单选在创建处已接 toggled
        self.spin_peracct.valueChanged.connect(self._render_results)
        self.input_include.textChanged.connect(self._render_results)
        self.input_exclude.textChanged.connect(self._render_results)
        self.chk_ai.toggled.connect(self._on_ai_toggled)

        hint = QLabel("空格 勾选 · Ctrl+A 全选当前 · 回车 下载选中 · 双击/悬浮卡 查看正文 · F5 强制刷新搜索")
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
        if self._closing:
            return
        if self.download_worker is not None or self.preview_worker is not None:
            QMessageBox.information(self, "请稍候", "下载或预览进行中，完成后再搜索（共用网络会话）。")
            return
        if self.search_worker is not None:
            return  # 已有搜索在途（结果按 sender 校验，过期自动丢弃）
        self.btn_search.setEnabled(False)
        self.lbl_status.setText("搜索中…")
        self._search_gen += 1  # 新查询使在途的旧 AI 精排结果过期
        self.search_worker = SearchWorker(
            self.sogou, self.store, query,
            pages=int(self.cfg.get("search_pages", 2)),
            ttl_min=float(self.cfg.get("cache_ttl_minutes", 30)),
            force=self.chk_force.isChecked(),
        )
        self.search_worker.done.connect(self.on_search_done)
        self.search_worker.failed.connect(self.on_search_failed)
        self._track_worker(self.search_worker, "search_worker")
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
        self._results_by_key = {rerank.key(r): r for r in self._results}
        self._checked_keys = set()  # 新查询：勾选从零开始
        self._dl_status = {}
        self._ai_scores = {}
        self._render_results()
        # AI 精排：异步打分，完成后自动重排（不挡搜索/勾选/下载操作）。
        # 允许与上一代精排并行：旧结果按 gen 过期丢弃，不会污染新查询。
        if (
            self.chk_ai.isChecked()
            and self.chk_ai.isEnabled()
            and self._results
        ):
            self.lbl_status.setText(note + "；AI 精排中…")
            self.rerank_worker = RerankWorker(
                self.input.text().strip(), list(self._results),
                model=str(self.cfg.get("rerank_model", "") or ""),
                base=str(self.cfg.get("api_base", "") or ""),
                token=str(self.cfg.get("api_token", "") or ""),
                gen=self._search_gen,
            )
            self.rerank_worker.done.connect(self._on_rerank_done)
            self._track_worker(self.rerank_worker, "rerank_worker")
            self.rerank_worker.start()
        else:
            self.lbl_status.setText(note)

    # ------------------------------------------------------------------ 渲染/筛选

    def _days_from_radio(self) -> int | None:
        for value, rb in self.radio_days.items():
            if rb.isChecked():
                return value or None
        return None

    def _sort_scored(self, scored: list) -> None:
        """按当前排序模式就地排序：relevance / date_desc / date_asc。

        日期排序用标准化时间戳（publish_ts），无日期的结果两个方向都放最后。
        """
        if self._sort_mode in ("date_desc", "date_asc"):
            def key(pair):
                r, local = pair
                if not r.publish_ts:
                    return (1, 0, -local)
                return (0, -r.publish_ts if self._sort_mode == "date_desc" else r.publish_ts, -local)
            scored.sort(key=key)
        else:
            def key(pair):
                r, local = pair
                ai = self._ai_scores.get(rerank.key(r)) if self.chk_ai.isChecked() else None
                return (-ai if ai is not None else 1 << 30, -local)
            scored.sort(key=key)

    def _score_cell(self, r: SearchResult) -> tuple[str, bool, bool]:
        """相关度列 (文本, 绿色, 红色)。未评分显示待评分，不视为 0 分。"""
        ai = self._ai_scores.get(rerank.key(r))
        if ai is not None:
            return str(ai), ai >= 7, ai <= 3
        if self.chk_ai.isChecked() and self.chk_ai.isEnabled():
            return "待评分", False, False
        return "\u2014", False, False

    def _render_results(self):
        """按当前筛选 + 排序重建表格。保留：勾选（稳定 ID 为源）、当前行、滚动位置。"""
        if not self._results:
            return
        sb = self.tree.verticalScrollBar()
        scroll_pos = sb.value()
        cur = self.tree.currentItem()
        cur_key = rerank.key(cur.data(0, Qt.UserRole)) if cur is not None else None

        query = self.input.text().strip()
        items = rerank.apply_filters(
            self._results,
            days=self._days_from_radio(),
            per_account=self.spin_peracct.value(),
            include=self.input_include.text(),
            exclude=self.input_exclude.text(),
        )
        scored = [(r, rerank.local_score(query, r)) for r in items]
        self._sort_scored(scored)

        self.tree.blockSignals(True)  # 重建期间不触发 itemChanged 刷计数
        self.tree.clear()
        for r, _local in scored:
            item = QTreeWidgetItem(["", r.title, r.account, r.date_str, "", ""])
            item.setData(0, Qt.UserRole, r)
            item.setCheckState(0, Qt.Checked if rerank.key(r) in self._checked_keys else Qt.Unchecked)
            item.setToolTip(1, r.title)
            score_text, green, red = self._score_cell(r)
            item.setText(4, score_text)
            if green:
                item.setForeground(4, Qt.darkGreen)
            elif red:
                item.setForeground(4, Qt.red)
            prev = self.store.get_download(r.url) if r.url else None
            st = self._dl_status.get(rerank.key(r))
            if st is not None:
                item.setText(5, st[0])
                item.setForeground(5, Qt.red if st[1] else Qt.darkGreen)
            elif prev:
                item.setText(5, "\u2713 此前已下载")
                item.setForeground(5, Qt.gray)
            self.tree.addTopLevelItem(item)
        # 恢复当前行与滚动位置
        if cur_key:
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                if rerank.key(it.data(0, Qt.UserRole)) == cur_key:
                    self.tree.setCurrentItem(it)
                    break
        sb.setValue(scroll_pos)
        self.tree.blockSignals(False)
        self._sync_selection_ui()

    def _sync_selection_ui(self):
        """同步全选框三态、已选计数（含隐藏）、下载按钮数量。"""
        displayed = {
            rerank.key(self.tree.topLevelItem(i).data(0, Qt.UserRole))
            for i in range(self.tree.topLevelItemCount())
        }
        n_checked = len(self._checked_keys)
        n_display_checked = len(self._checked_keys & displayed)
        hidden = n_checked - n_display_checked

        self.tree.blockSignals(True)
        if displayed and n_display_checked == len(displayed):
            self.chk_select_all.setCheckState(Qt.Checked)
        elif n_display_checked > 0:
            self.chk_select_all.setCheckState(Qt.PartiallyChecked)
        else:
            self.chk_select_all.setCheckState(Qt.Unchecked)
        self.tree.blockSignals(False)

        text = f"已选 {n_checked} 篇"
        if hidden > 0:
            text += f"，其中隐藏 {hidden} 篇"
        self.lbl_selected.setText(text)
        self.btn_download.setText(f"下载选中 ({n_checked})")
        self.btn_download.setEnabled(n_checked > 0 and self.download_worker is None)
        self.btn_clear_sel.setEnabled(n_checked > 0)

    def _on_select_all_clicked(self, checked: bool):
        """全选框：未选/部分 → 全选当前展示；已全选 → 取消当前展示。隐藏行勾选保留。"""
        displayed = [
            self.tree.topLevelItem(i).data(0, Qt.UserRole)
            for i in range(self.tree.topLevelItemCount())
        ]
        if self.chk_select_all.checkState() == Qt.Unchecked:
            for r in displayed:
                self._checked_keys.discard(rerank.key(r))
        else:
            for r in displayed:
                self._checked_keys.add(rerank.key(r))
        self._render_results()

    def _on_clear_selection(self):
        self._checked_keys.clear()
        self._render_results()

    def _on_header_clicked(self, col: int):
        """表头点击：日期列循环 最新→最早→相关度；相关度列直接回到相关度排序。"""
        header = self.tree.header()
        if col == 3:
            # 两态循环 最新↔最早；恢复相关度请点「相关度」表头
            if self._sort_mode == "date_desc":
                self._sort_mode = "date_asc"
                header.setSortIndicator(3, Qt.AscendingOrder)
            else:
                self._sort_mode = "date_desc"
                header.setSortIndicator(3, Qt.DescendingOrder)
        elif col == 4:
            self._sort_mode = "relevance"
            header.setSortIndicator(4, Qt.DescendingOrder)
        self._render_results()

    def _on_ai_toggled(self, checked: bool):
        if not checked:
            self._render_results()  # 取消勾选：回退到规则排序（分数保留，取消勾选即忽略）
            return
        if self._results and not self._ai_scores:
            self.lbl_status.setText("AI 精排中…")
            self.rerank_worker = RerankWorker(
                self.input.text().strip(), list(self._results),
                model=str(self.cfg.get("rerank_model", "") or ""),
                base=str(self.cfg.get("api_base", "") or ""),
                token=str(self.cfg.get("api_token", "") or ""),
                gen=self._search_gen,
            )
            self.rerank_worker.done.connect(self._on_rerank_done)
            self._track_worker(self.rerank_worker, "rerank_worker")
            self.rerank_worker.start()

    def _rerank_is_current(self, w) -> bool:
        """精排结果只属于"当前 worker 且当前搜索代数"，否则丢弃（防跨查询污染）。"""
        return (
            w is not None
            and w is self.rerank_worker
            and getattr(w, "gen", None) == self._search_gen
            and not self._closing
        )

    def _apply_rerank_result(self, w, scores: dict, note: str) -> bool:
        """应用精排结果（仅当前 worker 且当前代数）；返回是否被接受。

        分数到达时只更新相关度列的行内单元格，不重建表格（不打断阅读/勾选）；
        排序等用户点击「相关度」表头或改动筛选时再生效。
        """
        if not self._rerank_is_current(w):
            return False  # 过期结果（旧 worker 或旧查询代数），丢弃
        self.rerank_worker = None
        if scores:
            self._ai_scores.update(scores)
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                r: SearchResult = item.data(0, Qt.UserRole)
                if r is None:
                    continue
                ai = self._ai_scores.get(rerank.key(r))
                if ai is None:
                    continue
                item.setText(4, str(ai))
                if ai >= 7:
                    item.setForeground(4, Qt.darkGreen)
                elif ai <= 3:
                    item.setForeground(4, Qt.red)
            note = "相关度已更新，点击「相关度」表头按分数重排"
        self.lbl_status.setText(note)
        return True

    def _on_rerank_done(self, scores: dict, note: str):
        self._apply_rerank_result(self.sender(), scores, note)

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
        """勾选变化：以稳定 ID 为源同步（隐藏行的勾选同样记录）。"""
        if col == 0:
            r: SearchResult = item.data(0, Qt.UserRole)
            if r is not None:
                key = rerank.key(r)
                if item.checkState(0) == Qt.Checked:
                    self._checked_keys.add(key)
                else:
                    self._checked_keys.discard(key)
            self._sync_selection_ui()

    def check_all(self):
        state = Qt.Checked if any(
            self.tree.topLevelItem(i).checkState(0) != Qt.Checked
            for i in range(self.tree.topLevelItemCount())
        ) else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, state)

    def on_preview(self, item: QTreeWidgetItem, col: int):
        r: SearchResult = item.data(0, Qt.UserRole)
        if r is not None:
            self.start_preview(r)

    def start_preview(self, r: SearchResult):
        """应用内抓取正文并预览（窗口内渲染，含图片）。由双击/悬浮卡「查看正文」触发。"""
        if r is None or self._closing:
            return
        if self.preview_worker is not None:
            return  # 已有预览在抓取
        if self.download_worker is not None or self.search_worker is not None:
            QMessageBox.information(self, "请稍候", "搜索或下载进行中，完成后再预览（共用网络会话）。")
            return
        self.lbl_status.setText("预览抓取中…")
        self.preview_worker = PreviewWorker(self.sogou, r)
        self.preview_worker.done.connect(self._on_preview_done)
        self._track_worker(self.preview_worker, "preview_worker")
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
        if self._closing:
            return
        # 以勾选集合为源（含被筛选隐藏的选中项），数量与界面一致
        items = [
            self._results_by_key[k] for k in self._checked_keys if k in self._results_by_key
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
            retries=int(self.cfg.get("download_retries", 1)),
        )
        self.download_worker.item_status.connect(self.on_item_status)
        self.download_worker.batch_progress.connect(self.on_batch_progress)
        self.download_worker.finished_all.connect(self.on_download_done)
        self._track_worker(self.download_worker, "download_worker")
        self.download_worker.start()

    def on_item_status(self, url: str, text: str, error: bool):
        key = next((k for k, r in self._results_by_key.items()
                    if r.url == url or r.sogou_link == url), None)
        if key is not None:
            self._dl_status[key] = (text, error)  # 重建后按 ID 恢复
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
        self._sync_selection_ui()
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
        base = os.path.expanduser(str(self.cfg["base_dir"]))
        os.makedirs(base, exist_ok=True)
        os.startfile(base)

    def _set_always_on_top(self, enabled: bool):
        geometry = self.saveGeometry()
        state = self.windowState()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.restoreGeometry(geometry)
        self.setWindowState(state)
        self.show()  # 修改原生窗口标志会隐藏窗口，需重新显示
        self.btn_always_on_top.setText("已置顶 · 取消" if enabled else "窗口置顶")

    def eventFilter(self, src, ev):
        """结果表：悬浮展示摘要卡；卡片可见未固定时截获 Tab 为「固定」。"""
        t = ev.type()
        if (
            t == QEvent.KeyPress
            and ev.key() == Qt.Key_Tab
            and src in (self.tree, self.tree.viewport())
            and self.summary_card.isVisible()
            and not self.summary_card.is_pinned()
        ):
            self.summary_card.pin()  # 焦点在结果表时的 Tab = 固定摘要卡
            return True

        if src is not self.tree.viewport():
            return super().eventFilter(src, ev)

        if not self._results:
            return super().eventFilter(src, ev)

        if t in (QEvent.MouseMove, QEvent.HoverMove, QEvent.HoverEnter):
            idx = self.tree.indexAt(ev.position().toPoint())
            r: SearchResult | None = None
            if idx.isValid():
                item = self.tree.topLevelItem(idx.row())
                r = item.data(0, Qt.UserRole) if item is not None else None
            key = rerank.key(r) if r is not None else None
            if r is not None:
                self.summary_card.cancel_close()
            if r is not None and key != self._hover_key:
                self._hover_key = key
                self.summary_card.cancel_pending_show()
                self.summary_card.schedule_show(
                    r, self.input.text().strip(),
                    self.tree.viewport().mapToGlobal(ev.position().toPoint()), 180
                )
            elif r is None and self._hover_key is not None:
                self._hover_key = None
                self.summary_card.cancel_pending_show()
                self.summary_card.schedule_close()
        elif t in (QEvent.Leave, QEvent.HoverLeave):
            self._hover_key = None
            self.summary_card.cancel_pending_show()
            self.summary_card.schedule_close()
        return super().eventFilter(src, ev)

    def closeEvent(self, event):
        # 通知后台线程收尾并短暂等待；等不到就拦下关闭事件，
        # 由各线程原生 finished 触发 _try_finish_close 再真正关闭
        self._closing = True  # 关闭后禁止发起新任务
        self.summary_card.close_card()
        for w in list(self._active_workers):
            w.requestInterruption()
        for w in list(self._active_workers):
            if w.isRunning():
                w.wait(2000)
        self.preview_dlg.cleanup_tmp()
        if self._active_workers:
            event.ignore()
            self.lbl_status.setText("后台任务收尾中，完成后自动关闭…")
        else:
            self.store.close()
            super().closeEvent(event)

    def _try_finish_close(self):
        if not self._closing or self._active_workers:
            return
        self.preview_dlg.cleanup_tmp()
        self.store.close()
        self.close()


def _install_crash_logger():
    """打包版（--windowed）没有 stderr：未捕获异常追加写入 log/crash.log。"""
    from datetime import datetime

    log_dir = APP_DIR / "log"
    prev_hook = sys.excepthook

    def hook(t, v, tb):
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "crash.log", "a", encoding="utf-8") as f:
                f.write(f"\n{datetime.now().isoformat()}\n")
                import traceback
                traceback.print_exception(t, v, tb, file=f)
        except Exception:
            pass
        prev_hook(t, v, tb)

    sys.excepthook = hook


def main():
    _install_crash_logger()
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

    # 截图模式：--shot <路径> 启动→自动搜索→自截窗口→退出（视觉验收用）
    shot_path = None
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot_path = sys.argv[i + 1] if len(sys.argv) > i + 1 else "ui_shot.png"

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    win = MainWindow()
    win.show()
    # 命令行带话题词则自动搜索：python app.py "AI 编程"
    args = sys.argv[1:]
    if "--shot" in args:
        i = args.index("--shot")
        args = args[:i] + args[i + 2:]  # 摘除 --shot 及其路径参数
    topic = " ".join(a for a in args if not a.startswith("-")).strip()
    if topic:
        win.input.setText(topic)
        win.do_search()
    if shot_path:
        from PySide6.QtCore import QTimer

        def _capture():
            # 从鼠标事件入口触发悬停（QTest.mouseMove 在部分平台不派发 hover 时，
            # 退化为 sendEvent 同款鼠标事件——与交互测试一致，仍是事件入口路径）
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
            print(f"[shot] card visible={card.isVisible()} pinned={card.is_pinned()} "
                  f"pos={card.pos()} size={card.size()}")
            img = win.grab().toImage()
            card = win.summary_card
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

        captured = {"done": False}

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
            win.grab().save(shot_path)
            app.quit()

        def _wait_search():
            if win.btn_search.isEnabled():  # 搜索完成（成功或失败）
                QTimer.singleShot(900, _capture)
            else:
                QTimer.singleShot(300, _wait_search)

        QTimer.singleShot(400, _wait_search)
        QTimer.singleShot(20000, _capture)  # 看门狗：搜索再慢也按时出图
    sys.exit(app.exec())


if __name__ == "__main__":
    sys.exit(main() or 0)

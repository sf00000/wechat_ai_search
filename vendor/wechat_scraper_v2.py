#!/usr/bin/env python3
"""
微信短链接爬虫
功能：爬取微信短链接文章内容，包括标题、作者、正文、图片
输入：微信短链接（支持单个或多个）
输出：
  - JSON（.json）
  - Markdown（.md）

在 Markdown「逐篇落盘模式」下：
- 目录：公众号名称/
  - md 文件：文章发布时间_标题_程序开始运行时间.md
  - 图片目录：images/
    - 图片文件：文章发布时间_标题_程序开始运行时间_图片顺序.{扩展名}
"""

import requests
from bs4 import BeautifulSoup
import json
import hashlib
import re
import os
import time
import glob
from urllib.parse import urljoin, urlparse, parse_qs, urldefrag, unquote
from html import unescape
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Callable
from markdownify import markdownify as html_to_markdown


# 批量抓取连续 content_empty/wechat_gate_or_verify 阈值。
# 在 scrape_multiple 内一旦达到该阈值，判定当前 __biz 已被风控，立刻熔断剩余请求。
# 可通过环境变量 WECHAT_EMPTY_FAIL_THRESHOLD 覆盖（必须是正整数）。
try:
    EMPTY_FAIL_THRESHOLD = max(1, int(os.environ.get("WECHAT_EMPTY_FAIL_THRESHOLD", "5")))
except (TypeError, ValueError):
    EMPTY_FAIL_THRESHOLD = 5

# 触发熔断时，把后续 URL 填进 results 的标记 error。
_RATE_LIMITED_SKIP_ERROR = "rate_limited_skip"
# 算作"被限流"的 error 字符串集合。
_RATE_LIMIT_ERRORS = frozenset({"content_empty", "wechat_gate_or_verify"})


def _output_timestamp() -> str:
    """用于文件名的时间戳（不含非法字符）。"""
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def _sanitize_filename_part(s: str, max_len: int = 80) -> str:
    """把任意字符串变为可用于文件名的一段（去除非法字符与多余空白）。"""
    if s is None:
        return "nodate"
    s = str(s).strip()
    if not s:
        return "nodate"
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return "nodate"
    return s[:max_len]


def _extract_publish_time(text: str) -> str:
    """
    从任意文本中抽取发布时间字符串（尽量只匹配头部元信息里的日期时间）。
    支持解析以下输入格式：
    - 2026年3月23日 6:00
    - 2026-03-23 06:00
    无论命中哪个分支，统一归一化输出为：
    - 含时间：YYYY-MM-DD HH:MM
    - 仅日期：YYYY-MM-DD
    （月/日/时补零，保证所有文件名时间格式一致）
    """
    if not text:
        return ""
    t = str(text)

    # 中文格式：YYYY年M月D日 H:MM 或 HH:MM
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", t)
    if m:
        y, mo, d, hh, mm = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(hh):02d}:{mm}"

    # 数字格式：YYYY-MM-DD H:MM
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})\s*(\d{1,2}):(\d{2})", t)
    if m:
        y, mo, d, hh, mm = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(hh):02d}:{mm}"

    # 只有日期：YYYY-MM-DD 或 YYYY年M月D日
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", t)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", t)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # Unix 时间戳兜底：优先秒级（10 位），其次毫秒级（13 位）
    m = re.search(r"\b(1\d{9})\b", t)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)))
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"
        except (OverflowError, OSError, ValueError):
            pass

    m = re.search(r"\b(1\d{12})\b", t)
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)) // 1000)
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"
        except (OverflowError, OSError, ValueError):
            pass

    return ""


def _extract_publish_time_from_js(html_content: str) -> str:
    """
    从页面脚本变量中抽取发布时间（WeChat 常见：window.cgiDataNew.create_time / ori_create_time）。
    由于静态 HTML 里 `#publish_time` 往往为空，这里需要直接解析脚本文本。
    """
    if not html_content:
        return ""

    s = str(html_content)

    # 1) create_time: JsDecode('2026-03-23 06:00')
    #    允许 create_time 前面带 window.cgiDataNew. 前缀。
    m = re.search(
        r"(?:window\.cgiDataNew\.)?create_time\s*:\s*JsDecode\(\s*(['\"])(.*?)\1\s*\)",
        s,
        flags=re.S,
    )
    if m:
        raw = m.group(2).strip()
        extracted = _extract_publish_time(raw)
        return extracted or raw

    # 2) create_time: '2026-03-23 06:00'
    m = re.search(
        r"(?:window\.cgiDataNew\.)?create_time\s*:\s*(['\"])(.*?)\1",
        s,
        flags=re.S,
    )
    if m:
        raw = m.group(2).strip()
        extracted = _extract_publish_time(raw)
        return extracted or raw

    # 3) ori_create_time: '1774216858' * 1  （秒级 / 毫秒级 Unix）
    m = re.search(
        r"(?:window\.cgiDataNew\.)?ori_create_time\s*:\s*(['\"])(\d{9,12})\1\s*(?:\*\s*1)?",
        s,
        flags=re.S,
    )
    if m:
        ts = m.group(2)
        extracted = _extract_publish_time(ts)
        return extracted or ts

    # 4) ori_create_time: 1774216858 * 1
    m = re.search(
        r"(?:window\.cgiDataNew\.)?ori_create_time\s*:\s*(\d{9,12})\s*(?:\*\s*1)?",
        s,
        flags=re.S,
    )
    if m:
        ts = m.group(1)
        extracted = _extract_publish_time(ts)
        return extracted or ts

    return ""


def _looks_like_publish_time(s: str) -> bool:
    """用于过滤错误命中的节点（例如把作者当成发布时间）。"""
    if not s:
        return False
    t = str(s)
    if re.search(r"\d{4}年\d{1,2}月\d{1,2}日", t):
        return True
    if re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", t):
        return True
    if re.search(r"\b(1\d{9}|1\d{12})\b", t):
        return True
    # 兜底：含典型时间分隔符且包含年份
    if re.search(r"\d{4}.*\d{1,2}:\d{2}", t):
        return True
    return False


def _get_account_dir(author: str, default: str = "default") -> str:
    """从文章页面解析到的公众号昵称（author）生成目录名；解析不到时回退 default。"""
    part = _sanitize_filename_part(author or "", max_len=80)
    if part in ("", "nodate"):
        return default
    return part


def _is_wechat_gate_page(html: str) -> bool:
    """微信环境异常 / 验证 / 仅客户端打开等页面，通常无法解析正文。"""
    if not html:
        return False
    markers = (
        "环境异常",
        "当前环境异常",
        "访问过于频繁",
        "验证码",
        "完成验证后即可继续访问",
        "请在微信客户端打开链接",
        "仅支持在微信内访问",
        "登录后查看",
        "登陆后查看",
        "链接已过期",
        "该内容已被发布者删除",
        "内容因违规无法查看",
    )
    if "chrome-error" in html.lower():
        return True
    return any(m in html for m in markers)


def _find_article_content_root(soup: BeautifulSoup):
    """多选择器定位正文容器（不同模板 id/class 略有差异）。"""
    selectors = (
        "#js_content",
        "div#js_content",
        ".rich_media_area_primary #js_content",
        "#js_article_content",
        ".rich_media_area_primary_inner #js_content",
        ".rich_media_content#js_content",
    )
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag is not None:
            return tag
    return None


def _plain_text_from_content_tag(content_tag) -> str:
    """markdownify 得到空串时，用语义文本兜底。"""
    text = content_tag.get_text("\n", strip=True)
    if not text:
        return ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return "\n\n".join(lines)


def _sha256_text(s: str) -> str:
    """对文本做稳定的内容指纹，用于判断“内容是否相同”。"""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _read_download_log(log_path: str) -> list:
    """读取 _state/download_log.json，返回 articles 列表。"""
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("articles", [])
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_download_log(log_path: str, articles: list) -> None:
    """原子写入 _state/download_log.json（列表格式，含元信息）。"""
    from datetime import datetime
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    data = {
        "generated_at": datetime.now().isoformat(),
        "article_count": len(articles),
        "articles": articles,
    }
    tmp_path = log_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Windows can briefly keep the destination open (for example, while an
    # indexer scans the just-written JSON).  Retrying preserves the atomic
    # replace semantics while preventing an otherwise completed album from
    # aborting the whole batch.
    for attempt in range(5):
        try:
            os.replace(tmp_path, log_path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.25 * (attempt + 1))


def _read_latest_log_record(log_path: str, article_key: str) -> Optional[Dict]:
    """从 _state/download_log.json 中按 article_key 查找最新记录（列表末尾优先）。"""
    articles = _read_download_log(log_path)
    result = None
    for r in articles:
        if r.get("article_key") == article_key:
            result = r
    return result


def _append_log_record(log_path: str, record: Dict) -> None:
    """追加或更新一条记录到 _state/download_log.json（保持爬取顺序，upsert by article_key）。"""
    articles = _read_download_log(log_path)
    key = record.get("article_key", "")
    # 已存在则原地更新，否则追加到末尾
    for i, r in enumerate(articles):
        if r.get("article_key") == key:
            articles[i] = record
            break
    else:
        articles.append(record)
    _write_download_log(log_path, articles)



# ---------------------------------------------------------------------------
# 合集增量：已知 msgid 来自 result_root/_state/all_albums_state.json（见 _merge_articles_from_all_albums_state）
# ---------------------------------------------------------------------------


def _reconcile_album_state(
    known_msgids: set,
    articles: Dict[str, Dict[str, str]],
) -> set:
    """
    直接校对本地 md 文件是否存在，不依赖 download_log.jsonl。

    articles: {msgid: {msgid, title, abs_path}}，来自 all_albums_state 合并结果。
    - abs_path 非空且文件不存在 → msgid 移出 known，下次重新下载
    - abs_path 为空（占位）→ 不做删除，存疑保留

    全程仅本地 os.path.exists 检查，无网络请求。
    """
    if not known_msgids or not articles:
        return known_msgids

    missing: set = set()
    for msgid in known_msgids:
        article = articles.get(msgid) or {}
        md_path = str(article.get("abs_path") or "").strip()
        if md_path and not os.path.exists(md_path):
            missing.add(msgid)

    if missing:
        return known_msgids - missing
    return known_msgids


def _merge_articles_from_all_albums_state(
    result_root: str,
    album_id_key: str,
    album_title: str,
    articles: Dict[str, Dict[str, str]],
) -> None:
    """从 all_albums_state.json 补齐 articles：按 album_id 或与微信解析标题一致的 album_title 匹配。

    仅追加当前 articles 中不存在的 msgid，便于在仅有汇总文件、album_*.json 未同步时仍能 URL 级预过滤，
    避免逐篇请求正文再靠 hash 跳过。
    """
    path = os.path.join(result_root, "_state", "all_albums_state.json")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    for a in data.get("albums") or []:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("album_id") or "").strip()
        atitle = str(a.get("album_title") or "").strip()
        if album_id_key and aid and aid == album_id_key:
            pass
        elif atitle and atitle == album_title:
            pass
        else:
            continue
        raw = a.get("articles") or {}
        if not isinstance(raw, dict):
            continue
        for mid, rec in raw.items():
            msgid = str(mid or "").strip()
            if not msgid:
                continue
            if msgid in articles:
                continue
            if isinstance(rec, dict):
                articles[msgid] = {
                    "msgid": msgid,
                    "title": str(rec.get("title") or ""),
                    "abs_path": str(rec.get("abs_path") or rec.get("md_path") or ""),
                }
            else:
                p = str(rec or "").strip()
                articles[msgid] = {"msgid": msgid, "title": "", "abs_path": p}


def _delete_article_outputs(md_dir: str, account_images_dir: str, old_file_prefix: str) -> None:
    """
    删除旧版本文章对应的 md 与图片：
    - md：{old_file_prefix}*.md
    - 图片：{old_file_prefix}_*.*（按 basename 前缀删除）
    """
    if not old_file_prefix:
        return

    for md_path in glob.glob(os.path.join(md_dir, f"{old_file_prefix}*.md")):
        try:
            os.remove(md_path)
        except OSError:
            pass

    for img_path in glob.glob(os.path.join(account_images_dir, f"{old_file_prefix}_*.*")):
        try:
            os.remove(img_path)
        except OSError:
            pass


def _delete_article_outputs_by_base(
    md_dir: str,
    account_images_dir: str,
    article_base: str,
) -> None:
    """
    删除某篇文章的所有历史落盘（按 article_base 前缀删除）。
    article_base 形如：{publish_part}_{title_part}，而 file_prefix 形如：
    {article_base}_{runTs}，图片 basename 也会以该 file_prefix 为前缀。
    """
    if not article_base:
        return

    # 删除该篇文章所有版本 md
    for md_path in glob.glob(os.path.join(md_dir, f"{article_base}_*.md")):
        try:
            os.remove(md_path)
        except OSError:
            pass

    # 删除该篇文章所有版本图片（basename 以 file_prefix_开头，因此也以 article_base_开头）
    for img_path in glob.glob(os.path.join(account_images_dir, f"{article_base}_*.*")):
        try:
            os.remove(img_path)
        except OSError:
            pass


_ALBUM_BLOCK_START_RE = re.compile(r"var\s+album_info_list\s*=")
_ALBUM_ID_VAL_RE = re.compile(r"albumId:\s*'(\d{15,})'")
_ALBUM_TITLE_VAL_RE = re.compile(r"title:\s*'([^']*)'")
_ALBUM_SIZE_VAL_RE = re.compile(r"size:\s*'(\d+)'")
_ALBUM_SCAN_WINDOW = 50000


def _extract_album_infos(html_content: str) -> List[Dict[str, object]]:
    """从文章页 JS 的 album_info_list 抽取所属合集（一篇文章可同时属于多个合集）。

    不按 `]` 定位数组结尾——条目内嵌的正则字面量可能含 `]`，会把块截断；
    改为从赋值处向后取固定窗口，按 albumId 出现位置切分条目，
    title/size 取各自条目段内最靠近的前值。
    页面无该变量（文章不属于任何合集）时返回空列表。
    """
    if not html_content:
        return []
    start_m = _ALBUM_BLOCK_START_RE.search(html_content)
    if not start_m:
        return []
    region = html_content[start_m.start():start_m.start() + _ALBUM_SCAN_WINDOW]
    out: List[Dict[str, object]] = []
    seen: set = set()
    prev_end = 0
    for m in _ALBUM_ID_VAL_RE.finditer(region):
        seg = region[prev_end:m.start()]
        prev_end = m.end()
        album_id = m.group(1)
        if album_id in seen:
            continue
        seen.add(album_id)
        titles = _ALBUM_TITLE_VAL_RE.findall(seg)
        sizes = _ALBUM_SIZE_VAL_RE.findall(seg)
        out.append({
            "album_id": album_id,
            "title": titles[-1].strip() if titles else "",
            "size": int(sizes[-1]) if sizes else 0,
        })
    return out


_ALLOWED_URL_HOSTS = ("mp.weixin.qq.com", "w.url.cn", "url.cn", "mm.bizurl.cn", "j.url.cn")


def _is_wechat_host(url: str) -> bool:
    """严格按 hostname 白名单判断微信链接。

    子串匹配会把 `https://evil.example/?next=mp.weixin.qq.com`、
    `https://mp.weixin.qq.com.evil.example/s/x` 这类地址误判成微信，这里只认
    urlparse 出来的 hostname 本身（含子域名）。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_URL_HOSTS)


class WechatScraper:
    """微信短链接爬虫类"""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
        }
        self.session = requests.Session()
        self._run_ts: Optional[str] = None
        self._image_seq: int = 0

    def _headers_for_mp_article(self, article_url: str, mobile: bool = False) -> Dict[str, str]:
        """公众号文章页请求头：更接近真实浏览器，降低空正文概率。"""
        ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.48 Language/zh_CN"
        ) if mobile else (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        h = dict(self.headers)
        h["User-Agent"] = ua
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        h["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        h["Referer"] = "https://mp.weixin.qq.com/"
        h["Cache-Control"] = "max-age=0"
        if not mobile:
            h["Sec-Fetch-Dest"] = "document"
            h["Sec-Fetch-Mode"] = "navigate"
            h["Sec-Fetch-Site"] = "none"
            h["Sec-Fetch-User"] = "?1"
            h["Upgrade-Insecure-Requests"] = "1"
        return h

    @staticmethod
    def _looks_like_image_bytes(data: bytes) -> bool:
        """根据魔数判断响应体是否像图片。"""
        if not data:
            return False
        return (
            data.startswith(b"\xff\xd8\xff")  # jpg
            or data.startswith(b"\x89PNG\r\n\x1a\n")  # png
            or data.startswith(b"GIF87a")
            or data.startswith(b"GIF89a")
            or data.startswith(b"RIFF") and b"WEBP" in data[:16]
        )

    def resolve_short_url(self, short_url: str) -> Optional[str]:
        """
        解析短链接，获取真实URL
        """
        try:
            response = self.session.get(
                short_url,
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=True
            )
            return response.url
        except requests.RequestException as e:
            print(f"解析短链接失败: {short_url}, 错误: {e}")
            return None

    def is_wechat_url(self, url: str) -> bool:
        """检查URL是否为微信链接（严格 hostname 白名单，不做子串匹配）"""
        return _is_wechat_host(url)

    def download_image(
        self,
        img_url: str,
        output_dir: str,
        basename: str,
        referer: Optional[str] = None,
    ) -> Optional[str]:
        """下载图片到本地，文件名为 {basename}{扩展名}。"""
        try:
            os.makedirs(output_dir, exist_ok=True)
            attempts: List[Dict[str, str]] = []
            if referer:
                attempts.append({"Referer": referer})
            attempts.append({})

            for extra_headers in attempts:
                headers = dict(self.headers)
                headers.update(extra_headers)
                headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

                response = self.session.get(img_url, headers=headers, timeout=self.timeout)
                if response.status_code != 200:
                    continue

                content = response.content or b""
                content_type = (response.headers.get("Content-Type") or "").lower()
                is_image = ("image" in content_type) or self._looks_like_image_bytes(content)
                if not is_image:
                    continue

                ext = self._get_image_extension(response.headers.get('Content-Type', ''))
                filename = f"{basename}{ext}"
                filepath = os.path.join(output_dir, filename)

                with open(filepath, 'wb') as f:
                    f.write(content)
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    return filepath
            print(f"下载图片失败: {img_url}")
        except Exception as e:
            print(f"下载图片失败: {img_url}, 错误: {e}")
        return None

    def _get_image_extension(self, content_type: str) -> str:
        """根据Content-Type获取图片扩展名"""
        type_map = {
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
        }
        return type_map.get(content_type.lower(), '.jpg')

    def extract_content(
        self,
        html_content: str,
        url: str,
        images_dir: str = 'images',
        output_root: Optional[str] = None,
        dedupe: bool = False,
    ) -> Dict:
        """
        从HTML中提取微信文章内容
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        result = {
            'url': url,
            'title': '',
            'author': '',
            'publish_time': '',
            'content': '',
            # 仅用于“内容是否相同”的判断（基于 content 的稳定字符串指纹）
            'content_hash': '',
            # 文章去重的稳定 key（不包含 runTs）
            '_article_base': '',
            # 便于终端输出：download / update / skip
            'action': 'download',
            # 标记：若内容与上次相同则跳过落盘与图片下载
            'skip': False,
            'skip_reason': '',
            'images': [],
            'local_images': [],
            # 记录：远程图片URL -> 本地落盘路径（用于把 md 里的链接改成本地相对路径）
            'image_map': {},
            'digest': '',
            # 文章所属合集（来自页面 JS 的 album_info_list），供下载入口自动订阅用
            'albums': [],
            'crawl_time': datetime.now().isoformat(),
            'success': False,
            'error': None
        }

        try:
            # 合集信息：放在最前，重复文章走 skip 提前返回时也能带上
            result['albums'] = _extract_album_infos(html_content or "")

            # 提取标题
            title_tag = soup.find('h1', class_='rich_media_title')
            if title_tag:
                result['title'] = title_tag.get_text(strip=True)
            if not result.get("title"):
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    result["title"] = str(og_title.get("content", "")).strip()

            # 提取作者
            author_tag = soup.find('a', class_='rich_media_meta rich_media_meta_link rich_media_meta_nickname')
            if not author_tag:
                author_tag = soup.find('span', class_='rich_media_meta rich_media_meta_nickname')
            if author_tag:
                result['author'] = author_tag.get_text(strip=True)

            # 提取发布时间
            time_tag = soup.find(attrs={'id': 'publish_time'})
            extracted_time: str = ""
            if time_tag:
                extracted_time = time_tag.get_text(strip=True) or ""
            if extracted_time and _looks_like_publish_time(extracted_time):
                result['publish_time'] = extracted_time
            else:
                # 兜底：在 rich_media_meta 或整段 HTML 文本中抽取日期/时间戳
                meta_region = soup.select_one('.rich_media_meta') or soup
                extracted = _extract_publish_time(meta_region.get_text(' ', strip=True))
                if extracted:
                    result['publish_time'] = extracted
                else:
                    # 再兜底：在 HTML 前段文本中抓取第一个日期时间模式
                    # 一些页面的时间信息可能出现在脚本注入内容中，因此放大搜索范围
                    extracted2 = _extract_publish_time(html_content or "")
                    if extracted2:
                        result['publish_time'] = extracted2
                    else:
                        # 再兜底：直接从 <script> 的 window.cgiDataNew 抽取 create_time / ori_create_time
                        extracted3 = _extract_publish_time_from_js(html_content or "")
                        if extracted3 and _looks_like_publish_time(extracted3):
                            result['publish_time'] = extracted3

            # 文件命名前缀分两套：
            # - _file_prefix：图片 basename 用，形如 <发布时间>_<标题>，与公众号无关
            # - _md_file_prefix：md 文件名用，形如 <发布时间>|<公众号>|<标题>，含公众号
            publish_part = _sanitize_filename_part(result.get('publish_time') or "nodate")
            title_part = _sanitize_filename_part(result.get('title') or "untitled")
            author_part = _sanitize_filename_part(result.get('author') or "default", max_len=40)
            article_base = f"{publish_part}_{title_part}"
            file_prefix = article_base
            result['_article_base'] = article_base
            result['_file_prefix'] = file_prefix
            result['_md_file_prefix'] = f"{publish_part}｜{author_part}｜{title_part}"

            # 公众号目录：从页面解析的 author 字段；失败则放到 default/
            account_dir = _get_account_dir(result.get('author') or "")
            result['_account_dir'] = account_dir
            # 图片目录：按公众号分子目录 images/<公众号>/
            _sanitized_account = _sanitize_filename_part(account_dir, max_len=80) or "default"
            account_images_dir = os.path.join(images_dir, _sanitized_account)
            os.makedirs(account_images_dir, exist_ok=True)

            # 提取摘要
            digest_tag = soup.find('meta', property='og:description')
            if digest_tag:
                result['digest'] = digest_tag.get('content', '')

            # 提取正文内容
            content_tag = _find_article_content_root(soup)
            if content_tag:
                # 移除script和style标签
                for tag in content_tag.find_all(['script', 'style']):
                    tag.decompose()

                # 处理图片：先收集远程图片URL并写回 img['src']，但不立刻下载
                # （用于去重：内容相同则直接跳过图片下载与落盘）
                images = content_tag.find_all('img')
                for img in images:
                    original_url = (
                        img.get('data-src')
                        or img.get('data-original')
                        or img.get('src')
                    )
                    if not original_url:
                        continue
                    absolute_url = urljoin(url, original_url)
                    img['src'] = absolute_url
                    result['images'].append(absolute_url)

                # 预处理代码块：WeChat 的 <pre> 内常用多个 <code> 逐行包裹，
                # markdownify 会把它们拼成一行；这里先合并为单个 <pre><code> 块。
                for pre_tag in content_tag.find_all('pre'):
                    code_children = pre_tag.find_all('code')
                    if len(code_children) > 1:
                        lines = [c.get_text() for c in code_children]
                        merged_text = "\n".join(lines)
                        merged_text = merged_text.replace('\xa0', ' ')
                        pre_tag.clear()
                        new_code = soup.new_tag('code')
                        new_code.string = merged_text
                        pre_tag.append(new_code)
                    elif len(code_children) == 1:
                        # 单个 <code> 但文本中可能缺少换行（WeChat 用 <br> 或 \n 混合）
                        raw = code_children[0].decode_contents()
                        # 把 <br> / <br/> 替换成换行符
                        raw = re.sub(r'<br\s*/?>', '\n', raw)
                        # 去掉其余内嵌 HTML 标签（如 <span>）
                        cleaned = re.sub(r'<[^>]+>', '', raw)
                        cleaned = unescape(cleaned)
                        # 替换 NBSP 为普通空格
                        cleaned = cleaned.replace('\xa0', ' ')
                        code_children[0].clear()
                        code_children[0].string = cleaned

                # HTML 转 Markdown：保留标题层级、加粗、列表与代码块（避免 get_text 拍平结构）
                inner_html = content_tag.decode_contents()
                result['content'] = html_to_markdown(
                    inner_html,
                    heading_style='ATX',
                    bullets='-',
                )
                result['content'] = re.sub(r'\n{3,}', '\n\n', result['content'].strip())
                if not result['content'].strip():
                    result['content'] = _plain_text_from_content_tag(content_tag)

                # 基于“远程内容Markdown”（其中图片引用使用的是远程URL）计算指纹
                result['content_hash'] = _sha256_text(result.get('content') or "")

                # 去重逻辑（仅在 markdown 落盘模式启用）
                if dedupe and output_root and result.get('_article_base'):
                    article_key = result.get('_article_base')
                    md_dir = os.path.join(output_root, account_dir)
                    os.makedirs(md_dir, exist_ok=True)
                    log_path = os.path.join(output_root, "_state", "download_log.json")
                    prev = _read_latest_log_record(log_path, article_key)

                    if (
                        prev
                        and prev.get("status") == "success"
                        and prev.get("content_hash")
                        and result.get("content_hash")
                        and prev.get("content_hash") == result.get("content_hash")
                    ):
                        result["skip"] = True
                        result["skip_reason"] = "content_same"
                        result["action"] = "skip"
                        result["success"] = True
                        # 用日志中保存的原始 file_prefix / md_path，避免时间戳不一致导致路径找不到
                        if prev.get("file_prefix"):
                            result["_file_prefix"] = prev["file_prefix"]
                        if prev.get("md_path"):
                            result["md_path"] = prev["md_path"]
                        return result

                    # 内容不一样：删除上一版 md 与图片，然后继续下载新版本
                    if prev and prev.get("status") == "success":
                        result["action"] = "update"
                        article_base = result.get("_article_base") or ""
                        # 删除该篇文章的所有历史落盘（按 article_base 前缀，覆盖图片 + 旧版 md）
                        _delete_article_outputs_by_base(md_dir, account_images_dir, article_base)
                        # 额外按 download_log 里精确路径删一遍旧 md：新命名是
                        # <发布时间>|<公众号>|<标题>.md，不被上面前缀 glob 捕获
                        prev_md_path = prev.get("md_path") or ""
                        if prev_md_path and os.path.exists(prev_md_path):
                            try:
                                os.remove(prev_md_path)
                            except OSError:
                                pass

                # 需要保存/更新：下载图片并建立远程URL -> 本地路径映射
                local_images: List[str] = []
                image_map: Dict[str, str] = {}
                for idx, remote_url in enumerate(result.get("images") or []):
                    basename = f"{file_prefix}_{idx + 1}"
                    local_path = self.download_image(
                        remote_url,
                        account_images_dir,
                        basename,
                        referer=url,
                    )
                    if local_path and os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                        local_images.append(local_path)
                        image_map[remote_url] = local_path

                result['local_images'] = local_images
                result['image_map'] = image_map

            # 只有在正文成功抽取时才认为成功；否则避免把空内容写回去
            body = (result.get('content') or '').strip()
            result['success'] = bool(body)
            if not result['success'] and not result.get('error'):
                if _is_wechat_gate_page(html_content or ""):
                    result['error'] = 'wechat_gate_or_verify'
                else:
                    result['error'] = 'content_empty'

        except Exception as e:
            result['error'] = str(e)

        return result

    def scrape(
        self,
        url: str,
        images_dir: str = 'images',
        output_root: Optional[str] = None,
        dedupe: bool = False,
    ) -> Dict:
        """
        爬取单个微信链接
        """
        if not self._run_ts:
            self._run_ts = _output_timestamp()

        # 如果是短链接，先解析
        if self.is_wechat_url(url):
            if 'mp.weixin.qq.com' not in url:
                print(f"正在解析短链接: {url}")
                resolved_url = self.resolve_short_url(url)
                if resolved_url:
                    url = resolved_url
                    print(f"解析成功: {url}")
                else:
                    return {
                        'url': url,
                        'success': False,
                        'error': '无法解析短链接',
                        'crawl_time': datetime.now().isoformat()
                    }

        # 获取页面内容（桌面 UA + 微信内 UA 各试一次，缓解空正文）
        url_clean, _frag = urldefrag(url)
        header_variants = (
            self._headers_for_mp_article(url_clean, mobile=False),
            self._headers_for_mp_article(url_clean, mobile=True),
        )
        last_http_status = None
        last_out: Optional[Dict] = None

        try:
            for hdr in header_variants:
                response = self.session.get(
                    url_clean,
                    headers=hdr,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                last_http_status = response.status_code
                enc = response.encoding or ""
                if not enc or enc.lower() == "iso-8859-1":
                    response.encoding = response.apparent_encoding or "utf-8"
                if response.status_code != 200:
                    continue

                page_url = response.url or url_clean
                out = self.extract_content(
                    response.text,
                    page_url,
                    images_dir=images_dir,
                    output_root=output_root,
                    dedupe=dedupe,
                )
                out["url"] = url_clean
                last_out = out
                if out.get("success"):
                    return out

            if last_out is not None:
                return last_out

            return {
                'url': url_clean,
                'success': False,
                'error': f'HTTP状态码: {last_http_status}',
                'crawl_time': datetime.now().isoformat()
            }

        except requests.RequestException as e:
            return {
                'url': url_clean,
                'success': False,
                'error': str(e),
                'crawl_time': datetime.now().isoformat()
            }

    def scrape_multiple(
        self,
        urls: List[str],
        delay: float = 1.0,
        images_dir: str = 'images',
        run_ts: Optional[str] = None,
        output_root: Optional[str] = None,
        dedupe: bool = False,
        progress_callback: Optional[Callable[[int, int, Dict], None]] = None,
    ) -> List[Dict]:
        """
        批量爬取多个链接

        Args:
            urls: 链接列表
            delay: 请求间隔（秒）
            images_dir: 图片保存目录
            run_ts: 本批次图片文件名时间戳前缀；默认与首次生成一致

        Returns:
            爬取结果列表
        """
        self._run_ts = run_ts or _output_timestamp()
        self._image_seq = 0
        results = []
        total = len(urls)
        consecutive_empty_failures = 0
        circuit_broken = False

        for idx, url in enumerate(urls, 1):
            result = self.scrape(
                url,
                images_dir=images_dir,
                output_root=output_root,
                dedupe=dedupe,
            )
            results.append(result)
            action = result.get("action") or ("skip" if result.get("skip") else "download")

            if result.get("success"):
                print(f"[{idx}/{total}] {'跳过(内容未变)' if action == 'skip' else ('更新(内容变了)' if action == 'update' else '下载完成')}: {url}")
                consecutive_empty_failures = 0
            else:
                err = str(result.get("error") or "")
                print(f"[{idx}/{total}] 失败: {url}（{err}）")
                if err in _RATE_LIMIT_ERRORS:
                    consecutive_empty_failures += 1
                else:
                    consecutive_empty_failures = 0

            if progress_callback is not None:
                progress_callback(
                    idx,
                    total,
                    {
                        "url": url,
                        "action": action,
                        "success": bool(result.get("success")),
                        "error": str(result.get("error") or ""),
                    },
                )

            # 熔断：连续 N 次 content_empty 视为被风控，让出时间窗口，剩余 URL 标记为 rate_limited_skip。
            if consecutive_empty_failures >= EMPTY_FAIL_THRESHOLD and idx < total:
                remaining = urls[idx:]
                print(
                    f"⚠️ 连续 {consecutive_empty_failures} 次 content_empty/gate，疑似被风控，"
                    f"跳过本批剩余 {len(remaining)} 个 URL"
                )
                now_iso = datetime.now().isoformat()
                for skipped_url in remaining:
                    results.append({
                        'url': skipped_url,
                        'success': False,
                        'error': _RATE_LIMITED_SKIP_ERROR,
                        'skip_reason': 'batch_circuit_break',
                        'crawl_time': now_iso,
                    })
                    if progress_callback is not None:
                        progress_callback(
                            len(results),
                            total,
                            {
                                "url": skipped_url,
                                "action": "rate_limited_skip",
                                "success": False,
                                "error": _RATE_LIMITED_SKIP_ERROR,
                            },
                        )
                circuit_broken = True
                break

            if idx < total:
                # 已存在内容仅做跳过时，不必按完整抓取节奏等待
                wait_seconds = min(delay, 0.1) if action == "skip" else delay
                if wait_seconds > 0:
                    time.sleep(wait_seconds)

        if circuit_broken:
            # 标记一次便于上层快速判断（不破坏现有 list[Dict] 协议）。
            try:
                results[0].setdefault('_batch_circuit_break', True)
            except (IndexError, AttributeError):
                pass

        return results

    def save_articles_to_markdown_by_account(self, results: List[Dict], output_root: str) -> None:
        """
        按公众号目录保存 Markdown 与图片：
        - md：{output_root}/{account_dir}/{file_prefix}.md
        - 图片：在 extract_content 阶段已保存到 {images_dir}/{account_dir}/
        """
        saved = 0
        skipped = 0
        updated = 0
        for r in results:
            if not r.get('success'):
                continue
            if r.get("skip"):
                skipped += 1
                # 记录已存在的 md 路径，供日志汇总使用
                _skip_acc = r.get('_account_dir') or _get_account_dir(r.get('author') or "")
                _skip_acc = _sanitize_filename_part(_skip_acc, max_len=80) if _skip_acc else "default"
                _skip_fp = r.get('_file_prefix') or ""
                if _skip_fp and _skip_acc:
                    _skip_md = os.path.join(output_root, _skip_acc, f"{_skip_fp}.md")
                    if os.path.exists(_skip_md):
                        r["md_path"] = _skip_md
                continue
            if r.get("action") == "update":
                updated += 1

            account_dir = r.get('_account_dir') or _get_account_dir(r.get('author') or "")
            account_dir = _sanitize_filename_part(account_dir, max_len=80) if account_dir else "default"
            if account_dir in ("", "nodate"):
                account_dir = "default"

            # MD 文件名优先用 _md_file_prefix（含 <发布时间>|<公众号>|<标题>），
            # 没有再回退到 _file_prefix。
            file_prefix = r.get('_md_file_prefix') or r.get('_file_prefix')
            if not file_prefix:
                publish_part = _sanitize_filename_part(r.get('publish_time') or "nodate")
                title_part = _sanitize_filename_part(r.get('title') or "untitled")
                author_part = _sanitize_filename_part(r.get('author') or "default", max_len=40)
                file_prefix = f"{publish_part}｜{author_part}｜{title_part}"

            # 合集模式（r 上挂了 _album_title_context）下，md 直接放到合集根下，
            # 不再额外嵌套 <公众号>/ 子目录——因为 md 文件名本身就含公众号。
            if r.get('_album_title_context'):
                md_dir = output_root
            else:
                md_dir = os.path.join(output_root, account_dir)
            os.makedirs(md_dir, exist_ok=True)
            md_path = os.path.join(md_dir, f"{file_prefix}.md")
            if os.path.exists(md_path):
                md_path = os.path.join(md_dir, f"{file_prefix}_dup.md")

            title = r.get('title') or '无标题'
            content_text = r.get('content', '')

            # 把 md 里的远程图片 URL 替换成本地相对路径：方便离线阅读/跳转
            image_map: Dict[str, str] = r.get('image_map') or {}
            for remote_url, local_path in image_map.items():
                if not remote_url or not local_path:
                    continue
                if not os.path.exists(local_path):
                    # 本地图片缺失时保留远程 URL，避免写入不可用本地路径
                    continue
                rel_path = os.path.relpath(local_path, md_dir)
                content_text = content_text.replace(remote_url, rel_path)

            album_extra = ""
            _at = str(r.get("_album_title_context") or "").strip()
            if _at:
                album_extra += f"- 合集: {_at}\n"
            _aid = str(r.get("_album_id") or "").strip()
            if _aid:
                album_extra += f"- 合集 album_id: {_aid}\n"
            _aurl = str(r.get("_album_url") or "").strip()
            if _aurl:
                album_extra += f"- 合集链接: {_aurl}\n"

            parts: List[str] = [
                f"# {title}\n\n",
                (
                    f"- 链接: {r.get('url', '')}\n"
                    f"- 作者: {r.get('author', '')}\n"
                    f"- 发布时间: {r.get('publish_time', '')}\n"
                    f"- 爬取时间: {r.get('crawl_time', '')}\n"
                    + album_extra
                    + "\n"
                ),
            ]
            if r.get('digest'):
                parts.append(f"> {r['digest']}\n\n")
            parts.append(content_text)
            parts.append("\n")

            with open(md_path, "w", encoding="utf-8") as f:
                f.write("".join(parts))
            r["md_path"] = md_path
            saved += 1

            # 记录到本地下载日志：用于下一次对比内容是否变化
            article_key = r.get("_article_base") or ""
            log_path = os.path.join(output_root, "_state", "download_log.json")
            _append_log_record(
                log_path,
                {
                    "article_key": article_key,
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "author": r.get("author", ""),
                    "publish_time": r.get("publish_time", ""),
                    "content_hash": r.get("content_hash", ""),
                    "file_prefix": r.get("_file_prefix", ""),
                    "md_path": md_path,
                    "crawl_time": r.get("crawl_time", ""),
                    "status": "success",
                },
            )

        print(f"\n已保存 Markdown：{saved}/{len(results)} 篇（更新 {updated} 篇，跳过 {skipped} 篇）（按公众号目录）→ {output_root}")


def _extract_album_title(html_content: str) -> str:
    """从集合页 HTML 中提取集合标题。"""
    if not html_content:
        return "album"

    soup = BeautifulSoup(html_content, "html.parser")

    album_label = soup.select_one("#js_tag_name")
    if album_label:
        title = album_label.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return title

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title.get("content", "").strip()
        if title:
            return title

    script_patterns = [
        r"window\.cgiData\s*=\s*\{.*?\btitle\s*:\s*'([^'\n\r]{1,120})'",
        r"window\.cgiData\s*=\s*\{.*?\bmsg_title\s*:\s*'([^'\n\r]{1,120})'",
    ]
    for pattern in script_patterns:
        match = re.search(pattern, html_content, re.DOTALL)
        if not match:
            continue
        title = unescape(match.group(1)).strip()
        if title:
            return title

    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
        title = re.sub(r"\s*[-|_]\s*微信公众平台.*$", "", title).strip()
        if title:
            return title

    # 回退：尝试提取“XX 篇内容”前的短文本
    m = re.search(r"([^\n\r]{2,40})\s*\n\s*[0-9]+\s*篇内容", html_content)
    if m:
        t = m.group(1).strip()
        if t:
            return t
    return "album"


def _extract_biz_from_album_html(html_content: str) -> str:
    """从集合页 HTML 中提取 __biz（用于缺失 __biz 时补全 JSON 翻页请求）。"""
    if not html_content:
        return ""

    patterns = [
        r"[?&]__biz=([A-Za-z0-9%+/=]+)",
        r'"__biz"\s*:\s*"([A-Za-z0-9%+/=]+)"',
        r"'__biz'\s*:\s*'([A-Za-z0-9%+/=]+)'",
        r"__biz\s*:\s*JsDecode\(\s*(['\"])(.*?)\1\s*\)",
    ]

    for pattern in patterns:
        m = re.search(pattern, html_content, flags=re.S)
        if not m:
            continue
        raw = (m.group(2) if len(m.groups()) >= 2 else m.group(1)) or ""
        candidate = unquote(unescape(raw)).strip()
        if re.fullmatch(r"[A-Za-z0-9+/=]+", candidate):
            return candidate

    return ""


def _extract_album_article_count_hint(html_content: str) -> Optional[int]:
    """从合集页 HTML 中解析「N 篇内容」等，用于 JSON 早停时仍继续翻页。"""
    if not html_content:
        return None
    for pattern in (
        r"(?:>|['\"])\s*([0-9]{1,6})\s*<\s*/[^>]+>\s*篇内容",
        r"([0-9]{1,6})\s*篇内容",
        r"共\s*([0-9]{1,6})\s*篇",
    ):
        m = re.search(pattern, html_content)
        if not m:
            continue
        raw = m.group(1).strip()
        try:
            n = int(raw)
            if 1 <= n <= 500000:
                return n
        except ValueError:
            continue
    return None


def _mid_from_mp_article_url(url: str) -> str:
    try:
        q = parse_qs(urlparse(str(url or "").strip()).query)
        mid = str((q.get("mid") or [""])[0]).strip()
        if re.fullmatch(r"[1-9]\d*", mid):
            return mid
    except Exception:
        pass
    return ""


def _album_article_msgid_from_json(art: dict) -> str:
    """合集 JSON 单条的 msgid；缺省时从 url 的 mid= 解析（与增量汇总中的 msgid 对齐）。"""
    if not isinstance(art, dict):
        return ""
    for key in ("msgid", "msg_id", "mid"):
        v = str(art.get(key) or "").strip()
        if re.fullmatch(r"[1-9]\d*", v):
            return v
    return _mid_from_mp_article_url(str(art.get("url") or ""))


def _album_article_itemidx_from_json(art: dict) -> str:
    if not isinstance(art, dict):
        return ""
    for key in ("itemidx", "item_idx"):
        if art.get(key) is not None:
            v = str(art.get(key)).strip()
            if re.fullmatch(r"\d+", v):
                return v
    u = str(art.get("url") or "").strip()
    if u:
        try:
            q = parse_qs(urlparse(u).query)
            idx = str((q.get("idx") or [""])[0]).strip()
            if re.fullmatch(r"\d+", idx):
                return idx
        except Exception:
            pass
    return ""


def _album_json_total_article_hint(base_info: dict, album_obj: dict) -> Optional[int]:
    for d in (base_info, album_obj):
        if not isinstance(d, dict):
            continue
        for k in (
            "article_cnt",
            "total_article_cnt",
            "all_article_cnt",
            "content_cnt",
            "article_count",
            "total_count",
        ):
            raw = d.get(k)
            if raw is None:
                continue
            try:
                n = int(raw)
                if 1 <= n <= 500000:
                    return n
            except (TypeError, ValueError):
                continue
    return None


def _album_continue_flag_truthy(raw) -> bool:
    if raw is True or raw == 1:
        return True
    if raw is False or raw == 0:
        return False
    s = str(raw).strip().lower()
    return s in ("1", "true", "yes")


def _coerce_album_article_list_item(art) -> Optional[dict]:
    """微信 getalbum 的 article_list 有时为 dict，偶发为 JSON 字符串或直接是文章 URL 字符串。"""
    if isinstance(art, dict):
        return art
    if isinstance(art, str):
        s = art.strip()
        if not s:
            return None
        if s.startswith("{") and "}" in s:
            try:
                o = json.loads(s)
                if isinstance(o, dict):
                    return o
            except json.JSONDecodeError:
                pass
        if "mp.weixin.qq.com" in s:
            return {"url": s}
    return None


def extract_wechat_album_urls(album_url: str, timeout: int = 20) -> Dict:
    """
    解析微信集合页，提取文章链接列表。

    Returns:
        {
            "success": bool,
            "album_title": str,
            "urls": List[str],
            "page_count": int,
            "article_count": int,
            "source": str,
            "error": Optional[str],
        }
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    clean_url = album_url.strip()
    if "#wechat_redirect" in clean_url:
        clean_url = clean_url.split("#wechat_redirect", 1)[0]

    # 先请求 HTML（用于标题兜底）
    try:
        html_resp = requests.get(clean_url, headers=headers, timeout=timeout)
        html_resp.encoding = "utf-8"
    except requests.RequestException as e:
        return {"success": False, "album_title": "album", "urls": [], "error": str(e)}

    if html_resp.status_code != 200:
        return {
            "success": False,
            "album_title": "album",
            "urls": [],
            "error": f"HTTP状态码: {html_resp.status_code}",
        }

    html_text = html_resp.text or ""
    album_title = _extract_album_title(html_text)
    html_article_total_hint = _extract_album_article_count_hint(html_text)

    # 优先走 JSON 接口，稳定拿到 article_list（支持翻页）
    parsed_parts = urlparse(clean_url)
    query = parse_qs(parsed_parts.query)
    biz = (query.get("__biz") or [""])[0]
    album_id = (query.get("album_id") or [""])[0]
    if not biz:
        biz = _extract_biz_from_album_html(html_text)

    json_urls: List[str] = []
    json_url_to_msgid: Dict[str, str] = {}  # url -> msgid，供增量过滤使用
    json_page_count = 0
    if biz and album_id:
        begin_msgid = ""
        begin_itemidx = ""
        page_count = 0
        max_page_count = 1000  # 安全上限，避免异常循环；足够覆盖大多数超长专辑
        seen_page_cursors = set()
        while page_count < max_page_count:
            cursor = (begin_msgid, begin_itemidx)
            if cursor in seen_page_cursors:
                break
            seen_page_cursors.add(cursor)
            page_count += 1
            json_page_count = page_count
            page_size_req = 20
            params = {
                "__biz": biz,
                "action": "getalbum",
                "album_id": album_id,
                "f": "json",
                "count": str(page_size_req),
            }
            if begin_msgid:
                params["begin_msgid"] = begin_msgid
            if begin_itemidx:
                params["begin_itemidx"] = begin_itemidx

            try:
                json_resp = requests.get(
                    "https://mp.weixin.qq.com/mp/appmsgalbum",
                    params=params,
                    headers=headers,
                    timeout=timeout,
                )
                json_resp.encoding = "utf-8"
            except requests.RequestException:
                break

            if json_resp.status_code != 200:
                break

            try:
                obj = json_resp.json()
            except ValueError:
                break

            album_obj = obj.get("getalbum_resp") or {}
            base_info = album_obj.get("base_info") or {}
            if base_info.get("title"):
                album_title = str(base_info.get("title")).strip() or album_title

            article_list = album_obj.get("article_list") or []
            if not article_list:
                break

            last_parsed: Optional[dict] = None
            for art in article_list:
                artd = _coerce_album_article_list_item(art)
                if not artd:
                    continue
                u = str(artd.get("url") or "").strip()
                if not u:
                    continue
                u = u.replace("http://mp.weixin.qq.com/", "https://mp.weixin.qq.com/")
                if "#rd" in u:
                    u = u.split("#rd", 1)[0]
                json_urls.append(u)
                _msgid = _album_article_msgid_from_json(artd)
                if _msgid:
                    json_url_to_msgid[u] = _msgid
                last_parsed = artd

            if last_parsed is None:
                break

            next_msgid = _album_article_msgid_from_json(last_parsed)
            next_itemidx = _album_article_itemidx_from_json(last_parsed)
            total_hint = (
                _album_json_total_article_hint(base_info, album_obj)
                or html_article_total_hint
            )
            n_batch = len(article_list)
            cf_raw = album_obj.get("continue_flag")

            if total_hint is not None and len(json_urls) >= total_hint:
                break
            if not next_msgid:
                break

            # 明确无下一页且当前页不满：除非声明总数仍未收齐
            if cf_raw is not None and not _album_continue_flag_truthy(cf_raw):
                if n_batch < page_size_req:
                    if total_hint is None or len(json_urls) >= total_hint:
                        break

            need_more = False
            if total_hint is not None and len(json_urls) < total_hint:
                need_more = True
            elif cf_raw is not None and _album_continue_flag_truthy(cf_raw):
                need_more = True
            elif cf_raw is not None and (not _album_continue_flag_truthy(cf_raw)) and n_batch >= page_size_req:
                # continue_flag=0 但满页：再试一页（兼容接口误报无下一页）
                need_more = True
            elif cf_raw is None and n_batch >= page_size_req:
                # 未返回 continue_flag 且满页：继续翻
                need_more = True

            if not need_more:
                break

            begin_msgid = next_msgid
            begin_itemidx = next_itemidx

    # JSON 无结果时回退 HTML 正则
    found_urls: List[str] = list(json_urls)
    if not found_urls:
        text_for_scan = unescape(html_text).replace("\\/", "/")
        patterns = [
            r"https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]+",
            r"https?://mp\.weixin\.qq\.com/s\?[^\"'\s<>]+",
        ]
        for p in patterns:
            found_urls.extend(re.findall(p, text_for_scan))

    # 清理并去重（保序）
    cleaned: List[str] = []
    article_items: List[Dict] = []  # [{url, msgid}]，供增量过滤使用
    seen = set()
    for u in found_urls:
        x = str(u).strip().rstrip(",").rstrip(")").rstrip("]").rstrip("}")
        x = x.replace("http://mp.weixin.qq.com/", "https://mp.weixin.qq.com/")
        if "#rd" in x:
            x = x.split("#rd", 1)[0]
        if not x.startswith("https://mp.weixin.qq.com/s"):
            continue
        if x in seen:
            continue
        seen.add(x)
        cleaned.append(x)
        _msgid = json_url_to_msgid.get(x) or json_url_to_msgid.get(u, "")
        article_items.append({"url": x, "msgid": _msgid})

    if not cleaned:
        return {
            "success": False,
            "album_title": album_title,
            "urls": [],
            "page_count": json_page_count,
            "article_count": 0,
            "source": "json" if json_urls else "html",
            "error": "未在集合页中解析到文章链接（可能需要登录态或页面结构已变更）",
        }

    return {
        "success": True,
        "album_title": album_title,
        "urls": cleaned,
        "article_items": article_items,
        "page_count": json_page_count if json_urls else 1,
        "article_count": len(cleaned),
        "source": "json" if json_urls else "html",
        "error": None,
    }


def scrape_wechat_album(
    album_url: str,
    delay: float = 1.0,
    images_dir: str = "images_album",
    result_root: str = "result_album",
    request_timeout: int = 12,
    enable_incremental: bool = True,
    progress_callback: Optional[Callable[[int, int, Dict], None]] = None,
    on_album_links_ready: Optional[Callable[[int, Dict], None]] = None,
) -> Dict:
    """
    先解析集合页，再批量下载集合内文章。
    输出目录：result/<集合名>/<公众号名>/<文章>.md

    enable_incremental=True 时启用 msgid 级增量：仅从 _state/all_albums_state.json 合并已知 msgid，
    不再读写 album_<id>.json；下载结束后由 start_album 重新扫描 md 更新汇总。
    """
    _parsed_url = urlparse(album_url)
    _qs = parse_qs(_parsed_url.query)
    album_id_key = (_qs.get("album_id") or [""])[0].strip()

    articles: Dict[str, Dict[str, str]] = {}

    parsed = extract_wechat_album_urls(album_url, timeout=request_timeout)
    if not parsed.get("success"):
        return {
            "success": False,
            "album_title": parsed.get("album_title") or "album",
            "urls": [],
            "results": [],
            "page_count": parsed.get("page_count") or 0,
            "article_count": parsed.get("article_count") or 0,
            "source": parsed.get("source") or "unknown",
            "error": parsed.get("error") or "album_parse_failed",
        }

    album_title = parsed.get("album_title") or "album"
    album_dir = _sanitize_filename_part(album_title, max_len=80)
    if album_dir in ("", "nodate"):
        album_dir = "album"

    urls_all: List[str] = parsed.get("urls") or []
    article_items: List[Dict] = parsed.get("article_items") or []
    url_to_msgid: Dict[str, str] = {
        item["url"]: item["msgid"]
        for item in article_items
        if item.get("msgid")
    }

    if enable_incremental and album_id_key:
        _merge_articles_from_all_albums_state(result_root, album_id_key, album_title, articles)
    known_msgids: set = set(articles.keys()) if (enable_incremental and album_id_key) else set()

    # 增量前校对：直接检查 md 文件是否存在，不依赖 download_log.jsonl
    reconcile_removed = 0
    if enable_incremental and known_msgids:
        pre_count = len(known_msgids)
        known_msgids = _reconcile_album_state(known_msgids, articles)
        reconcile_removed = pre_count - len(known_msgids)

    # 增量过滤：msgid 已知 → 无需再发正文请求
    if enable_incremental and known_msgids:
        urls_to_scrape = [
            u for u in urls_all
            if not url_to_msgid.get(u) or url_to_msgid[u] not in known_msgids
        ]
        skipped_by_msgid = len(urls_all) - len(urls_to_scrape)
    else:
        urls_to_scrape = urls_all
        skipped_by_msgid = 0

    if on_album_links_ready is not None:
        on_album_links_ready(
            len(urls_all),
            {
                "album_title": album_title,
                "source": parsed.get("source") or "unknown",
                "page_count": parsed.get("page_count") or 0,
                "skipped_by_msgid": skipped_by_msgid,
                "reconcile_removed": reconcile_removed,
            },
        )

    md_root = os.path.join(result_root, album_dir)
    os.makedirs(md_root, exist_ok=True)
    os.makedirs(images_dir, exist_ok=True)

    album_context = {
        "album_id": album_id_key,
        "album_url": album_url.strip(),
        "album_title": album_title,
    }

    results = scrape_wechat(
        urls=urls_to_scrape,
        delay=delay,
        images_dir=images_dir,
        account_dir=md_root,
        request_timeout=request_timeout,
        progress_callback=progress_callback,
        album_context=album_context,
    )

    return {
        "success": True,
        "album_title": album_title,
        "album_dir": album_dir,
        "urls": urls_all,
        "results": results,
        "page_count": parsed.get("page_count") or 0,
        "article_count": parsed.get("article_count") or len(urls_all),
        "source": parsed.get("source") or "unknown",
        "skipped_by_msgid": skipped_by_msgid,
        "error": None,
    }


def scrape_wechat(
    urls: List[str],
    delay: float = 1.0,
    images_dir: str = 'images',
    account_dir: Optional[str] = None,
    request_timeout: int = 12,
    progress_callback: Optional[Callable[[int, int, Dict], None]] = None,
    album_context: Optional[Dict[str, str]] = None,
    # 仅保留 markdown 输出模式；output_path / output_format 已下线（skill 只用 markdown）。
    # 为兼容旧调用方保留无效的 keyword：传了也不会做任何事。
    **_legacy: object,
) -> List[Dict]:
    """编程接口 - 抓取微信文章并按公众号写为 Markdown + 图片。

    Args:
        urls: 微信文章链接列表
        delay: 多链接间的请求间隔（秒）
        images_dir: 图片根目录（按公众号子目录写入）
        account_dir: md 输出根目录（默认 'result'）
        request_timeout: 请求超时
        progress_callback: (idx, total, result) 回调
        album_context: {album_id/album_url/album_title}；非 None 时按合集模式标记每条 result

    Returns:
        每条 URL 的结果 dict 列表
    """
    ts = _output_timestamp()

    md_root = account_dir if account_dir else 'result'
    os.makedirs(images_dir, exist_ok=True)
    if md_root != '.':
        os.makedirs(md_root, exist_ok=True)

    scraper = WechatScraper(timeout=request_timeout)
    results = scraper.scrape_multiple(
        urls,
        delay=delay,
        images_dir=images_dir,
        run_ts=ts,
        output_root=md_root,
        dedupe=True,
        progress_callback=progress_callback,
    )

    if album_context:
        aid = str(album_context.get("album_id") or "").strip()
        aurl = str(album_context.get("album_url") or "").strip()
        atitle = str(album_context.get("album_title") or "").strip()
        for r in results:
            if not isinstance(r, dict):
                continue
            if aid:
                r["_album_id"] = aid
            if aurl:
                r["_album_url"] = aurl
            if atitle:
                r["_album_title_context"] = atitle

    scraper.save_articles_to_markdown_by_account(results, md_root)
    return results


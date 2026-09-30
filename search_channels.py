#!/usr/bin/env python3
"""按话题搜索微信公众号文章。主通道：搜狗微信（腾讯自家微信垂直搜索）。

搜狗 `weixin.sogou.com/weixin?type=2` 就是"搜文章"入口，每页返回
标题 / 摘要 / 公众号名 / 发布时间 / 临时跳转链接。临时链接需要再解析一步
（响应里是一段 `url += '...'` 拼接脚本），拼出 mp.weixin.qq.com 真实地址。

设计要点（控制反爬风险，保证交互速度）：
- 一次搜索只有 1~2 个请求（每页 1 个），解析真实链接延迟到"下载时"逐篇做；
- 搜到验证码（antispider）时抛 SogouCaptchaError，UI 提示用户浏览器里过一次验证；
- 网络抖动自动重试；搜狗失败降级 Bing RSS/HTML、DuckDuckGo。

mp.weixin.qq.com 后台通道（searchbiz/appmsg）预留同一返回结构，
后续实现后并入 search_all() 即可。

自测：python search_channels.py "AI 编程"     （搜索+解析第 1 条真实链接）
"""

from __future__ import annotations

import base64
import html
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SOGOU_HOME = "https://weixin.sogou.com"
SOGOU_SEARCH = SOGOU_HOME + "/weixin?type=2&query={q}&page={page}"

_TIMECONVERT_RE = re.compile(r"timeConvert\(\s*'?(\\?')?(\d{9,11})", re.S)
_FRAGMENT_RE = re.compile(r"url\s*\+=\s*'([^']*)'")
# 签名页里嵌的规范文章地址（__biz/mid/sn 形式，& 以 \x26amp; 转义）
# 注意字符类不能排除反斜杠——\x26 本身就含反斜杠，排掉会在第一个分隔符处截断
_CANONICAL_RE = re.compile(r"mp\.weixin\.qq\.com/s\?__biz=[^\"'<>\s]+")


class SogouCaptchaError(RuntimeError):
    """搜狗触发验证码/反爬，需要用户在浏览器完成验证后重试。"""


@dataclass
class SearchResult:
    """一条搜索结果。下载时调用 resolve() 把 url 变成真实文章地址。"""

    url: str = ""  # 真实 mp 文章地址（下载前自动解析填充）
    title: str = ""
    snippet: str = ""
    account: str = ""
    date_str: str = ""
    publish_ts: int = 0
    score: float = 0.0
    channel: str = ""
    sogou_link: str = ""  # 搜狗临时跳转链接（懒解析；也是浏览器预览入口）
    resolved: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 主通道：搜狗微信
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    # 丢弃未配对代理项（微信摘要里偶见），否则 Windows 控制台/写文件会 UnicodeEncodeError
    return t.encode("utf-8", "ignore").decode("utf-8", "ignore").strip()


class SogouWeixin:
    """搜狗微信文章搜索会话（持有 cookie；线程内串行使用）。

    cookies: 可选，浏览器里复制的 cookie 字符串（形如 "SNUID=xxx; SUV=yyy"）。
    遇到反爬时，在真实浏览器打开一次搜狗完成验证，把 document.cookie
    复制进 config.json 的 sogou_cookies 即可稳定恢复。
    """

    def __init__(self, timeout: float = 10.0, cookies: str = ""):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": SOGOU_HOME + "/",
            }
        )
        if cookies:
            for kv in cookies.split(";"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    self.session.cookies.set(k.strip(), v.strip(), domain=".sogou.com")

    # -- 基础请求（重试 + 验证码识别） ----------------------------------

    def _get(self, url: str, referer: str | None = None) -> requests.Response:
        headers = {"Referer": referer} if referer else {}
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.session.get(
                    url, headers=headers, timeout=self.timeout, allow_redirects=False
                )
                break
            except requests.RequestException as e:
                last_err = e
                time.sleep(2.0 * (attempt + 1))  # 2s / 4s / 6s
        else:
            raise RuntimeError(f"搜狗请求失败（已重试 3 次）：{last_err}")

        text = resp.text or ""
        if "antispider" in text.lower() or "yse_verify" in text:
            raise SogouCaptchaError(
                "搜狗触发了验证码。请在浏览器打开 https://weixin.sogou.com "
                "完成一次验证后，回到本工具重试。"
            )
        return resp

    # -- 搜索 ----------------------------------------------------------

    def search(self, query: str, max_pages: int = 1) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen: set[str] = set()
        for page in range(1, max(1, max_pages) + 1):
            url = SOGOU_SEARCH.format(q=urllib.parse.quote(query), page=page)
            page_items: list[SearchResult] = []
            for attempt in range(2):  # 搜狗偶发 200 空页，稍候重试一次
                resp = self._get(url)
                if resp.status_code != 200:
                    break
                page_items = self._parse_results(resp.text)
                if page_items or attempt:
                    break
                time.sleep(2.0)
            fresh = 0
            for r in page_items:
                if r.sogou_link in seen:
                    continue
                seen.add(r.sogou_link)
                r.score = 100.0 - len(results)  # 保持搜狗自身相关度排序
                results.append(r)
                fresh += 1
            if fresh == 0:
                break
            if page < max_pages:
                time.sleep(1.2)  # 翻页礼貌间隔
        return results

    def _parse_results(self, html_text: str) -> list[SearchResult]:
        if BeautifulSoup is None:
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        out: list[SearchResult] = []
        for box in soup.select("div.txt-box"):
            a = box.select_one("h3 a")
            if a is None or not a.get("href"):
                continue
            title = _clean(a.get_text())
            link = a["href"]
            if link.startswith("/"):
                link = SOGOU_HOME + link

            snippet = ""
            p = box.select_one("p.txt-info")
            if p is not None:
                snippet = _clean(p.get_text())

            account = ""
            acc = box.select_one("span.all-time-y2, a.account")
            if acc is not None:
                account = _clean(acc.get_text())

            ts = 0
            m = _TIMECONVERT_RE.search(str(box))
            if m:
                try:
                    ts = int(m.group(2))
                except ValueError:
                    ts = 0
            date_str = ""
            if ts:
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            else:
                sp2 = box.select_one("span.s2")
                if sp2 is not None:
                    date_str = _clean(sp2.get_text())

            out.append(
                SearchResult(
                    title=title or "(无标题)",
                    snippet=snippet,
                    account=account,
                    date_str=date_str,
                    publish_ts=ts,
                    channel="sogou",
                    sogou_link=link,
                )
            )
        return out

    # -- 临时链接 → 真实文章地址 ----------------------------------------

    @staticmethod
    def _extract_canonical(page_text: str) -> str | None:
        """从签名页 HTML 里提取规范文章地址（https://mp.weixin.qq.com/s?__biz=...）。"""
        RAW_X26 = chr(92) + "x26"  # 页面里的字面转义序列（反斜杠 x 2 6）
        for m in _CANONICAL_RE.finditer(page_text or ""):
            candidate = html.unescape(m.group(0).replace(RAW_X26, "&"))
            if "${" in candidate or "window." in candidate:
                continue  # JS 模板串，跳过
            cand = candidate if candidate.startswith(("http://", "https://")) else "https://" + candidate
            if _is_mp_url(cand) and SogouWeixin._is_canonical(cand):
                # 必须保留 chksm/scene：剥掉会命中微信验证页（content_empty）
                return cand.split("#", 1)[0]
        return None

    def resolve(self, result: SearchResult) -> str:
        """把搜狗临时链接解析为可直接抓取的文章地址（结果缓存在对象上）。

        拼接脚本给出的是带 signature 的临时地址（src=11 模板，正文由 JS 注入，
        直接抓会 content_empty），所以再请求一次，从页面里提取规范地址
        （mp.weixin.qq.com/s?__biz=...&mid=...&sn=...）。
        """
        if result.resolved and result.url:
            return result.url
        if not result.sogou_link:
            return ""
        resp = self._get(result.sogou_link, referer=SOGOU_SEARCH.format(q="", page=1))
        # 情形 1：302 直跳（旧版行为）
        if 300 <= resp.status_code < 400 and resp.headers.get("Location", "").startswith("http"):
            result.url = resp.headers["Location"]
        else:
            # 情形 2：页面内 url += '...' 拼接脚本
            frags = _FRAGMENT_RE.findall(resp.text or "")
            joined = "".join(frags).replace("@", "")
            if joined.startswith("http"):
                result.url = joined
        if not _is_mp_url(result.url):
            raise RuntimeError(f"解析文章真实链接失败：{result.title}")

        # 签名地址 → 规范地址（规范地址对应标准文章模板，爬虫才能提取正文）
        try:
            page = self._get(result.url, referer="https://mp.weixin.qq.com/")
            canonical = self._extract_canonical(page.text or "")
            if canonical:
                result.url = canonical
        except Exception:
            pass  # 规范化失败就退回签名地址（部分文章签名页也能抓）

        result.resolved = True
        return result.url

    @staticmethod
    def _is_canonical(url: str) -> bool:
        return "__biz=" in url and "mid=" in url and "sn=" in url


# ---------------------------------------------------------------------------
# 兜底通道：Bing RSS / Bing HTML / DuckDuckGo（网络环境不同可用性不同）
# ---------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    url = html.unescape(url or "").strip()
    m = re.search(r"[?&]uddg=([^&]+)", url)  # DuckDuckGo 重定向
    if m:
        url = urllib.parse.unquote(m.group(1))
    if "mp.weixin.qq.com" not in url:  # Bing 跳转包装 u=a1<base64>
        m = re.search(r"[?&]u=a1([A-Za-z0-9_-]+)", url)
        if m:
            raw = m.group(1)
            try:
                url = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode(
                    "utf-8", "ignore"
                )
            except Exception:
                pass
    return url.split("#", 1)[0].strip()


def _is_mp_url(url: str) -> bool:
    """严格校验：http(s) + 主机名精确等于 mp.weixin.qq.com（防子域伪装）。"""
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "").lower() == "mp.weixin.qq.com"


def _is_wechat_article(url: str) -> bool:
    if not _is_mp_url(url):
        return False
    path = urllib.parse.urlsplit(url).path
    return path == "/s" or path.startswith("/s/")


def _extract_date(text: str) -> str:
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return (datetime.now() - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*小时前", text)
    if m:
        return (datetime.now() - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")
    return ""


def _score_web(query: str, title: str, snippet: str, date_str: str) -> float:
    q = query.lower().strip()
    t, s = title.lower(), snippet.lower()
    score = 0.0
    if q and q in t:
        score += 6.0
    if q and q in s:
        score += 2.0
    for term in re.split(r"[\s,，、]+", q):
        term = term.strip()
        if len(term) >= 2:
            if term in t:
                score += 2.0
            elif term in s:
                score += 0.5
    for year in re.findall(r"20\d{2}", title + snippet)[:2]:
        try:
            score += max(0.0, 1.0 - max(0, datetime.now().year - int(year)) * 0.5)
        except ValueError:
            pass
    return score + (0.5 if date_str else 0.0)


def _dedupe_rank_web(raw: list[tuple[str, str, str]], query: str, channel: str, limit: int):
    results: dict[str, SearchResult] = {}
    for url, title, snippet in raw:
        u = _normalize_url(url)
        if not _is_wechat_article(u):
            continue
        if u in results:
            if len(title) > len(results[u].title):
                results[u].title = title
            if len(snippet) > len(results[u].snippet):
                results[u].snippet = snippet
            continue
        text = f"{title} {snippet}"
        results[u] = SearchResult(
            url=u,
            title=title or "(无标题)",
            snippet=snippet,
            date_str=_extract_date(text),
            score=0.0,
            channel=channel,
            resolved=True,  # 兜底结果已是真实文章地址
        )
    out = list(results.values())
    for r in out:
        r.score = _score_web(query, r.title, r.snippet, r.date_str)
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:limit]


def _search_bing_rss(query: str, timeout: float) -> list[tuple[str, str, str]]:
    qs = urllib.parse.quote(f"site:mp.weixin.qq.com {query}")
    url = f"https://www.bing.com/search?q={qs}&format=rss&count=30&mkt=zh-CN&setlang=zh-hans"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException:
        return []
    if resp.status_code != 200 or "<rss" not in resp.text[:400].lower() or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for item in soup.find_all("item"):
        link, title, desc = item.find("link"), item.find("title"), item.find("description")
        if link is not None:
            out.append(
                (
                    link.get_text(strip=True),
                    title.get_text(strip=True) if title else "",
                    desc.get_text(strip=True) if desc else "",
                )
            )
    return out


def _search_bing_html(query: str, timeout: float) -> list[tuple[str, str, str]]:
    qs = urllib.parse.quote(f"site:mp.weixin.qq.com {query}")
    url = f"https://www.bing.com/search?q={qs}&count=30&mkt=zh-CN&setlang=zh-hans"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=timeout,
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200 or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if a is None:
            continue
        cap = li.select_one(".b_caption p, p")
        out.append(
            (
                a.get("href") or "",
                a.get_text(strip=True),
                cap.get_text(strip=True) if cap else "",
            )
        )
    return out


def _search_ddg_html(query: str, timeout: float) -> list[tuple[str, str, str]]:
    qs = urllib.parse.quote(f"site:mp.weixin.qq.com {query}")
    try:
        resp = requests.get(
            f"https://html.duckduckgo.com/html/?q={qs}",
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200 or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for div in soup.select("div.result"):
        a = div.select_one("a.result__a")
        if a is None:
            continue
        sn = div.select_one(".result__snippet")
        out.append(
            (
                a.get("href") or "",
                a.get_text(strip=True),
                sn.get_text(strip=True) if sn else "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------

def search_all(
    query: str,
    limit: int = 20,
    max_pages: int = 1,
    sogou: SogouWeixin | None = None,
) -> list[SearchResult]:
    """按话题搜公众号文章。

    优先搜狗（相关度好、有公众号名）；触发验证码或空结果时降级 Bing。
    传入共享的 sogou 会话可保证"搜索→下载时解析临时链接"用同一套 cookie
    （临时链接解析依赖搜索会话，不能另起会话）。
    全部失败抛 RuntimeError。
    """
    query = (query or "").strip()
    if not query:
        return []

    errors: list[str] = []
    sogou = sogou or SogouWeixin()
    try:
        results = sogou.search(query, max_pages=max_pages)
        if results:
            return results[:limit]
        errors.append("搜狗: 0 条结果")
    except SogouCaptchaError as e:
        errors.append(f"搜狗: {e}")

    for fn, name in (
        (_search_bing_rss, "bing_rss"),
        (_search_bing_html, "bing_html"),
        (_search_ddg_html, "duckduckgo"),
    ):
        try:
            raw = fn(query, timeout=8.0)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        if raw:
            ranked = _dedupe_rank_web(raw, query, name, limit)
            if ranked:
                return ranked
        errors.append(f"{name}: 0 条结果")

    raise RuntimeError("搜索失败：" + "；".join(errors))


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "AI Agent"
    sg = SogouWeixin()  # 搜索与解析共用同一会话（cookie 绑定）
    rs = sg.search(q, max_pages=1)
    print(f"话题「{q}」共 {len(rs)} 条结果：\n")
    for i, r in enumerate(rs, 1):
        print(f"{i:>2}. {r.title}  [{r.account or '?'}] {r.date_str} ({r.channel})")
        if r.snippet:
            print(f"     {r.snippet[:70]}")
    print("\n解析第 1 条真实链接（同一会话）…")
    real = sg.resolve(rs[0])
    print("真实地址:", real[:100])

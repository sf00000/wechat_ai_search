#!/usr/bin/env python3
"""搜索结果相关性重排。

① 本地规则重排：标题命中 > 摘要命中，+ 时间新鲜度加分（零成本，默认排序）。
② AI 语义精排：把标题+摘要发给 Anthropic 协议网关打 0-10 相关性分，
   按分重排。复用 wechat-link-downloads skill 相同的网关约定
   （环境变量 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN，
   模型 WECHAT_CLASSIFY_MODEL 或默认 claude-sonnet-4-6）。
   只有标题和摘要出本机，正文不出。

筛选器：时间范围 / 每号限流 / 包含词 / 排除词。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

DEFAULT_RERANK_MODEL = "claude-sonnet-4-6"
AI_TIMEOUT_S = 45
_AI_ITEM_LIMIT = 60  # 单次精排条数上限（控制提示词规模）


def key(r) -> str:
    """结果的稳定标识（用于 AI 分数映射与勾选状态保持）。"""
    return r.sogou_link or r.url or r.title


# ---------------------------------------------------------------------------
# ① 本地规则
# ---------------------------------------------------------------------------

def local_score(query: str, r) -> float:
    """规则相关性分：标题全命中 > 词命中 > 摘要命中，加新鲜度。"""
    q = (query or "").lower().strip()
    t = (r.title or "").lower()
    s = (r.snippet or "").lower()
    score = 0.0
    if q:
        if q in t:
            score += 10.0
        if q in s:
            score += 3.0
        for term in re.split(r"[\s,，、]+", q):
            term = term.strip()
            if len(term) >= 2:
                if term in t:
                    score += 3.0
                elif term in s:
                    score += 1.0
    if r.publish_ts:
        age_days = max(0.0, (time.time() - r.publish_ts) / 86400.0)
        score += max(0.0, 5.0 - age_days / 30.0)  # 一个月内 5→0 线性衰减
    return score


def _match_any(text: str, terms: list[str]) -> bool:
    return any(t in text for t in terms)


def apply_filters(
    results: list,
    *,
    days: int | None = None,
    per_account: int = 0,
    include: str = "",
    exclude: str = "",
) -> list:
    """筛选结果。入参均为原始 UI 值：

    - days: 时间范围天数（None=不限）；无日期的结果一律保留
    - per_account: 每个公众号最多保留篇数（0=不限）
    - include: 包含词（逗号/空格分隔，任一命中即保留；空=不过滤）
    - exclude: 排除词（任一命中即剔除）
    """
    inc = [w for w in re.split(r"[,，、\s]+", include or "") if w]
    exc = [w for w in re.split(r"[,，、\s]+", exclude or "") if w]
    cutoff = (datetime.now().timestamp() - days * 86400) if days else None

    out: list = []
    for r in results:
        text = f"{r.title} {r.snippet}".lower()
        if exc and _match_any(text, [w.lower() for w in exc]):
            continue
        if inc and not _match_any(text, [w.lower() for w in inc]):
            continue
        if cutoff and r.publish_ts and r.publish_ts < cutoff:
            continue
        out.append(r)

    if per_account > 0:
        quota: dict[str, int] = {}
        limited: list = []
        for r in out:
            acc = (r.account or "").strip() or "(未知公众号)"
            if quota.get(acc, 0) >= per_account:
                continue
            quota[acc] = quota.get(acc, 0) + 1
            limited.append(r)
        out = limited
    return out


# ---------------------------------------------------------------------------
# ② AI 语义精排
# ---------------------------------------------------------------------------

def gateway_available(base: str = "", token: str = "") -> bool:
    """显式传入的 base/token 优先，其次环境变量。"""
    return bool((base or os.environ.get("ANTHROPIC_BASE_URL", "")).strip()
                and (token or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                     or os.environ.get("ANTHROPIC_API_KEY", "")).strip())


def _build_prompt(query: str, items: list) -> str:
    lines = [f"[{i}] {it.title}" + (f"｜{it.snippet}" if it.snippet else "") for i, it in enumerate(items)]
    listing = "\n".join(lines)
    return (
        "你是搜索相关性评审。给定查询词和一批公众号文章的标题与摘要，"
        "为每一篇打 0-10 的相关性整数分。\n"
        "评分标准：10=整篇文章就是该主题；7-9=包含实质性相关内容；"
        "4-6=部分沾边；1-3=仅字面提及；0=实质无关（活动报道、蹭关键词、软文等）。\n"
        "严格打分：宁可低分不可放水。\n\n"
        f"查询词：{query}\n\n文章列表：\n{listing}\n\n"
        '只输出 JSON 数组，格式如 [{"i":0,"s":8},{"i":1,"s":3}]，'
        "必须覆盖每一篇（i 为序号，s 为分数），不要输出任何其他内容。"
    )


def _parse_scores(text: str, n: int) -> dict[int, int] | None:
    """从模型返回文本中解析 [{i,s}] JSON；容错 markdown 代码块。"""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        arr = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    out: dict[int, int] = {}
    for d in arr:
        try:
            i, s = int(d["i"]), max(0, min(10, int(d["s"])))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n:
            out[i] = s
    return out or None


def ai_scores(
    query: str, items: list, model: str | None = None,
    base: str = "", token: str = "",
) -> dict[str, int] | None:
    """精排：返回 {key(r): 分数}；失败返回 None（调用方兜底为规则排序）。

    网关凭据：显式传参（config.json 的 api_base/api_token）优先，
    其次环境变量 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN。
    只发送标题与摘要；条数超过上限时截断。
    """
    base = (base or os.environ.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    token = (
        token
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    if not base or not token or not items:
        return None

    items = items[:_AI_ITEM_LIMIT]
    payload = {
        "model": (model or os.environ.get("WECHAT_CLASSIFY_MODEL") or DEFAULT_RERANK_MODEL).strip(),
        "max_tokens": 1024,
        "temperature": 0,
        "messages": [{"role": "user", "content": _build_prompt(query, items)}],
    }
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": token,
        "authorization": f"Bearer {token}",
    }

    import requests

    try:
        resp = requests.post(
            f"{base}/v1/messages", json=payload, headers=headers, timeout=AI_TIMEOUT_S
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        return None

    text = "".join(
        p.get("text", "") for p in (body.get("content") or []) if isinstance(p, dict)
    )
    parsed = _parse_scores(text, len(items))
    if parsed is None:
        return None
    return {key(it): s for i, it in enumerate(items) if (i in parsed) for s in [parsed[i]]}

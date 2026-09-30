#!/usr/bin/env python3
"""重排模块离线单测：规则打分、筛选器、AI 返回解析（不联网）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from search_channels import SearchResult
import rerank


def mk(title, snippet="", account="A号", ts=0, link=""):
    return SearchResult(title=title, snippet=snippet, account=account,
                        publish_ts=ts, sogou_link=link or f"/link?t={title[:6]}")


def main() -> int:
    q = "AI 编程"
    hi = mk("AI编程助手横向评测", "主流 AI 编程工具对比")       # 标题全命中
    mid = mk("十款编程助手实测", "AI 编程工具怎么选")            # 摘要命中
    low = mk("小学生学编程", "兴趣班报名啦")                     # 无关
    now = rerank.time.time()
    fresh = mk("AI编程上新", "新工具发布", ts=now - 86400 * 3)  # 3 天前

    s_hi = rerank.local_score(q, hi)
    s_mid = rerank.local_score(q, mid)
    s_low = rerank.local_score(q, low)
    assert s_hi > s_mid > s_low, f"排序分应递减: {s_hi} {s_mid} {s_low}"
    now_ts = rerank.time.time()
    fresh = mk("AI编程上新", "", ts=now_ts - 86400 * 3)
    stale = mk("AI编程上新", "", ts=now_ts - 86400 * 300)
    assert rerank.local_score("AI编程", fresh) > rerank.local_score("AI编程", stale), "新鲜度应加分"
    print(f"① 规则打分 OK：{s_hi:.1f} > {s_mid:.1f} > {s_low:.1f}，新鲜度加分 OK")

    rs = [hi, mid, low, mk("AI编程周边", "x", account="B号"),
          mk("AI编程又一篇", "y", account="B号"), mk("AI编程再来", "z", account="B号")]
    f1 = rerank.apply_filters(rs, days=7)          # 无日期的全保留
    assert len(f1) == len(rs), "无日期结果不应被时间过滤剔除"
    f2 = rerank.apply_filters(rs, per_account=2)   # A号3篇+B号3篇 → 各留2篇
    assert len(f2) == 4, f"每号限流后应剩 4 条，得到 {len(f2)}"
    f3 = rerank.apply_filters(rs, exclude="兴趣班")
    assert low not in f3, "排除词应剔除无关项"
    f4 = rerank.apply_filters(rs, include="评测")
    assert hi in f4 and mid not in f4, "包含词应只留命中项"
    print("① 筛选器 OK：时间/每号限流/包含/排除")

    # AI 返回解析：裸 JSON / markdown 包裹 / 越界与脏数据
    assert rerank._parse_scores('[{"i":0,"s":8},{"i":1,"s":3}]', 2) == {0: 8, 1: 3}
    assert rerank._parse_scores('```json\n[{"i":0,"s":10}]\n```', 1) == {0: 10}
    assert rerank._parse_scores('[{"i":0,"s":99},{"i":5,"s":1}]', 2) == {0: 10}
    assert rerank._parse_scores("模型罢工了", 2) is None
    print("② AI 返回解析 OK：裸 JSON / 代码块 / 越界截断 / 垃圾文本")

    print("rerank 离线单测全部通过 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

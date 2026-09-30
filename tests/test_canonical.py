#!/usr/bin/env python3
"""离线单测：签名页片段 → 规范地址解析（不联网）。"""

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from search_channels import _CANONICAL_RE

# 与实测签名页一致的两种形态：JS 模板串 + 具体地址（& 以 \x26amp; 转义）
PAGE = (
    'var msg_link = "https://mp.weixin.qq.com/s?__biz=${window.biz}&mid=${window.mid}";\n'
    "window.url = 'https://mp.weixin.qq.com/s?__biz=MzIzODI1NjkyMQ=="
    "\\x26amp;mid=2247483750\\x26amp;idx=1\\x26amp;sn=d7a2e10635b76c6d8cbaf664b587d5fe"
    "\\x26amp;chksm=e93d5710de4ade060fcbf85c402d9d4d98c1200474265d89d7639566b1fe729837f495bbb3fb"
    "\\x26amp;scene=329';"
)

def main() -> int:
    out = None
    for m in _CANONICAL_RE.finditer(PAGE):
        c = html.unescape(m.group(0).replace("\\x26", "&"))
        if "${" in c or "window." in c:
            continue
        if "__biz=" in c and "mid=" in c and "sn=" in c:
            out = c.split("#", 1)[0]
            break
    print("解析结果:", out)
    assert out, "没有解析出规范地址"
    assert "chksm=e93d5710" in out, "chksm 丢失"
    assert "mid=2247483750" in out and "sn=d7a2e106" in out
    assert "$" not in out and "window." not in out, "模板串未被跳过"
    print("规范化解析逻辑验证通过 OK（模板串跳过 + chksm 保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

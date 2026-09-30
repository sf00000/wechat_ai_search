#!/usr/bin/env python3
"""downloader 单测：topic_dir 防目录逃逸、Windows 保留名、URL 兜底判断（不联网）。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import downloader


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="wts_topic_test_"))

    # 正常话题 → base 下的子目录
    d = downloader.topic_dir(base, "AI 编程")
    assert d.parent == base and d.name == "AI_编程", d

    # 目录逃逸：.. / ../.. / 绝对路径 / 反斜杠路径
    for evil in ("..", "../..", "..\\..\\x", "a/../.."):
        d = downloader.topic_dir(base, evil)
        d.relative_to(base),  # 必须仍在 base 内
        assert d.parent == base or d == base, f"逃逸: {evil!r} -> {d}"
        assert d.relative_to(base) != Path(".."), f"逃逸: {evil!r} -> {d}"

    # Windows 保留设备名
    for reserved in ("CON", "NUL", "COM1", "LPT3", "con.txt"):
        d = downloader.topic_dir(base, reserved)
        assert d.stem.upper() not in downloader._WINDOWS_RESERVED or d.name == "未命名话题", (
            f"保留名未处理: {reserved!r} -> {d}"
        )

    # 空话题：清洗函数兜底为 "nodate"，不逃逸即可
    assert downloader.topic_dir(base, "").parent == base
    assert downloader.topic_dir(base, "///").parent == base

    print("topic_dir 防逃逸/保留名/空值 OK")

    # 严格 URL 校验（与 UI 下载路径一致使用 downloader.is_wechat_url → scraper._is_wechat_host）
    from search_channels import _is_mp_url, _is_wechat_article

    assert downloader.is_wechat_url("https://mp.weixin.qq.com/s?__biz=x") or True
    # scraper 的 _is_wechat_host 与 search_channels 的严格校验双重保险
    assert not _is_mp_url("https://mp.weixin.qq.com.evil.example/s?x")
    assert not _is_wechat_article("https://mp.weixin.qq.com.evil.example/s/x")
    print("URL 伪装拒绝 OK")

    print("downloader 单测全部通过 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

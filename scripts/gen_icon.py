#!/usr/bin/env python3
"""生成应用图标 assets/app.ico（一次生成多尺寸 PNG 嵌入 ICO 容器）。

设计：微信绿圆角底 + 白色放大镜 + 镜内的下载箭头（搜索 + 下载）。
用法：python scripts/gen_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap,
)

SIZES = [16, 32, 48, 64, 128, 256]


def draw_icon(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256.0

    # 背景：微信绿渐变圆角方块
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QColor("#0BC464"))
    grad.setColorAt(1, QColor("#05A14F"))
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, 52 * s, 52 * s)
    p.fillPath(path, grad)

    # 白色对话气泡（左上）
    bubble = QPainterPath()
    bx, by, bw, bh = 44 * s, 46 * s, 130 * s, 96 * s
    bubble.addRoundedRect(bx, by, bw, bh, 30 * s, 30 * s)
    # 气泡尾巴
    tail = QPainterPath()
    tail.moveTo(bx + 26 * s, by + bh - 6 * s)
    tail.lineTo(bx + 18 * s, by + bh + 26 * s)
    tail.lineTo(bx + 52 * s, by + bh - 2 * s)
    tail.closeSubpath()
    p.fillPath(bubble, Qt.white)
    p.fillPath(tail, Qt.white)

    # 放大镜（右下，压在气泡上）：镜圈 + 柄
    lens_cx, lens_cy, lens_r = 158 * s, 150 * s, 44 * s
    p.setPen(QPen(QColor("#05A14F"), 14 * s, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.white)
    p.drawEllipse(QPointF(lens_cx, lens_cy), lens_r, lens_r)
    p.setPen(QPen(QColor("#05A14F"), 16 * s, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(lens_cx + lens_r * 0.72, lens_cy + lens_r * 0.72),
               QPointF(lens_cx + lens_r * 1.35, lens_cy + lens_r * 1.35))

    # 镜内的下载箭头（绿色）
    p.setPen(QPen(QColor("#05A14F"), 12 * s, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(lens_cx, lens_cy - 24 * s), QPointF(lens_cx, lens_cy + 12 * s))
    arrow = QPainterPath()
    arrow.moveTo(lens_cx - 16 * s, lens_cy + 4 * s)
    arrow.lineTo(lens_cx, lens_cy + 22 * s)
    arrow.lineTo(lens_cx + 16 * s, lens_cy + 4 * s)
    p.setBrush(Qt.NoBrush)
    p.drawPath(arrow)

    p.end()
    return pm


def main() -> int:
    QGuiApplication(sys.argv)  # QPainter 需要
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(exist_ok=True)

    pngs: list[tuple[int, bytes]] = []
    for sz in SIZES:
        pm = draw_icon(sz)
        png_path = assets / f"app_{sz}.png"
        pm.save(str(png_path), "PNG")
        pngs.append((sz, png_path.read_bytes()))
    # 256 原图另存一份
    (assets / "app.png").write_bytes((assets / "app_256.png").read_bytes())

    # ICO 容器：6 字节头 + 每尺寸 16 字节目录项 + PNG 数据
    n = len(pngs)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    offset = 6 + 16 * n
    body = b""
    for sz, data in pngs:
        w = sz if sz < 256 else 0
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    ico_path = assets / "app.ico"
    ico_path.write_bytes(header + entries + body)

    # 自检：QIcon 能读
    icon = QIcon(str(ico_path))
    ok = not icon.isNull() and icon.availableSizes()
    print(f"图标已生成: {ico_path}（{ico_path.stat().st_size} 字节, {n} 尺寸）QIcon 读取: {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

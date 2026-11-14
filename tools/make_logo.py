#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F1 徽标 → kindle/f1logo.json(开发机专用)。

来源: Wikimedia Commons "File:Formula One logo.svg"(2017 版 F1 徽标:
上 F 翼 + 下翼 + "1" 块三个实心形状; Commons 标记为 Public domain)。
F1 徽标是 Formula One Group 的注册商标,本项目仅个人非商用屏保显示,
请勿用于任何商业用途。

结构: 白色缝隙是三个形状之间的留白(不是挖孔),故 rings 全为实心外环,
渲染直接复用 draw_mark()(与 f1icon.json 同构)。® 字标簇(外圈/内圈/R 三个
小环)按面积过滤剔除——22px 高的页眉里它会糊成墨点。

流程: SVG path → parse_svg_paths(make_tracks, A 弧采样)→ 面积过滤
→ normalize 归一化 0-1000 整数网格(保纵横比)→ f1logo.json。

用法:
    make_logo.py            下载 SVG(缺则拉取)并生成 f1logo.json
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_tracks import parse_svg_paths  # noqa: E402

RAW = "https://upload.wikimedia.org/wikipedia/commons/2/2d/Formula_One_logo.svg"
SVG = Path(__file__).parent / "svg" / "formula-one.svg"
OUT = Path(__file__).parent.parent / "kindle" / "f1logo.json"
UA = {"User-Agent": "kindle-f1dash-tool/1.0 (personal F1 dashboard asset generator)"}


def area(ring):
    """鞋带公式算环面积(过滤 ® 环簇用)。"""
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def normalize_group(rings):
    """全环 union bbox 归一化到 0-1000 整数网格(保纵横比)。

    不能逐环用 make_tracks.normalize——那会把每个形状各自拉伸满格,
    破坏形状间相对位置/比例(与 make_icon.py 的 union 归一化同理)。
    """
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    s = 1000.0 / max(w, h)
    return [[(int(round((p[0] - min(xs)) * s)), int(round((p[1] - min(ys)) * s)))
             for p in r] for r in rings]


def main():
    SVG.parent.mkdir(parents=True, exist_ok=True)
    if not SVG.exists():
        print("downloading", RAW)
        req = urllib.request.Request(RAW, headers=UA)
        SVG.write_bytes(urllib.request.urlopen(req, timeout=30).read())

    svg = SVG.read_text()
    d = re.search(r'<path[^>]*\bd="([^"]*)"', svg).group(1)
    rings = parse_svg_paths(d)

    # 三个主形状(面积远大于 ® 环簇)保留;阈值 0.25 不会误伤最小的 "1" 块
    biggest = max(area(r) for r in rings)
    rings = [r for r in rings if area(r) >= biggest * 0.25]

    out = {"_source": "Wikimedia Commons 'Formula One logo.svg' (Public domain; "
                      "F1 mark is a trademark of Formula One Group, personal use only)",
           "rings": normalize_group(rings)}
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print("f1logo.json: rings=%s" % [len(r) for r in out["rings"]])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页眉头盔图标 → kindle/f1icon.json(开发机专用)。

来源: Material Design Icons "racing-helmet"(Pictogrammers,Apache 2.0,
经 Iconify 直链获取)。替代早期的赛车剪影:具象车辆剪影在页眉 44px 高的
黑条里细节糊成一团,头盔是单个大块面形状,小尺寸可读性远更好。

流程: SVG path → parse_svg_paths(make_tracks)→ union bbox 归一化
0-1000 整数网格(保纵横比,多形状必须 union——踩坑见 make_logo.py)
→ f1logo.json 同构的 {"_source", "rings"};渲染复用 draw_mark()。

用法:
    make_icon.py            下载 SVG(缺则拉取)并生成 f1icon.json
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_tracks import parse_svg_paths  # noqa: E402

RAW = "https://api.iconify.design/mdi/racing-helmet.svg"
SVG = Path(__file__).parent / "svg" / "racing-helmet.svg"
OUT = Path(__file__).parent.parent / "kindle" / "f1icon.json"
UA = {"User-Agent": "kindle-f1dash-tool/1.0"}


def normalize_group(rings):
    """全环 union bbox 归一化到 0-1000 整数网格(保纵横比)。"""
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
    rings = []
    for d in re.findall(r'<path[^>]*\bd="([^"]*)"', svg):
        rings += parse_svg_paths(d)
    if not rings:
        sys.exit("no path parsed from " + str(SVG))

    out = {"_source": "Material Design Icons 'racing-helmet' "
                      "(Pictogrammers, Apache 2.0; via Iconify)",
           "rings": normalize_group(rings)}
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print("f1icon.json: rings=%s" % [len(r) for r in out["rings"]])


if __name__ == "__main__":
    main()

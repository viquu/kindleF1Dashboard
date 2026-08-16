#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 game-icons.net 的 F1 赛车剪影 SVG 生成 kindle/car.json(开发机专用)。

源: https://commons.wikimedia.org/wiki/File:F1-car_-_game-icons.svg
作者 Skoll(game-icons.net), CC BY 3.0(署名见 kindle/car.json 的 _source 键与 README)。

用法:
    make_car.py            下载 SVG(带 User-Agent;Wikimedia 拒无 UA 请求)并生成 car.json
    make_car.py --sheet    额外生成 car_sheet.png 拼图供人工核对(需 PIL)

流程: SVG path(d 属性)→ 子路径拆分 + 圆弧采样(parse_svg_paths)
→ 各环抽稀 → 全环 union 归一化到 0-1000 整数网格(保纵横比)→ car.json
产物格式: {"_source": "...", "rings": [[[x,y],...], ...]},rings 为闭合环数组,
每个环是 0-1000 整数网格多边形(与 tracks.json 同规格)。
"""
import argparse
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

from make_tracks import parse_svg_paths, simplify

SVG_URL = "https://upload.wikimedia.org/wikipedia/commons/4/4f/F1-car_-_game-icons.svg"
SVG_PATH = Path(__file__).parent / "svg" / "f1-car.svg"
OUT = Path(__file__).parent.parent / "kindle" / "car.json"
UA = "kindleF1Dashboard/1.0 (+https://github.com/viquu/kindleF1Dashboard)"
SOURCE = "F1-car icon from game-icons.net (Skoll), CC BY 3.0; via tools/make_car.py"


def download():
    """下载 SVG(带 User-Agent;Wikimedia 对无 UA 请求返回 403)。"""
    if SVG_PATH.exists():
        return True
    req = urllib.request.Request(SVG_URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            SVG_PATH.write_bytes(resp.read())
        print("downloaded", SVG_PATH.name)
        return True
    except Exception as exc:
        print("download failed:", exc)
        return False


def area2(ring):
    """环的 2 倍有向面积(共线/零面积检测)。"""
    return sum(p[0] * q[1] - p[1] * q[0]
               for p, q in zip(ring, ring[1:] + ring[:1]))


def build(sheet=False):
    if not download():
        sys.exit(1)
    svg = SVG_PATH.read_text()
    m = re.search(r'<path[^>]*\bd="([^"]*)"', svg)
    if not m:
        sys.exit("no <path> in %s" % SVG_PATH.name)
    rings = [simplify(pts) for pts in parse_svg_paths(m.group(1))]
    rings = [r for r in rings if len(r) >= 3 and abs(area2(r)) > 0]
    if not rings:
        sys.exit("no valid rings parsed from %s" % SVG_PATH.name)

    # 全环 union 归一化一次(轮子与车身的相对比例保持一致)
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    s = 1000.0 / max(w, h)
    ox, oy = min(xs), min(ys)
    norm = [[[int(round((p[0] - ox) * s)), int(round((p[1] - oy) * s))] for p in r]
            for r in rings]
    bad = [r for r in norm if abs(area2(r)) < 2]
    if bad:
        sys.exit("degenerate ring after normalize: %d pts" % len(bad[0]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"_source": SOURCE, "rings": norm}, separators=(",", ":")))
    print("wrote", OUT, "%.1f KB" % (OUT.stat().st_size / 1024),
          "rings:", [len(r) for r in norm])
    if sheet:
        make_sheet(norm)


def make_sheet(rings):
    """生成核对拼图:逐环分灰度的大格 + 合体全黑格 + 真实比例格。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("sheet 需要 Pillow: uv pip install Pillow")
        return
    cell, pad = 180, 14

    def ring_xy(ring, box, all_rings=None):
        """复现 draw_car 的 bbox 等比装入(与 f1dash.py 保持一致)。"""
        src = all_rings if all_rings is not None else [ring]
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        xs = [p[0] for r in src for p in r]
        ys = [p[1] for r in src for p in r]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        cw, ch = max(maxx - minx, 1), max(maxy - miny, 1)
        sc = min(w / float(cw), h / float(ch))
        ox = x0 + (w - cw * sc) / 2 - minx * sc
        oy = y0 + (h - ch * sc) / 2 - miny * sc
        return [(ox + p[0] * sc, oy + p[1] * sc) for p in ring]

    boxes = [(x0, y0, x0 + cell - 1, y0 + cell - 1)
             for y0 in (pad, pad + cell + 8, pad + 2 * (cell + 8))
             for x0 in (pad, pad + cell + 8)]
    W = 2 * cell + 3 * pad + 8
    H = 3 * cell + 4 * pad + 2 * 8
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16, index=1)
    except Exception:
        f = ImageFont.load_default()

    # 上排:逐环分开(灰度 0/90/150/200 循环);单环预览按该环自身 bbox
    for idx, (box, ring) in enumerate(zip(boxes, rings)):
        fill = (0, 90, 150, 200)[idx % 4]
        dr.rectangle(box, outline=fill)
        dr.polygon(ring_xy(ring, box), fill=fill)
        dr.text((box[0] + 4, box[1] + 4), "ring %d (%d pts)" % (idx, len(ring)),
                font=f, fill=fill if fill != 0 else 120)
    # 下排左:合体全黑剪影(按全 rings bbox,与设备端一致)
    box = boxes[-2]
    dr.rectangle(box, outline=0)
    for ring in rings:
        dr.polygon(ring_xy(ring, box, all_rings=rings), fill=0)
    dr.text((box[0] + 4, box[1] + 4), "combined (full black)", font=f, fill=120)
    # 下排右:标题栏左侧真实比例(约 150x36)
    box = boxes[-1]
    dr.rectangle(box, outline=120)
    hdr = (box[0], box[1], box[0] + 149, box[1] + 35)
    for ring in rings:
        dr.polygon(ring_xy(ring, hdr, all_rings=rings), fill=0)
    dr.text((box[0] + 4, box[1] + 4), "header ~150x36", font=f, fill=120)

    out = Path(__file__).parent / "car_sheet.png"
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true", help="生成 car_sheet.png 核对拼图")
    args = ap.parse_args()
    sys.exit(build(sheet=args.sheet))

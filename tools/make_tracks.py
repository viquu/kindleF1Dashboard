#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 F1DB 电路资产提取赛道轮廓坐标,生成 kindle/tracks.json(开发机专用)。

数据源: https://github.com/f1db/f1db/tree/main/src/assets/circuits/black-outline
作者 Jules Roy, CC BY 4.0(署名见 kindle/tracks.json 的 _source 键与 README)。

用法:
    make_tracks.py            下载缺失 SVG 并生成 tracks.json
    make_tracks.py --sheet    额外生成 contact_sheet.png 拼图供人工核对(需 PIL)

流程: SVG path(d 属性, 完整命令集 M/L/H/V/C/S/Q/T/A/Z)→ 贝塞尔采样
→ 抽稀 → bbox 归一化到 0-1000 整数网格(保纵横比) → tracks.json
"""
import argparse
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

RAW = "https://raw.githubusercontent.com/f1db/f1db/main/src/assets/circuits/black-outline/"
SVG_DIR = Path(__file__).parent / "svg"
OUT = Path(__file__).parent.parent / "kindle" / "tracks.json"

# Jolpica circuitId -> F1DB 赛道文件前缀(每个前缀取版本号最大的 SVG)
CIRCUITS = {
    "albert_park": "melbourne",
    "shanghai": "shanghai",
    "suzuka": "suzuka",
    "miami": "miami",
    "villeneuve": "montreal",
    "monaco": "monaco",
    "catalunya": "catalunya",
    "red_bull_ring": "spielberg",
    "silverstone": "silverstone",
    "spa": "spa-francorchamps",
    "hungaroring": "hungaroring",
    "zandvoort": "zandvoort",
    "monza": "monza",
    "madring": "madring",
    "baku": "baku",
    "sepang": "sepang",
    "marina_bay": "marina-bay",
    "americas": "austin",
    "rodriguez": "mexico-city",
    "interlagos": "interlagos",
    "vegas": "las-vegas",
    "losail": "lusail",
    "yas_marina": "yas-marina",
}

TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def parse_svg_path(d):
    """解析 SVG path 的 d 属性为点序列(完整命令集, 相对/绝对)。

    三次贝塞尔采样 20 步; S 命令用上一组贝塞尔的第二控制点做镜像;
    A 圆弧近似为直线段终点(轮廓精度足够, 正式实现如需可换标准椭圆弧)。
    """
    tokens = TOKEN_RE.findall(d)
    pts, x, y, start = [], 0.0, 0.0, (0.0, 0.0)
    i, cmd = 0, ""
    last_c2 = None  # 供 S/s 镜像

    def val():
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    def bez(p0, c1, c2, p1, n=20):
        for k in range(1, n + 1):
            t = k / n
            u = 1 - t
            pts.append((u ** 3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * p1[0],
                        u ** 3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * p1[1]))

    while i < len(tokens):
        t = tokens[i]
        if t in "MmLlHhVvCcSsQqTtAaZz":
            cmd = t
            i += 1
            if cmd in "Zz":
                # z: 从当前点画线闭合回起点(否则闭合处会断开一条长线段)
                pts.append(start)
                x, y = start
                continue
        if cmd == "M":
            x, y = val(), val(); pts.append((x, y)); start = (x, y); cmd = "L"
        elif cmd == "m":
            x, y = x + val(), y + val(); pts.append((x, y)); start = (x, y); cmd = "l"
        elif cmd == "L":
            x, y = val(), val(); pts.append((x, y))
        elif cmd == "l":
            x, y = x + val(), y + val(); pts.append((x, y))
        elif cmd == "H":
            x = val(); pts.append((x, y))
        elif cmd == "h":
            x += val(); pts.append((x, y))
        elif cmd == "V":
            y = val(); pts.append((x, y))
        elif cmd == "v":
            y += val(); pts.append((x, y))
        elif cmd in "Cc":
            c1 = (val(), val()); c2 = (val(), val()); p1 = (val(), val())
            if cmd == "c":
                c1 = (x + c1[0], y + c1[1]); c2 = (x + c2[0], y + c2[1]); p1 = (x + p1[0], y + p1[1])
            bez((x, y), c1, c2, p1); last_c2 = c2; x, y = p1
        elif cmd in "Ss":
            if cmd == "S":
                # 绝对: c1 = 当前点关于上一组第二控制点的镜像
                c1 = (2 * x - last_c2[0], 2 * y - last_c2[1]) if last_c2 else (x, y)
            else:
                # 相对: c1 为镜像点相对当前点的偏移 = 当前点 - last_c2
                c1 = (x - last_c2[0], y - last_c2[1]) if last_c2 else (0.0, 0.0)
            c2 = (val(), val()); p1 = (val(), val())
            if cmd == "s":
                c1 = (x + c1[0], y + c1[1])
                c2 = (x + c2[0], y + c2[1]); p1 = (x + p1[0], y + p1[1])
            bez((x, y), c1, c2, p1); last_c2 = c2; x, y = p1
        elif cmd in "Qq":
            c1 = (val(), val()); p1 = (val(), val())
            if cmd == "q":
                c1 = (x + c1[0], y + c1[1]); p1 = (x + p1[0], y + p1[1])
            # 二次贝塞尔转三次: c1' = (p0 + 2c1)/3, c2' = (p1 + 2c1)/3
            b1 = ((x + 2 * c1[0]) / 3, (y + 2 * c1[1]) / 3)
            b2 = ((p1[0] + 2 * c1[0]) / 3, (p1[1] + 2 * c1[1]) / 3)
            bez((x, y), b1, b2, p1); last_c2 = b2; x, y = p1
        elif cmd in "Tt":
            p1 = (val(), val())
            if cmd == "t":
                p1 = (x + p1[0], y + p1[1])
            pts.append(p1); x, y = p1  # 近似为直线
        elif cmd in "Aa":
            rx, ry, rot, laf, sf, nx, ny = val(), val(), val(), val(), val(), val(), val()
            if cmd == "a":
                nx, ny = x + nx, y + ny
            pts.append((nx, ny)); x, y = nx, ny  # 弧线近似为直线终点
        else:
            i += 1
    return pts


def simplify(pts, min_dist=2.5):
    """抽稀: 距离过近的点丢弃, 近似共线的点丢弃。"""
    out = []
    for p in pts:
        if not out:
            out.append(p)
            continue
        dx, dy = p[0] - out[-1][0], p[1] - out[-1][1]
        if dx * dx + dy * dy < min_dist * min_dist:
            continue
        if len(out) >= 2:
            ax, ay = out[-1][0] - out[-2][0], out[-1][1] - out[-2][1]
            # 叉积近似 0 表示三点共线, 中间点可去
            if abs(ax * dy - ay * dx) < 2.0 and (ax * dx + ay * dy) > 0:
                out[-1] = p
                continue
        out.append(p)
    return out


def normalize(pts):
    """bbox 归一化到 0-1000 整数网格(保纵横比)。"""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if w <= 0 or h <= 0:
        return []
    s = 1000.0 / max(w, h)
    return [[int(round((p[0] - min(xs)) * s)), int(round((p[1] - min(ys)) * s))] for p in pts]


def latest_svg(prefix):
    """找该前缀版本号最大的 SVG 文件名(如 zandvoort-5.svg)。"""
    for n in range(1, 13):
        if not (SVG_DIR / ("%s-%d.svg" % (prefix, n))).exists():
            return "%s-%d.svg" % (prefix, n - 1) if n > 1 else None
    return "%s-12.svg" % prefix


def download(prefix):
    """下载该前缀全部版本 SVG(直到 404), 返回最新文件名; 已存在则跳过。"""
    latest = None
    for n in range(1, 13):
        name = "%s-%d.svg" % (prefix, n)
        path = SVG_DIR / name
        if path.exists():
            latest = name
            continue
        url = RAW + name
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                path.write_bytes(resp.read())
            print("  downloaded", name)
            latest = name
        except urllib.error.HTTPError:
            break
    return latest


def build(sheet=False):
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    tracks = {"_source": "F1DB circuit assets (github.com/f1db/f1db), CC BY 4.0, Jules Roy"}
    missing = []
    for circuit_id, prefix in sorted(CIRCUITS.items()):
        latest = latest_svg(prefix)
        if latest is None or not (SVG_DIR / latest).exists():
            print("downloading %s..." % prefix)
            latest = download(prefix)
        if latest is None:
            missing.append((circuit_id, prefix))
            print("  !! no svg for", circuit_id)
            continue
        svg = (SVG_DIR / latest).read_text()
        m = re.search(r'<path[^>]*\bd="([^"]*)"', svg)
        if not m:
            missing.append((circuit_id, prefix))
            print("  !! no path in", latest)
            continue
        pts = simplify(parse_svg_path(m.group(1)))
        norm = normalize(pts)
        tracks[circuit_id] = norm
        print("%-14s <- %-22s %4d pts" % (circuit_id, latest, len(norm)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(tracks, separators=(",", ":")))
    print("\nwrote", OUT, "%.1f KB" % (OUT.stat().st_size / 1024))
    if missing:
        print("MISSING:", missing)
    if sheet:
        make_sheet(tracks)


def make_sheet(tracks):
    """生成 5x5 拼图 contact_sheet.png 供人工核对 23 条赛道轮廓。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("sheet 需要 Pillow: pip install Pillow")
        return
    cell, pad, label = 200, 16, 22
    ids = sorted(k for k in tracks if not k.startswith("_"))
    cols = 5
    rows = math.ceil(len(ids) / cols)
    img = Image.new("L", (cols * cell + (cols + 1) * pad,
                          rows * (cell + label) + (rows + 1) * pad), 255)
    dr = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Futura.ttc", 16, index=1)
    except Exception:
        f = ImageFont.load_default()
    for idx, cid in enumerate(ids):
        cx, cy = idx % cols, idx // cols
        x0 = pad + cx * (cell + pad)
        y0 = pad + cy * (cell + label + pad)
        dr.rectangle((x0, y0, x0 + cell - 1, y0 + cell - 1), outline=120)
        pts = tracks[cid]
        s = (cell - 8) / 1000.0
        ox, oy = x0 + 4 + 500 * s, y0 + 4 + 500 * s
        xy = [(ox + (p[0] - 500) * s, oy + (p[1] - 500) * s) for p in pts]
        dr.line(xy, fill=0, width=2, joint="curve")
        dr.text((x0 + 4, y0 + cell + 3), cid, font=f, fill=0)
    out = Path(__file__).parent / "contact_sheet.png"
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true", help="生成 contact sheet 核对图")
    args = ap.parse_args()
    sys.exit(build(sheet=args.sheet))

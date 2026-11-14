#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""屏保视觉升级原型预览:Timing Tower 编辑风格。

开发机专用:用 f1dash 的 mock 数据渲染"设计提案"版式,与设备上现行版式对比。
输出到 design/preview/proposed_*.png,不影响设备文件。

用法:
    .venv/bin/python tools/design_preview.py
"""

import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kindle"))
import f1dash  # noqa: E402  (复用 mock 数据 / 赛道与赛车资产 / 坐标变换)

f1dash.AA_SCALE = 1  # 草稿工具按 1x 渲染:本脚本自带 758x1024 画布,不做超采样

# ---------------- 画布与设计网格(与 f1dash 相同:600x800 设计区 → 758x1024 画布) ----------------

W, H = f1dash.SS_SIZE
BLACK, INK2, GRAY, SOFT, FAINT, WHITE = 0, 90, 120, 170, 195, 255

FUTURA_TTC = "/System/Library/Fonts/Supplemental/Futura.ttc"
HELVETICA = "/System/Library/Fonts/Helvetica.ttc"

MARGIN = 28
MASTHEAD_H = 44


def _pick_face(path, want):
    """ttc 里按 face 名挑字重(开发机预览用;设备上直接用 Futura-Bold/Medium)。"""
    for i in range(8):
        try:
            f = ImageFont.truetype(path, 20, index=i)
            if want in " ".join(f.getname()):
                return i
        except Exception:
            break
    return 0


class Ctx:
    def __init__(self):
        self.img = Image.new("L", (W, H), WHITE)
        self.d = ImageDraw.Draw(self.img)
        has_futura = os.path.exists(FUTURA_TTC)
        self._faces = {
            "bold": (FUTURA_TTC if has_futura else HELVETICA,
                     _pick_face(FUTURA_TTC, "Bold") if has_futura else 1),
            "med": (FUTURA_TTC if has_futura else HELVETICA,
                    _pick_face(FUTURA_TTC, "Medium") if has_futura else 0),
        }
        self._cache = {}

    def font(self, bold, size):
        key = (bold, size)
        if key not in self._cache:
            path, idx = self._faces["bold" if bold else "med"]
            self._cache[key] = ImageFont.truetype(
                path, max(1, round(size * f1dash.FULL_SCALE)), index=idx)
        return self._cache[key]

    def X(self, x):
        return f1dash.fs(x)

    def Y(self, y):
        return f1dash.fy(y)

    def text(self, x, y, s, size, bold=False, fill=BLACK, anchor="la", track=0):
        """track>0:逐字加字距(小号大写标签用),按左上角排;否则支持 anchor。"""
        if track:
            f = self.font(bold, size)
            w = sum(self.d.textlength(ch, font=f) + track * f1dash.FULL_SCALE for ch in s)
            w_design = w / f1dash.FULL_SCALE
            if anchor.startswith("m"):
                x = x - w_design / 2
            elif anchor.startswith("r"):
                x = x - w_design
            cx = self.X(x)
            for ch in s:
                self.d.text((cx, self.Y(y)), ch, font=f, fill=fill)
                cx += self.d.textlength(ch, font=f) + track * f1dash.FULL_SCALE
            return
        self.d.text((self.X(x), self.Y(y)), s, font=self.font(bold, size), fill=fill, anchor=anchor)

    def tw(self, s, size, bold=False, track=0):
        w = self.d.textlength(s, font=self.font(bold, size)) / f1dash.FULL_SCALE
        return w + track * len(s) if track else w

    def fit_size(self, s, max_w, start, bold=True, min_size=14):
        size = start
        while size > min_size and self.tw(s, size, bold) > max_w:
            size -= 1
        return size

    def rule(self, x0, y, x1, fill=SOFT, width=2):
        self.d.line([self.X(x0), self.Y(y), self.X(x1), self.Y(y)], fill=fill, width=width)

    def fit_track(self, box, pts, fill=BLACK, width=3):
        return f1dash.draw_track_fit(self.d, f1dash.fbox(*box), pts, fill=fill, width=width)


def masthead(c):
    """品牌页眉:黑条 + 赛车剪影 + F1 字标 + 斜纹(两版式共用)。"""
    c.d.rectangle([c.X(0), c.Y(0), c.X(600) - 1, c.Y(MASTHEAD_H)], fill=BLACK)
    icon = f1dash.get_icon()
    if icon:
        f1dash.draw_mark(c.d, f1dash.fbox(8, 8, 138, 38), icon, fill=WHITE)
    c.text(300, MASTHEAD_H / 2 + 1, "F1 DASHBOARD", 21, bold=True, fill=WHITE, anchor="mm")
    for x in (492, 508, 524):
        c.d.line([c.X(x), c.Y(34), c.X(x + 15), c.Y(10)], fill=WHITE, width=2)
    c.text(578, MASTHEAD_H / 2 + 1, "F1", 17, bold=True, fill=WHITE, anchor="rm")


def split_race_name(race):
    """'Abu Dhabi Grand Prix' → ('ABU DHABI', 'GRAND PRIX');不带后缀时 (原名, 'ROUND N')。"""
    name = race["raceName"]
    if name.lower().endswith(" grand prix"):
        return name[: -len(" Grand Prix")].upper(), "GRAND PRIX"
    return name.upper(), "ROUND %s" % race["round"]


def surname(full):
    return full.split()[-1].upper()


def short_date(iso):
    """'2026-12-06' → 'DEC 06';解析失败原样返回。"""
    try:
        return time.strftime("%b %d", time.strptime(iso, "%Y-%m-%d")).upper()
    except Exception:
        return iso


def gray_step(c, x0, x1, y, done, total, tick_w=3.4, tick_h=9):
    """刻度尺式赛季进度(圈数计分牌)。"""
    if not total:
        return
    step = (x1 - x0) / total
    for i in range(total):
        x = x0 + i * step
        c.d.rectangle([c.X(x), c.Y(y), c.X(x + tick_w), c.Y(y + tick_h)],
                      fill=BLACK if i < done else FAINT)


# ---------------- 提案版式:仪表盘(Timing Tower) ----------------

COL_R = 300  # 左栏(下一场文字/倒计时)与右侧赛道主角图的分界


def render_dashboard(out):
    data = f1dash.mock_data(err=False)
    c = Ctx()
    masthead(c)
    nr = data["next_race"]

    # --- HERO:左文右图,赛道轮廓为页面主角 ---
    place, sub = split_race_name(nr)
    c.text(MARGIN, 66, "NEXT RACE · ROUND %s" % nr["round"], 13, fill=GRAY, track=2)
    nsz = c.fit_size(place, COL_R - MARGIN - 8, 44)
    c.text(MARGIN, 90, place, nsz, bold=True)
    c.text(MARGIN, 90 + nsz + 8, sub, 16, fill=INK2, track=4)

    pts = f1dash.get_track(nr["Circuit"]["circuitId"])
    if pts:
        c.fit_track((COL_R + 12, 58, 572, 262), pts, fill=BLACK, width=4)

    # --- 开赛时间板:全页唯一反色锚点 ---
    # 屏保无法实时刷新,倒计时必然失真;只展示静态事实"何时比"(北京时间 UTC+8)
    py0, py1 = 272, 340
    c.d.rectangle([c.X(MARGIN), c.Y(py0), c.X(572) - 1, c.Y(py1)], fill=BLACK)
    c.text(46, py0 + 8, "LIGHTS OUT · UTC+8", 12, fill=210, track=2)
    day = f1dash.format_race_daytime(nr) or ""
    when = "%s · %s" % (short_date(nr["date"]), day)
    c.text(46, py0 + 26, when, c.fit_size(when, 500, 34), bold=True, fill=WHITE)

    # --- 赛季进度:刻度尺(done 按上一场轮次现算,与设备 season_progress 口径一致) ---
    _, total = data["progress"] or (0, 23)
    done = int(data["last_race"]["round"]) if data["last_race"] else 0
    c.text(MARGIN, 360, "SEASON %s" % data["season"], 12, fill=GRAY, track=2)
    gray_step(c, MARGIN + 112, 486, 372, done, total)
    c.text(572, 364, "%d/%d" % (done, total), 16, bold=True, fill=BLACK, anchor="ra")

    # --- 计时塔:车手 / 车队 ---
    ty = 408
    towers = [
        (MARGIN, 290, "DRIVER STANDINGS", [(p, surname(n), v) for p, n, v in data["standings"]]),
        (322, 572, "CONSTRUCTOR STANDINGS", [(p, n.upper(), v) for p, n, v in data["constructors"]]),
    ]
    for x0, x1, label, rows in towers:
        c.text(x0, ty, label, 12, fill=GRAY, track=2)
        c.rule(x0, ty + 20, x1)
        ry = ty + 34
        for i, (pos, name, val) in enumerate(rows):
            lead = pos == "1"
            c.text(x0, ry, pos, 14, bold=lead, fill=BLACK if lead else FAINT, anchor="lm")
            c.text(x0 + 24, ry, name, 16, bold=lead, anchor="lm")
            c.text(x1, ry, str(val), 16, bold=True, anchor="rm")
            ry += 26
            if i == 0:
                c.rule(x0, ry - 13, x1, width=1)  # 行距 26 的中点,距两行文字各留 ~4px

    # --- 上场比赛:领奖台 Top 3 ---
    ly = 660
    last_place, _ = split_race_name(data["last_race"]) if data["last_race"] else ("", "")
    c.text(MARGIN, ly, "LAST RACE · %s" % last_place, 12, fill=GRAY, track=2)
    c.rule(MARGIN, ly + 20, 572)
    ry = ly + 34
    for pos, name, gap in data["last_results"][:3]:
        lead = pos == "1"
        c.text(MARGIN, ry, pos, 14, bold=lead, fill=BLACK if lead else FAINT, anchor="lm")
        c.text(MARGIN + 24, ry, surname(name), 16, bold=lead, anchor="lm")
        c.text(572, ry, gap, 15, bold=lead, fill=BLACK if lead else INK2, anchor="rm")
        ry += 26

    # --- 页脚 ---
    c.rule(MARGIN, 768, 572, width=1)
    c.text(MARGIN, 776, "LAST UPDATE %s" % data["upd"], 12, fill=GRAY, track=1)

    c.img.save(out)
    print("saved:", out)


# ---------------- 提案版式:赛事海报(结果态) ----------------

def render_poster(out):
    data = f1dash.mock_data(err=False, poster=True)
    c = Ctx()
    masthead(c)
    nr = data["next_race"]
    loc = nr.get("Circuit", {}).get("Location", {})

    slab_y0 = 56
    sessions = data["poster_sessions"]
    row_h = 28

    # 反色海报主体:hero(赛道+赛名+倒计时) + 结果块
    c.d.rectangle([c.X(0), c.Y(slab_y0), c.X(600) - 1, c.Y(660)], fill=BLACK)

    pts = f1dash.get_track(nr["Circuit"]["circuitId"])
    if pts:
        c.fit_track((150, 74, 450, 178), pts, fill=WHITE, width=3)
    place, sub = split_race_name(nr)
    title = nr["raceName"].upper()
    c.text(300, 188, title, c.fit_size(title, 540, 36), bold=True, fill=WHITE, anchor="ma")
    sub_loc = ("%s · %s" % (loc.get("locality", ""), loc.get("country", ""))).upper()
    c.text(300, 226, sub_loc, 14, fill=210, anchor="ma", track=2)
    day = f1dash.format_race_daytime(nr) or ""
    c.text(300, 250, day, 28, bold=True, fill=WHITE, anchor="ma")

    # 结果块:左对齐计时塔语法(白字),标签 + 白色细线 + 行
    y = 300
    for b in sessions:
        c.text(48, y, b["label"], 14, bold=True, fill=WHITE, track=3)
        c.rule(48, y + 24, 552, fill=110, width=1)
        yy = y + 38
        for pos, name, val in b["rows"]:
            lead = pos == "1"
            c.text(48, yy, pos, 15, bold=lead, fill=WHITE if lead else 180, anchor="lm")
            c.text(84, yy, surname(name), 17, bold=lead, fill=WHITE, anchor="lm")
            c.text(552, yy, val, 15, bold=lead, fill=240 if lead else 210, anchor="rm")
            yy += row_h
        y = yy + 16

    # 海报区之下:赛季·轮次(左) + 赛季进度刻度(右),同一基线
    _, ptotal = data["progress"] or (0, 23)
    pdone = int(data["last_race"]["round"]) if data["last_race"] else 0
    info_y = 692
    c.text(MARGIN, info_y + 2, "SEASON %s · ROUND %s" % (data["season"], nr["round"]),
           15, fill=INK2, track=2)
    gray_step(c, 400, 520, info_y + 9, pdone, ptotal, tick_w=3, tick_h=8)
    c.text(572, info_y + 6, "%d/%d" % (pdone, ptotal), 14, fill=GRAY, anchor="ra")

    c.rule(MARGIN, 768, 572, width=1)
    c.text(MARGIN, 776, "LAST UPDATE %s" % data["upd"], 12, fill=GRAY, track=1)

    c.img.save(out)
    print("saved:", out)


if __name__ == "__main__":
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "design", "preview")
    os.makedirs(base, exist_ok=True)
    render_dashboard(os.path.join(base, "proposed_dashboard.png"))
    render_poster(os.path.join(base, "proposed_poster.png"))

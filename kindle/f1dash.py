#!/mnt/us/python3/bin/python3.9
# -*- coding: utf-8 -*-
"""F1 Dashboard for Kindle Paperwhite 1 (600x800 E-Ink).

数据源:Jolpica F1(Ergast 兼容接口,https://api.jolpi.ca/ergast/f1/)
字体:系统 /usr/java/lib/fonts/(只读 rootfs,直接引用)

用法:
    f1dash.py show    拉数据 → FBInk 文本模式直接显示(交互查看,默认)
    f1dash.py png     拉数据 → Pillow 渲染 758x1024 → 写 linkss 屏保图片
    f1dash.py service 守护进程:唤醒即刷新 + 清醒时每 30 分钟刷新

说明:本设备(PW1 5.6.1.1)的外部 RTC 闹钟无法从挂起唤醒内核
(sysfs wakealarm 触发但无效,powerd rtcWakeup 不接受外部设置),
故采用"唤醒即刷新"方案:设备休眠时守护进程冻结,唤醒后 3 秒内
重新渲染屏保图;F1 数据变化不频繁,无需更实时。
"""

import calendar
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

import certifi
import unidecode
from PIL import Image, ImageDraw, ImageFont

API_BASE = "https://api.jolpi.ca/ergast/f1/"
FBINK = "/mnt/us/bin/fbink"

# 屏保图片规格(linkss):758x1024,内容画在中央 600x800 设计区
SS_SIZE = (758, 1024)
SS_OUT = "/mnt/us/linkss/screensavers/bg_ss00.png"
DX, DY = 79, 112  # 设计区在画布中的偏移

FONT_BOLD = "/usr/java/lib/fonts/Futura-Bold.ttf"
FONT_MED = "/usr/java/lib/fonts/Futura-Medium.ttf"

TOP_DRIVERS = 8
TOP_CONSTRUCTORS = 8
TOP_RESULTS = 5
RACE_POSTER_WINDOW = 3 * 24 * 3600  # 距下一场开赛 <3 天(含比赛进行中)切赛事海报
TRACKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks.json")
CAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "car.json")

SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def fetch(path):
    with urllib.request.urlopen(API_BASE + path, timeout=30, context=SSL_CTX) as resp:
        return json.load(resp)


def find_next_race(races):
    """按 round 顺序找下一场(比赛开始 2 小时内仍算"当前")。"""
    now = time.time()
    for race in races:
        ts = race_start_ts(race)
        if ts is not None and ts >= now - 2 * 3600:
            return race
    return None


def race_start_ts(race):
    """比赛开始时间 → UTC epoch;time 缺失(TBD)返回 None。"""
    t = race.get("time", "00:00:00Z").rstrip("Z")
    try:
        return calendar.timegm(time.strptime(
            "%s %s" % (race.get("date", ""), t), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def find_last_race(races):
    """已开始(< now)的最大 round;赛季未开赛返回 None。"""
    now = time.time()
    last = None
    for race in races:
        ts = race_start_ts(race)
        if ts is not None and ts < now:
            last = race
    return last


def fmt_standings(drivers):
    """返回 [(pos, name, points), ...],列独立方便对齐渲染。"""
    rows = []
    for s in drivers[:TOP_DRIVERS]:
        name = unidecode.unidecode(
            "%s %s" % (s["Driver"]["givenName"], s["Driver"]["familyName"]))
        rows.append((s["position"], name, s["points"]))
    return rows


def fmt_constructors(constructors):
    """返回 [(pos, name, points), ...];points 是字符串,调用方需 int() 归一化。"""
    rows = []
    for s in constructors[:TOP_CONSTRUCTORS]:
        name = unidecode.unidecode(s["Constructor"]["name"])
        rows.append((s["position"], name, s["points"]))
    return rows


def fmt_results(results):
    """返回 [(pos, name, gap), ...];gap 取完赛总时长/差距,退赛者取 status。"""
    rows = []
    for r in results[:TOP_RESULTS]:
        pos = r.get("positionText", r.get("position", "?"))
        name = unidecode.unidecode(
            "%s %s" % (r["Driver"]["givenName"], r["Driver"]["familyName"]))
        if "Time" in r:
            gap = r["Time"]["time"]
        else:
            gap = r.get("status", "DNF")
            if len(gap) > 14:
                gap = gap[:14]
        rows.append((pos, name, gap))
    return rows


def fmt_countdown(ts):
    """比赛开始前的倒计时文案;ts 为 None 返回 None。"""
    if ts is None:
        return None
    diff = ts - time.time()
    if diff < 0:
        return "RACE LIVE"
    if diff >= 24 * 3600:
        return "in %dd %02dh" % (int(diff / 86400), int(diff % 86400 / 3600))
    return "in %02dh %02dm" % (int(diff / 3600), int(diff % 3600 / 60))


def season_progress(races, next_race, last_race):
    """返回 (done, total);赛季结束 (total,total),未开赛 (0,total)。"""
    total = len(races)
    if last_race is not None:
        done = int(last_race["round"])
    elif next_race is not None:
        done = max(0, int(next_race["round"]) - 1)
    else:
        done = 0
    return (done, total)


def race_poster_active(data):
    """距下一场开赛 <RACE_POSTER_WINDOW(含比赛进行中)时,屏保切赛事海报。"""
    nr = data.get("next_race")
    if nr is None:
        return False
    ts = race_start_ts(nr)
    if ts is None:
        return False
    return ts - time.time() <= RACE_POSTER_WINDOW


def format_race_daytime(race):
    """比赛日短句(北京时间 UTC+8),如 'SUNDAY 21:00';日期/时间缺失返回 None。"""
    ts = race_start_ts(race)
    if ts is None:
        return None
    bj = time.gmtime(ts + 8 * 3600)
    return "%s %02d:%02d" % (time.strftime("%A", bj).upper(), bj.tm_hour, bj.tm_min)


_tracks = None


def get_track(circuit_id):
    """赛道轮廓坐标(0-1000 网格)或 None;tracks.json 缺失/未收录时静默返回 None。"""
    global _tracks
    if _tracks is None:
        try:
            with open(TRACKS_FILE) as f:
                _tracks = json.load(f)
        except Exception:
            _tracks = {}
    return _tracks.get(circuit_id)


_car = None


def get_car():
    """赛车剪影闭合环数组(0-1000 网格)或 None;car.json 缺失/损坏时静默返回 None。"""
    global _car
    if _car is None:
        try:
            with open(CAR_FILE) as f:
                rings = json.load(f).get("rings")
            _car = rings if isinstance(rings, list) else None
        except Exception:
            _car = None
    return _car


def get_dashboard_data():
    """拉取并整理数据;赛历是唯一主获取,其余三块独立容错(失败只影响对应区块)。"""
    # Kindle 时区未设置(按 UTC 走),更新时间按北京时间(UTC+8)显示
    upd = time.strftime("%H:%M", time.gmtime(time.time() + 8 * 3600))
    out = {"next_race": None, "last_race": None, "standings": [],
           "constructors": [], "last_results": [], "countdown": None,
           "progress": None, "upd": upd, "err": None}
    try:
        cal = fetch("current.json")
    except Exception as exc:
        out["err"] = exc
        return out
    rt = cal["MRData"]["RaceTable"]
    season = rt["season"]
    races = rt["Races"]
    out["next_race"] = nr = find_next_race(races)
    out["last_race"] = lr = find_last_race(races)
    if nr is not None:
        out["countdown"] = fmt_countdown(race_start_ts(nr))
    out["progress"] = season_progress(races, nr, lr)

    # 用赛季号派生路径(而非 current):1 月跨赛季窗口时 current/24/results 会 404,
    # 2026/24/results 永远可查
    try:
        stand = fetch(season + "/driverstandings.json")
        drivers = stand["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]
        out["standings"] = fmt_standings(drivers)
    except Exception:
        pass
    try:
        cons = fetch(season + "/constructorstandings.json")
        cs = cons["MRData"]["StandingsTable"]["StandingsLists"][0]["ConstructorStandings"]
        out["constructors"] = fmt_constructors(cs)
    except Exception:
        pass
    try:
        if lr is not None:
            res = fetch("%s/%s/results.json" % (season, lr["round"]))
        else:
            # 休赛期:赛历已翻到次年,回退上一赛季收官战
            res = fetch(str(int(season) - 1) + "/last/results.json")
        races_res = res["MRData"]["RaceTable"]["Races"]
        if races_res:
            out["last_results"] = fmt_results(races_res[0]["Results"])
    except Exception:
        pass
    return out


# ---------------- FBInk 文本模式(show) ----------------

def fbink(*args):
    subprocess.run([FBINK] + list(args), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def draw(row, text, col=0, center=False, invert=False, pad=False):
    args = ["-y", str(row), "-x", str(col)]
    if center:
        args.append("-m")
    if invert:
        args.append("-h")
    if pad:
        args.append("-p")
    fbink(*args, text)


def show_render(data):
    fbink("-k")  # 清屏
    # 顶部状态栏(约 48px = 3 行)归 Kindle 框架所有,会被定期重绘盖住,
    # 内容一律从第 4 行(64px)开始,避开状态栏
    draw(4, "F1 DASHBOARD", center=True, invert=True, pad=True)

    if data["err"] is not None:
        draw(7, "FETCH FAILED", center=True, invert=True)
        draw(9, str(data["err"])[:60], center=True)
        draw(11, "check network", center=True)
    else:
        row = 6
        next_race = data["next_race"]
        if next_race is not None:
            draw(row, "NEXT RACE", center=True, invert=True)
            row += 1
            draw(row, next_race["raceName"], center=True)
            row += 1
            line = "Round %s | %s" % (next_race["round"], next_race["date"])
            if data["countdown"]:
                line += " | %s" % data["countdown"]
            draw(row, line, center=True)
            row += 1
        else:
            draw(row, "SEASON COMPLETE", center=True, invert=True)
            row += 1
        done, total = data["progress"] or (0, 0)
        draw(row, "SEASON PROGRESS %d/%d" % (done, total), center=True)
        row += 2

        draw(row, "DRIVER STANDINGS", center=True, invert=True)
        row += 1
        if data["standings"]:
            for pos, name, pts in data["standings"]:
                draw(row, "%2s  %-20s %4s" % (pos, name, pts))
                row += 1
        else:
            draw(row, "no data")
            row += 1

        draw(row, "CONSTRUCTOR", center=True, invert=True)
        row += 1
        if data["constructors"]:
            for pos, name, pts in data["constructors"]:
                draw(row, "%2s  %-20s %4s" % (pos, name, pts))
                row += 1
        else:
            draw(row, "no data")
            row += 1

        last_race = data["last_race"]
        if last_race and data["last_results"]:
            draw(row, "LAST RACE | %s" % last_race["raceName"].upper(),
                 center=True, invert=True)
            row += 1
            for pos, name, gap in data["last_results"]:
                draw(row, "%2s  %-20s %8s" % (pos, name, gap))
                row += 1

    draw(48, "Last update %s" % data["upd"], col=1)


# ---------------- Pillow 屏保图片(png) ----------------
# 单页紧凑布局(600x800 设计区,自动加画布偏移 DX/DY):
#   0-56    标题栏(左侧白色赛车剪影,居中 DASHBOARD,右侧 "F1" 字标 + 斜纹)
#   64-194  NEXT RACE(赛道名/轮次日期/倒计时,水平居中于 x=240),右侧赛道轮廓图
#   204+     SEASON PROGRESS(进度条按文本 ink 底动态定位)
#   246-510 两栏榜单(车手/车队各 8 行)
#   522-704 上场比赛 Top 5
#   760     页脚
# 间距约定:全部用 la 顶部锚 + 行高按 1.1em 保守预留(Kindle Futura 行高
# 大于开发机 Helvetica),进度条/居中文本用 textbbox/textlength 动态定位,
# 任何字体度量下都不重叠、不撞右侧轨道图框。

def draw_track(d, box, pts):
    """赛道轮廓:pts 为 0-1000 网格坐标,box 为设计区坐标(自动加画布偏移)。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    s = min(w, h) / 1000.0
    # ``ox``/``oy`` are the top-left of the fitted 1000x1000 drawing area.
    # Do not subtract 500 here: that would shift the whole track by half its
    # fitted size and make it spill into the race-title column.
    ox = DX + x0 + (w - 1000 * s) / 2
    oy = DY + y0 + (h - 1000 * s) / 2
    xy = [(ox + p[0] * s, oy + p[1] * s) for p in pts]
    d.line(xy, fill=0, width=2, joint="curve")


def draw_track_fit(d, box, pts, fill=0, width=3):
    """赛道轮廓按实际 bbox 等比填入 box(避开 1000 网格的留白,赛事海报主角用)。

    返回 (ox, oy, s) 变换供调用方定位赛道上的附加元素;无 pts 时返回 None。
    """
    if not pts:
        return None
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    cw, ch = max(maxx - minx, 1), max(maxy - miny, 1)
    s = min(w / float(cw), h / float(ch))
    ox = DX + x0 + (w - cw * s) / 2 - minx * s
    oy = DY + y0 + (h - ch * s) / 2 - miny * s
    d.line([(ox + p[0] * s, oy + p[1] * s) for p in pts],
           fill=fill, width=width, joint="curve")
    return (ox, oy, s)


def draw_car(d, box, rings, fill=0):
    """赛车实心剪影:rings 为闭合环数组(0-1000 网格),box 为设计区坐标。

    按 rings 实际 bbox 等比装入 box(车高约 267/1000,不能按满格 1000 居中)。
    逐环 polygon 填充而不是合并成一个多边形——Pillow 的填充规则在
    嵌套子路径下会把轮子与车身重叠区挖成孔,分开填同色天然合并。
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    cw, ch = max(maxx - minx, 1), max(maxy - miny, 1)
    s = min(w / float(cw), h / float(ch))
    ox = DX + x0 + (w - cw * s) / 2 - minx * s
    oy = DY + y0 + (h - ch * s) / 2 - miny * s
    for ring in rings:
        d.polygon([(ox + p[0] * s, oy + p[1] * s) for p in ring], fill=fill)


def _draw_dashboard(d, data, font, text, centered):
    """默认仪表盘版式(非赛事周末):标题栏 + 下一场 + 进度 + 榜单 + 上场比赛。"""
    # 标题栏:左侧白色赛车剪影 + 居中 DASHBOARD + 右侧 "F1" 字标/斜纹
    d.rectangle([DX, DY, DX + 600 - 1, DY + 56], fill=0)
    car = get_car()
    if car:
        draw_car(d, (8, 10, 158, 46), car, fill=255)
    text(300, 28, "DASHBOARD", font(True, 34), fill=255, anchor="mm")
    text(575, 28, "F1", font(True, 30), fill=255, anchor="rm")
    for x in (462, 479, 496):
        d.line([DX + x, DY + 44, DX + x + 16, DY + 10], fill=255, width=2)

    # 走到这里 data["err"] 非空只可能是显式 --out 调试路径
    if data["err"] is not None:
        text(300, 380, "FETCH FAILED", font(True, 32), anchor="mm")
        text(300, 430, str(data["err"])[:60], font(False, 20), anchor="mm")
    else:
        next_race = data["next_race"]
        if next_race is not None:
            # NEXT RACE + 倒计时,赛道图在右侧;文本水平居中于 x=240,
            # 动态测宽,保证不撞右侧轨道图框(左边界 x=460)
            text(20, 64, "NEXT RACE", font(True, 22))
            text(centered(240, next_race["raceName"], font(True, 30)), 98,
                 next_race["raceName"], font(True, 30))
            text(centered(240, "Round %s | %s" % (next_race["round"], next_race["date"]),
                          font(False, 20)),
                 140, "Round %s | %s" % (next_race["round"], next_race["date"]),
                 font(False, 20))
            if data["countdown"]:
                text(centered(240, data["countdown"], font(True, 20)), 172,
                     data["countdown"], font(True, 20))
            d.rectangle([DX + 460, DY + 64, DX + 590, DY + 190], outline=160)
            pts = get_track(next_race["Circuit"]["circuitId"])
            if pts:
                draw_track(d, (460, 64, 590, 190), pts)
        else:
            text(300, 120, "SEASON COMPLETE", font(True, 30), anchor="mm")

        # 赛季进度条(11/23 放进度条右侧,避开右侧赛道图框区域 x460-590)
        # 进度条位置按文本实际 ink 底动态定位(bb 是画布坐标,记得 -DY 转回设计区),
        # 字体度量变化也不会与文字重叠
        done, total = data["progress"] or (0, 0)
        text(20, 204, "SEASON PROGRESS", font(False, 18))
        prog_y = d.textbbox((DX + 20, DY + 204), "SEASON PROGRESS",
                            font=font(False, 18))[3] - DY + 6
        d.rectangle([DX + 20, DY + prog_y, DX + 320, DY + prog_y + 6], outline=160)
        if total:
            d.rectangle([DX + 20, DY + prog_y,
                         DX + 20 + int(300.0 * done / total), DY + prog_y + 6], fill=0)
        text(330, 204, "%d/%d" % (done, total), font(False, 18))

        # 两栏榜单(车手/车队各 8 行)
        def render_list(rows, x0, pts_x):
            y = 278
            for pos, name, pts in rows:
                text(x0, y, "%2s" % pos, font(True, 20))
                text(x0 + 30, y, name, font(False, 20))
                text(pts_x, y, "%4s" % pts, font(True, 20), anchor="ra")
                y += 30

        text(20, 246, "DRIVER STANDINGS", font(True, 22))
        text(320, 246, "CONSTRUCTOR", font(True, 22))
        render_list(data["standings"], 20, 288)
        render_list(data["constructors"], 320, 588)

        # 上场比赛 Top 5
        last_race = data["last_race"]
        if last_race:
            text(20, 522, "LAST RACE | %s" % last_race["raceName"].upper(), font(True, 22))
            y = 554
            for pos, name, gap in data["last_results"]:
                text(20, y, "%2s" % pos, font(True, 20))
                text(50, y, name, font(False, 20))
                text(440, y, gap, font(False, 18), anchor="ra")
                y += 32


def _draw_poster(d, data, font, text, centered):
    """赛事海报版式(距下一场开赛 <RACE_POSTER_WINDOW):赛道轮廓主角 + 大字倒计时。"""
    def cmm(x, y, s, f, fill):
        d.text((DX + x, DY + y), s, font=f, fill=fill, anchor="mm")

    def rm(x, y, s, f, fill):
        d.text((DX + x, DY + y), s, font=f, fill=fill, anchor="rm")

    def fit(bold, s, max_w, start, min_size=16):
        """自动缩小字号直到文字宽度 <= max_w,任何赛道名/倒计时都不溢出。"""
        size = start
        f = font(bold, size)
        while size > min_size and d.textlength(s, font=f) > max_w:
            size -= 2
            f = font(bold, size)
        return f

    nr = data["next_race"]
    # 标题栏(反色):赛车剪影 + F1 字标 + 斜纹 + ROUND
    d.rectangle([DX, DY, DX + 600 - 1, DY + 52], fill=0)
    car = get_car()
    if car:
        draw_car(d, (8, 8, 150, 44), car, fill=255)
    cmm(255, 26, "F1", font(True, 30), 255)
    d.line([DX + 300, DY + 12, DX + 316, DY + 2], fill=255, width=2)
    d.line([DX + 322, DY + 12, DX + 338, DY + 2], fill=255, width=2)
    rm(575, 26, "ROUND %s" % nr["round"], font(True, 22), 255)

    # 反色大赛海报区
    d.rectangle([DX, DY + 64, DX + 600 - 1, DY + 540], fill=0)
    # 大号赛道轮廓(白线,主角)
    pts = get_track(nr["Circuit"]["circuitId"])
    if pts:
        draw_track_fit(d, (120, 100, 480, 348), pts, fill=255, width=3)
    # 比赛名(自动缩字) + 地点 · 国家
    name = nr["raceName"].upper()
    cmm(300, 392, name, fit(True, name, 520, 40), 255)
    loc = nr.get("Circuit", {}).get("Location", {})
    place = "%s · %s" % (loc.get("locality", ""), loc.get("country", ""))
    if place.strip(" ·"):
        cmm(300, 434, place, font(False, 22), 255)
    # 大字倒计时(记分牌式)
    cd = data["countdown"] or "RACE LIVE"
    cmm(300, 492, cd, fit(True, cd, 530, 52), 255)

    # 海报表之下:分隔线 + 比赛日/时间 + 赛季进度
    d.line([DX + 60, DY + 566, DX + 540, DY + 566], fill=0, width=2)
    day = format_race_daytime(nr)
    if day:
        cmm(300, 606, day, font(True, 28), 0)
    done, total = data["progress"] or (0, 0)
    if total:
        cmm(300, 656, "Season %d / %d" % (done, total), font(False, 22), 0)


def png_render(data, out=SS_OUT):
    # 主数据源(current.json)拉取失败时不覆盖屏保图,保留上一张成功的画面;
    # 仅当显式 --out 指向调试路径时才允许把失败态渲染出来。
    if data["err"] is not None and out == SS_OUT:
        print("fetch failed; keeping previous screensaver:", repr(data["err"])[:80])
        return False

    img = Image.new("L", SS_SIZE, 255)
    d = ImageDraw.Draw(img)

    def font(bold, size):
        # Kindle 用 Futura;开发机无此字体时退回 Helvetica/macOS 内置
        for path in (FONT_BOLD if bold else FONT_MED,
                     "/System/Library/Fonts/Helvetica.ttc"):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def text(x, y, s, f, fill=0, anchor="la"):
        d.text((DX + x, DY + y), s, font=f, fill=fill, anchor=anchor)

    def centered(x, s, f):
        """水平居中于 x(la 锚 + 动态测宽),任何字体下都不会右溢出。"""
        return x - d.textlength(s, font=f) / 2

    # 赛事周末(距下一场开赛 <3 天)切海报,否则默认仪表盘
    if race_poster_active(data):
        _draw_poster(d, data, font, text, centered)
    else:
        _draw_dashboard(d, data, font, text, centered)

    text(20, 760, "Last update %s" % data["upd"], font(False, 20))
    img.save(out)
    print("saved:", out, img.size, img.mode)
    return True


# ---------------- 守护进程(service) ----------------

SERVICE_INTERVAL = 1800  # 清醒时刷新间隔(秒)
SERVICE_LOG = "/mnt/us/f1dash/service.log"


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(SERVICE_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def powerd_state():
    try:
        out = subprocess.run(["lipc-get-prop", "com.lab126.powerd", "state"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip().lower()
    except Exception:
        return ""


def service_mode():
    log("service started")
    last_state = None
    last_refresh = 0.0

    # 启动即刷新一次(确保屏保图是最新的)
    try:
        if png_render(get_dashboard_data()):
            last_refresh = time.time()
            log("initial refresh done")
        else:
            log("initial fetch failed; kept previous screensaver")
    except Exception as exc:
        log("initial refresh failed: %r" % exc)

    while True:
        state = powerd_state()
        now = time.time()

        if state != last_state:
            log("state: %s -> %s" % (last_state, state))

        if state in ("active", "ready"):
            if last_state not in ("active", "ready"):
                # 刚唤醒:立即刷新
                try:
                    if png_render(get_dashboard_data()):
                        last_refresh = now
                        log("wake refresh done")
                    else:
                        log("wake fetch failed; kept previous screensaver")
                except Exception as exc:
                    log("wake refresh failed: %r" % exc)
            elif now - last_refresh >= SERVICE_INTERVAL:
                # 清醒中定期刷新
                try:
                    if png_render(get_dashboard_data()):
                        last_refresh = now
                        log("periodic refresh done")
                    else:
                        log("periodic fetch failed; kept previous screensaver")
                except Exception as exc:
                    log("periodic refresh failed: %r" % exc)

        last_state = state
        time.sleep(3)


# ---------------- 入口 ----------------

def mock_data(err=False, poster=False):
    """离线假数据:--mock(仪表盘)/ --mock-race(赛事海报)/ --mock-err 用,渲染不联网。

    poster=True 时 next_race 落在赛事海报窗口内(距开赛 <3 天),倒计时由真实
    时间差推导,便于开发机预览海报版式;否则取较远的收官战,预览仪表盘版式。
    """
    if poster:
        next_race = {"raceName": "Dutch Grand Prix", "round": "12",
                     "date": "2026-08-23", "time": "13:00:00Z",
                     "Circuit": {"circuitId": "zandvoort",
                                 "Location": {"locality": "Zandvoort",
                                              "country": "Netherlands"}}}
        last_race = {"raceName": "Hungarian Grand Prix", "round": "11"}
    else:
        next_race = {"raceName": "Abu Dhabi Grand Prix", "round": "23",
                     "date": "2026-12-06", "time": "13:00:00Z",
                     "Circuit": {"circuitId": "yas_marina",
                                 "Location": {"locality": "Abu Dhabi",
                                              "country": "United Arab Emirates"}}}
        last_race = {"raceName": "Qatar Grand Prix", "round": "22"}
    ts = race_start_ts(next_race)
    return {
        "next_race": None if err else next_race,
        "last_race": None if err else last_race,
        "standings": [("1", "Kimi Antonelli", "219"), ("2", "Lewis Hamilton", "169"),
                      ("3", "Max Verstappen", "163"), ("4", "Lando Norris", "152"),
                      ("5", "Charles Leclerc", "131"), ("6", "George Russell", "122"),
                      ("7", "Oscar Piastri", "118"), ("8", "Carlos Sainz", "104")],
        "constructors": [("1", "Mercedes", "379"), ("2", "Ferrari", "307"),
                         ("3", "McLaren", "281"), ("4", "Red Bull", "242"),
                         ("5", "Aston Martin", "117"), ("6", "Williams", "48"),
                         ("7", "Alpine", "42"), ("8", "Haas", "21")],
        "last_results": [("1", "Lando Norris", "1:39:56.180"),
                         ("2", "Max Verstappen", "+15.080"),
                         ("3", "Oscar Piastri", "+18.362"),
                         ("4", "Charles Leclerc", "+22.054"),
                         ("5", "George Russell", "Power Unit")],
        "countdown": None if err else fmt_countdown(ts),
        "progress": None if err else (11, 23),
        "upd": "12:34",
        "err": Exception("network unreachable") if err else None,
    }


def main():
    argv = sys.argv[1:]
    mode = next((a for a in argv if not a.startswith("--")), "show")
    out = SS_OUT
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    if mode == "service":
        service_mode()
        return 0
    if "--mock" in argv:
        data = mock_data(err=False)
    elif "--mock-race" in argv:
        data = mock_data(err=False, poster=True)
    elif "--mock-err" in argv:
        data = mock_data(err=True)
    else:
        data = get_dashboard_data()
    if mode == "png":
        png_render(data, out=out)
    else:
        show_render(data)
    return 0 if data["err"] is None else 1


if __name__ == "__main__":
    sys.exit(main())

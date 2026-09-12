#!/mnt/us/python3/bin/python3.9
# -*- coding: utf-8 -*-
"""F1 Dashboard for Kindle Paperwhite 1 (600x800 E-Ink).

数据源:Jolpica F1(Ergast 兼容接口,https://api.jolpi.ca/ergast/f1/)
字体:系统 /usr/java/lib/fonts/(只读 rootfs,直接引用)

用法:
    f1dash.py png     拉数据 → Pillow 渲染 758x1024 → 写 linkss 屏保图片(默认)
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

# 屏保图片规格(linkss):758x1024,与 KPW1 物理屏 1:1 对应。
# 全屏铺满:内容不再只画在居中 600×800 设计区(原 DX=79/DY=112 会在四周留 ~10% 白边),
# 而是把 600×800 设计网格按 FULL_SCALE 等比放大到整个画布,仅留极细边距避免顶到面板边。
SS_SIZE = (758, 1024)
SS_OUT = "/mnt/us/linkss/screensavers/bg_ss00.png"
DX, DY = 0, 0
FULL_SCALE = 1.24
FX = round((SS_SIZE[0] - 600 * FULL_SCALE) / 2)   # ≈7
FY = round((SS_SIZE[1] - 800 * FULL_SCALE) / 2)   # ≈16


def fs(x):
    """设计区 x 坐标 → 画布 x 坐标(等比放大 + 居中偏移)。"""
    return round(x * FULL_SCALE + FX)


def fy(y):
    """设计区 y 坐标 → 画布 y 坐标(等比放大 + 居中偏移)。"""
    return round(y * FULL_SCALE + FY)


def fbox(x0, y0, x1, y1):
    """设计区 box → 画布 box(传给 draw_track / draw_car / draw_track_fit)。"""
    return (fs(x0), fy(y0), fs(x1), fy(y1))

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


# ---------------- 赛事周末结果块(排位赛 / 冲刺赛) ----------------
# 海报在周末里随"会话已结束"渐进补结果。会话时间表直接读 next_race 里的
# Qualifying / Sprint 字段(UTC),无需额外请求;仅拉取已结束会话的数据。

def session_ts(sess):
    """会话 dict(含 date+time) → UTC epoch;缺失返回 None。"""
    if not sess:
        return None
    return race_start_ts({"date": sess.get("date", ""), "time": sess.get("time", "00:00:00Z")})


def completed_sessions(race, now=None):
    """已结束的赛果会话列表 [(key, label, ts)...]。

    按"正赛相关度优先"排序:排位赛(决定正赛发车)在前、冲刺赛在后。
    只返回"已结束"(session_ts <= now)的会话。
    """
    if now is None:
        now = time.time()
    items = []
    q = race.get("Qualifying")
    if q:
        items.append(("qualifying", "QUALIFYING", session_ts(q)))
    sp = race.get("Sprint")
    if sp:
        items.append(("sprint", "SPRINT RACE", session_ts(sp)))
    return [(k, l, t) for (k, l, t) in items if t is not None and t <= now]


def _fastest_lap(qual):
    """Q3→Q2→Q1 中已记录的最快单圈;无则空串。"""
    for k in ("Q3", "Q2", "Q1"):
        if qual.get(k):
            return qual[k]
    return ""


def fmt_qualifying(results):
    """返回 [(pos, name, lap)...];lap 取该车最快单圈(默认 Q3)。"""
    rows = []
    for r in results[:TOP_RESULTS]:
        pos = r.get("position", "?")
        name = unidecode.unidecode(
            "%s %s" % (r["Driver"]["givenName"], r["Driver"]["familyName"]))
        rows.append((pos, name, _fastest_lap(r)))
    return rows


def fmt_sprint(results):
    """返回 [(pos, name, gap)...];gap 取完赛总时长/差距,退赛者取 status(规则同 fmt_results)。"""
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


def weekend_poster_sessions(next_race, season, now=None):
    """海报结果块列表 [{key,label,rows}...];仅拉已结束会话,每块独立容错(失败空 rows)。"""
    blocks = []
    for key, label, _ in completed_sessions(next_race, now):
        try:
            if key == "qualifying":
                res = fetch("%s/%s/qualifying.json" % (season, next_race["round"]))
                arr = res["MRData"]["RaceTable"]["Races"][0]["QualifyingResults"]
                rows = fmt_qualifying(arr)
            elif key == "sprint":
                res = fetch("%s/%s/sprint.json" % (season, next_race["round"]))
                arr = res["MRData"]["RaceTable"]["Races"][0]["SprintResults"]
                rows = fmt_sprint(arr)
            else:
                continue
        except Exception:
            rows = []
        if rows:
            blocks.append({"key": key, "label": label, "rows": rows})
    return blocks


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
           "constructors": [], "last_results": [],
           "progress": None, "season": None, "poster_sessions": [],
           "upd": upd, "err": None}
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
    out["season"] = season
    if nr is not None:
        # 仅赛事周末:拉已结束会话的排位/冲刺赛结果,供海报展示
        if race_poster_active(out):
            out["poster_sessions"] = weekend_poster_sessions(nr, season)
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


# ---------------- Pillow 屏保图片(png) ----------------
# 单页 Timing Tower 编辑版式(600x800 设计区,× FULL_SCALE 铺满 758×1024):
#   0-44    页眉黑条(赛车剪影 + F1 DASHBOARD + 斜纹)
#   66-262  HERO:左侧下一场大字比赛名,右侧赛道轮廓主角
#   272-340 开赛时间反色板(LIGHTS OUT · UTC+8;屏保零刷新,不渲染倒计时)
#   360-402 赛季刻度尺 + 双计时塔(车手/车队各 8 行,P1 行加粗)
#   660-754 上场比赛领奖台 Top 3
#   768     页脚(LAST UPDATE)
# 事件海报(距开赛 <3 天)与领奖台/结果块共用同一字体/灰度/标签语法。

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


GRAY_INK2, GRAY_LABEL, GRAY_SOFT, GRAY_FAINT = 90, 120, 170, 195


def surname(full):
    """'Kimi Antonelli' → 'ANTONELLI'(计时塔语法,单名词原样大写)。"""
    return full.split()[-1].upper()


def split_race_name(race):
    """'Abu Dhabi Grand Prix' → ('ABU DHABI', 'GRAND PRIX');无后缀时 (原名, 'ROUND N')。"""
    name = race["raceName"]
    if name.lower().endswith(" grand prix"):
        return name[: -len(" Grand Prix")].upper(), "GRAND PRIX"
    return name.upper(), "ROUND %s" % race["round"]


def short_date(iso):
    """'2026-12-06' → 'DEC 06';解析失败原样返回。"""
    try:
        return time.strftime("%b %d", time.strptime(iso, "%Y-%m-%d")).upper()
    except Exception:
        return iso


def _fit(d, font, s, max_w, start, bold=True, min_size=14):
    """动态缩字直到宽度 <= max_w(设计区坐标)。"""
    size = start
    while size > min_size and d.textlength(s, font=font(bold, size)) / FULL_SCALE > max_w:
        size -= 1
    return size


def _text_tracked(d, font, x, y, s, size, bold=False, fill=0, track=2, anchor="la"):
    """加字距小号大写标签;支持 la/ma/ra 三种水平锚。"""
    f = font(bold, size)
    wd = sum(d.textlength(ch, font=f) for ch in s) / FULL_SCALE + track * len(s)
    if anchor[0] == "m":
        x -= wd / 2
    elif anchor[0] == "r":
        x -= wd
    cx = fs(x)
    for ch in s:
        d.text((cx, fy(y)), ch, font=f, fill=fill)
        cx += d.textlength(ch, font=f) + track * FULL_SCALE


def _masthead(d, font, text):
    """品牌页眉:黑条 + 赛车剪影 + F1 DASHBOARD + 斜纹(两版式共用)。"""
    d.rectangle([fs(0), fy(0), fs(600) - 1, fy(44)], fill=0)
    car = get_car()
    if car:
        draw_car(d, fbox(8, 8, 138, 38), car, fill=255)
    text(300, 23, "F1 DASHBOARD", font(True, 21), fill=255, anchor="mm")
    for x in (492, 508, 524):
        d.line([fs(x), fy(34), fs(x + 15), fy(10)], fill=255, width=2)
    text(578, 23, "F1", font(True, 17), fill=255, anchor="rm")


def _season_ruler(d, x0, x1, y, done, total, tick_w=3.4, tick_h=9):
    """圈数计分牌式赛季进度刻度尺。"""
    if not total:
        return
    step = (x1 - x0) / float(total)
    for i in range(total):
        x = x0 + i * step
        d.rectangle([fs(x), fy(y), fs(x + tick_w), fy(y + tick_h)],
                    fill=0 if i < done else GRAY_FAINT)


def _tower_block(d, font, text, x0, x1, label, rows, ty):
    """计时塔区块:小标签 + 细线 + 8 行(P1 加粗,行下压细线)。"""
    _text_tracked(d, font, x0, ty, label, 12, fill=GRAY_LABEL)
    d.line([fs(x0), fy(ty + 20), fs(x1), fy(ty + 20)], fill=GRAY_SOFT, width=2)
    ry = ty + 34
    for i, (pos, name, val) in enumerate(rows):
        lead = pos == "1"
        text(x0, ry, pos, font(lead, 14), fill=0 if lead else GRAY_FAINT, anchor="lm")
        text(x0 + 24, ry, name, font(lead, 16), anchor="lm")
        text(x1, ry, str(val), font(True, 16), anchor="rm")
        ry += 26
        if i == 0:
            # 分隔线落在 26px 行距的中点,距上下两行文字各留 ~4px
            d.line([fs(x0), fy(ry - 13), fs(x1), fy(ry - 13)], fill=GRAY_SOFT, width=1)


def _draw_dashboard(d, data, font, text):
    """默认仪表盘版式(非赛事周末):Timing Tower 编辑风格。

    屏保零刷新,一切"相对当前时刻"的动态文案(倒计时)不渲染,只展示静态事实。
    """
    MARGIN, RIGHT = 28, 572
    _masthead(d, font, text)
    nr = data["next_race"]

    # HERO:左文右图,赛道轮廓为页面主角
    place, sub = split_race_name(nr)
    _text_tracked(d, font, MARGIN, 66, "NEXT RACE · ROUND %s" % nr["round"], 13,
                  fill=GRAY_LABEL)
    nsz = _fit(d, font, place, 300 - MARGIN - 8, 44)
    text(MARGIN, 90, place, font(True, nsz))
    _text_tracked(d, font, MARGIN, 90 + nsz + 8, sub, 16, fill=GRAY_INK2, track=4)
    pts = get_track(nr["Circuit"]["circuitId"])
    if pts:
        draw_track_fit(d, fbox(312, 58, 572, 262), pts, fill=0, width=4)

    # 开赛时间板:全页唯一反色锚点(北京时间)
    py0, py1 = 272, 340
    d.rectangle([fs(MARGIN), fy(py0), fs(RIGHT) - 1, fy(py1)], fill=0)
    _text_tracked(d, font, 46, py0 + 8, "LIGHTS OUT · UTC+8", 12, fill=210)
    day = format_race_daytime(nr) or ""
    when = "%s · %s" % (short_date(nr["date"]), day) if day else short_date(nr["date"])
    text(46, py0 + 26, when, font(True, _fit(d, font, when, 500, 34)), fill=255)

    # 赛季进度刻度尺
    done, total = data["progress"] or (0, 0)
    _text_tracked(d, font, MARGIN, 360, "SEASON %s" % data["season"], 12, fill=GRAY_LABEL)
    _season_ruler(d, MARGIN + 112, 486, 372, done, total)
    text(RIGHT, 364, "%d/%d" % (done, total), font(True, 16), anchor="ra")

    # 计时塔:车手 / 车队
    ty = 408
    _tower_block(d, font, text, MARGIN, 290, "DRIVER STANDINGS",
                 [(p, surname(n), v) for p, n, v in data["standings"]], ty)
    _tower_block(d, font, text, 322, RIGHT, "CONSTRUCTOR STANDINGS",
                 [(p, n.upper(), v) for p, n, v in data["constructors"]], ty)

    # 上场比赛:领奖台 Top 3
    if data["last_race"]:
        last_place, _ = split_race_name(data["last_race"])
        ly = 660
        _text_tracked(d, font, MARGIN, ly, "LAST RACE · %s" % last_place, 12, fill=GRAY_LABEL)
        d.line([fs(MARGIN), fy(ly + 20), fs(RIGHT), fy(ly + 20)], fill=GRAY_SOFT, width=2)
        ry = ly + 34
        for pos, name, gap in data["last_results"][:3]:
            lead = pos == "1"
            text(MARGIN, ry, pos, font(lead, 14), fill=0 if lead else GRAY_FAINT, anchor="lm")
            text(MARGIN + 24, ry, surname(name), font(lead, 16), anchor="lm")
            text(RIGHT, ry, gap, font(lead, 15), fill=0 if lead else GRAY_INK2, anchor="rm")
            ry += 26

    # 页脚
    d.line([fs(MARGIN), fy(768), fs(RIGHT), fy(768)], fill=GRAY_SOFT, width=1)
    _text_tracked(d, font, MARGIN, 776, "LAST UPDATE %s" % data["upd"], 12,
                  fill=GRAY_LABEL, track=1)


def _poster_footer(d, data, font, text, nr):
    """海报区之下:SEASON·ROUND + 进度刻度(同一基线) + 共用页脚。"""
    done, total = data["progress"] or (0, 0)
    _text_tracked(d, font, 28, 694, "SEASON %s · ROUND %s" % (data["season"], nr["round"]),
                  15, fill=GRAY_INK2, track=2)
    _season_ruler(d, 400, 520, 701, done, total, tick_w=3, tick_h=8)
    text(572, 698, "%d/%d" % (done, total), font(False, 14), fill=GRAY_LABEL, anchor="ra")
    d.line([fs(28), fy(768), fs(572), fy(768)], fill=GRAY_SOFT, width=1)
    _text_tracked(d, font, 28, 776, "LAST UPDATE %s" % data["upd"], 12,
                  fill=GRAY_LABEL, track=1)


def _draw_poster(d, data, font, text):
    """赛事海报版式(距下一场开赛 <RACE_POSTER_WINDOW)。

    无赛果:大海报(赛道主角 + 比赛名 + 地点 + 开赛时间);
    有赛果(周末里排位/冲刺已结束):压缩 hero + 结果块堆叠(_draw_poster_results)。
    """
    _masthead(d, font, text)
    nr = data["next_race"]
    if data.get("poster_sessions"):
        _draw_poster_results(d, data, font, text, nr)
        return

    # ---- 无赛果:大海报版式 ----
    d.rectangle([fs(0), fy(56), fs(600) - 1, fy(600)], fill=0)
    pts = get_track(nr["Circuit"]["circuitId"])
    if pts:
        draw_track_fit(d, fbox(120, 96, 480, 300), pts, fill=255, width=3)
    title = nr["raceName"].upper()
    text(300, 340, title, font(True, _fit(d, font, title, 540, 40)), fill=255, anchor="ma")
    loc = nr.get("Circuit", {}).get("Location", {})
    place = ("%s · %s" % (loc.get("locality", ""), loc.get("country", ""))).upper()
    if place.strip(" ·"):
        _text_tracked(d, font, 300, 382, place, 15, fill=210, anchor="ma", track=2)
    day = format_race_daytime(nr)
    if day:
        text(300, 430, day, font(True, _fit(d, font, day, 530, 52)), fill=255, anchor="ma")
    _poster_footer(d, data, font, text, nr)


def _draw_poster_results(d, data, font, text, nr):
    """赛事周末"有结果"版式:压缩 hero + 结果块堆叠(排位在前、冲刺在后,各 Top 5)。"""
    d.rectangle([fs(0), fy(56), fs(600) - 1, fy(660)], fill=0)
    pts = get_track(nr["Circuit"]["circuitId"])
    if pts:
        draw_track_fit(d, fbox(150, 74, 450, 178), pts, fill=255, width=3)
    title = nr["raceName"].upper()
    text(300, 188, title, font(True, _fit(d, font, title, 540, 36)), fill=255, anchor="ma")
    loc = nr.get("Circuit", {}).get("Location", {})
    place = ("%s · %s" % (loc.get("locality", ""), loc.get("country", ""))).upper()
    if place.strip(" ·"):
        _text_tracked(d, font, 300, 226, place, 14, fill=210, anchor="ma", track=2)
    day = format_race_daytime(nr)
    if day:
        text(300, 250, day, font(True, 28), fill=255, anchor="ma")

    # 结果块:计时塔语法(白字),标签 + 白细线 + 行,P1 行加粗
    y = 300
    for b in data["poster_sessions"]:
        _text_tracked(d, font, 48, y, b["label"], 14, fill=255, track=3)
        d.line([fs(48), fy(y + 24), fs(552), fy(y + 24)], fill=110, width=1)
        yy = y + 38
        for pos, name, val in b["rows"]:
            lead = pos == "1"
            text(48, yy, pos, font(lead, 15), fill=255 if lead else 180, anchor="lm")
            text(84, yy, surname(name), font(lead, 17), fill=255, anchor="lm")
            text(552, yy, val, font(lead, 15), fill=240 if lead else 210, anchor="rm")
            yy += 28
        y = yy + 16

    _poster_footer(d, data, font, text, nr)


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
        # 字号按 FULL_SCALE 等比放大,保证真正铺满 758×1024 画布
        for path in (FONT_BOLD if bold else FONT_MED,
                     "/System/Library/Fonts/Helvetica.ttc"):
            try:
                return ImageFont.truetype(path, max(1, round(size * FULL_SCALE)))
            except Exception:
                continue
        return ImageFont.load_default()

    def text(x, y, s, f, fill=0, anchor="la"):
        d.text((fs(x), fy(y)), s, font=f, fill=fill, anchor=anchor)

    # 赛事周末(距下一场开赛 <3 天)切海报,否则默认仪表盘
    if race_poster_active(data):
        _draw_poster(d, data, font, text)
    else:
        _draw_dashboard(d, data, font, text)

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
                     "Qualifying": {"date": "2026-08-22", "time": "13:00:00Z"},
                     "Sprint": {"date": "2026-08-22", "time": "08:00:00Z"},
                     "Circuit": {"circuitId": "zandvoort",
                                 "Location": {"locality": "Zandvoort",
                                              "country": "Netherlands"}}}
        last_race = {"raceName": "Hungarian Grand Prix", "round": "11"}
        poster_sessions = [
            {"key": "qualifying", "label": "QUALIFYING", "rows": [
                ("1", "Kimi Antonelli", "1:32.064"),
                ("2", "George Russell", "1:32.286"),
                ("3", "Lewis Hamilton", "1:32.415"),
                ("4", "Charles Leclerc", "1:32.428"),
                ("5", "Oscar Piastri", "1:32.550")]},
            {"key": "sprint", "label": "SPRINT RACE", "rows": [
                ("1", "George Russell", "33:38.998"),
                ("2", "Charles Leclerc", "+2.051"),
                ("3", "Lewis Hamilton", "+3.214"),
                ("4", "Lando Norris", "+4.158"),
                ("5", "Max Verstappen", "+7.032")]},
        ]
    else:
        next_race = {"raceName": "Abu Dhabi Grand Prix", "round": "23",
                     "date": "2026-12-06", "time": "13:00:00Z",
                     "Circuit": {"circuitId": "yas_marina",
                                 "Location": {"locality": "Abu Dhabi",
                                              "country": "United Arab Emirates"}}}
        last_race = {"raceName": "Qatar Grand Prix", "round": "22"}
        poster_sessions = []
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
        "progress": None if err else (11, 23),
        "season": None if err else ("2026" if poster else "2026"),
        "poster_sessions": [] if err else poster_sessions,
        "upd": "12:34",
        "err": Exception("network unreachable") if err else None,
    }


def main():
    argv = sys.argv[1:]
    mode = next((a for a in argv if not a.startswith("--")), "png")
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
    png_render(data, out=out)
    return 0 if data["err"] is None else 1


if __name__ == "__main__":
    sys.exit(main())

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
TRACKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks.json")

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
#   0-56   标题栏
#   64-196 NEXT RACE(赛道名/轮次日期/倒计时/赛季进度条),右侧赛道轮廓图
#   204-476 两栏榜单(车手/车队各 8 行,行内积分条)
#   496-682 上场比赛 Top 5
#   760    页脚

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


def png_render(data, out=SS_OUT):
    img = Image.new("L", SS_SIZE, 255)
    d = ImageDraw.Draw(img)

    def font(bold, size):
        return ImageFont.truetype(FONT_BOLD if bold else FONT_MED, size)

    def text(x, y, s, f, fill=0, anchor="la"):
        d.text((DX + x, DY + y), s, font=f, fill=fill, anchor=anchor)

    # 标题栏
    d.rectangle([DX, DY, DX + 600 - 1, DY + 56], fill=0)
    text(300, 28, "F1  DASHBOARD", font(True, 34), fill=255, anchor="mm")

    if data["err"] is not None:
        text(300, 380, "FETCH FAILED", font(True, 32), anchor="mm")
        text(300, 430, str(data["err"])[:60], font(False, 20), anchor="mm")
    else:
        next_race = data["next_race"]
        if next_race is not None:
            # NEXT RACE + 倒计时,赛道图在右侧
            text(20, 64, "NEXT RACE", font(True, 22))
            text(240, 98, next_race["raceName"], font(True, 30), anchor="mm")
            text(240, 134, "Round %s | %s" % (next_race["round"], next_race["date"]),
                 font(False, 20), anchor="mm")
            if data["countdown"]:
                text(240, 162, data["countdown"], font(True, 20), anchor="mm")
            d.rectangle([DX + 460, DY + 64, DX + 590, DY + 196], outline=160)
            pts = get_track(next_race["Circuit"]["circuitId"])
            if pts:
                draw_track(d, (460, 64, 590, 196), pts)
        else:
            text(300, 120, "SEASON COMPLETE", font(True, 30), anchor="mm")

        # 赛季进度条(11/23 放进度条右侧,避开右侧赛道图框区域 x460-590)
        done, total = data["progress"] or (0, 0)
        text(20, 186, "SEASON PROGRESS", font(False, 18))
        d.rectangle([DX + 20, DY + 184, DX + 320, DY + 190], outline=160)
        if total:
            d.rectangle([DX + 20, DY + 184,
                         DX + 20 + int(300.0 * done / total), DY + 190], fill=0)
        text(330, 186, "%d/%d" % (done, total), font(False, 18))

        # 两栏榜单(行内积分条,与榜首归一化)
        def render_list(rows, x0, pts_x, leader_pts):
            y = 236
            for pos, name, pts in rows:
                text(x0, y, "%2s" % pos, font(True, 20), anchor="lm")
                text(x0 + 30, y, name, font(False, 20), anchor="lm")
                text(pts_x, y, "%4s" % pts, font(True, 20), anchor="rm")
                if leader_pts:
                    bar_w = int(int(pts) * 208 / leader_pts)
                    if bar_w > 0:
                        d.rectangle([DX + x0 + 30, DY + y + 14,
                                     DX + x0 + 30 + bar_w, DY + y + 18], fill=160)
                y += 30

        text(20, 204, "DRIVER STANDINGS", font(True, 22))
        text(320, 204, "CONSTRUCTOR", font(True, 22))
        render_list(data["standings"], 20, 288,
                    int(data["standings"][0][2]) if data["standings"] else 0)
        render_list(data["constructors"], 320, 588,
                    int(data["constructors"][0][2]) if data["constructors"] else 0)

        # 上场比赛 Top 5
        last_race = data["last_race"]
        if last_race:
            text(20, 496, "LAST RACE | %s" % last_race["raceName"].upper(), font(True, 22))
            y = 528
            for pos, name, gap in data["last_results"]:
                text(20, y, "%2s" % pos, font(True, 20), anchor="lm")
                text(50, y, name, font(False, 20), anchor="lm")
                text(440, y, gap, font(False, 18), anchor="rm")
                y += 32

    text(20, 760, "Last update %s" % data["upd"], font(False, 20))
    img.save(out)
    print("saved:", out, img.size, img.mode)


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
        png_render(get_dashboard_data())
        last_refresh = time.time()
        log("initial refresh done")
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
                    png_render(get_dashboard_data())
                    last_refresh = now
                    log("wake refresh done")
                except Exception as exc:
                    log("wake refresh failed: %r" % exc)
            elif now - last_refresh >= SERVICE_INTERVAL:
                # 清醒中定期刷新
                try:
                    png_render(get_dashboard_data())
                    last_refresh = now
                    log("periodic refresh done")
                except Exception as exc:
                    log("periodic refresh failed: %r" % exc)

        last_state = state
        time.sleep(3)


# ---------------- 入口 ----------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "show"
    if mode == "service":
        service_mode()
        return 0
    data = get_dashboard_data()
    if mode == "png":
        png_render(data)
    else:
        show_render(data)
    return 0 if data["err"] is None else 1


if __name__ == "__main__":
    sys.exit(main())

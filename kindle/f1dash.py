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

TOP_DRIVERS = 10

SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def fetch(path):
    with urllib.request.urlopen(API_BASE + path, timeout=30, context=SSL_CTX) as resp:
        return json.load(resp)


def find_next_race(races):
    """按 round 顺序找下一场(比赛开始 2 小时内仍算"当前")。"""
    now = time.time()
    for race in races:
        t = race.get("time", "00:00:00Z").rstrip("Z")
        ts = calendar.timegm(time.strptime(
            "%s %s" % (race.get("date", ""), t), "%Y-%m-%d %H:%M:%S"))
        if ts >= now - 2 * 3600:
            return race
    return None


def fmt_standings(drivers):
    """返回 [(pos, name, points), ...],列独立方便对齐渲染。"""
    rows = []
    for s in drivers[:TOP_DRIVERS]:
        name = unidecode.unidecode(
            "%s %s" % (s["Driver"]["givenName"], s["Driver"]["familyName"]))
        rows.append((s["position"], name, s["points"]))
    return rows


def get_dashboard_data():
    """拉取并整理数据,返回 dict;任何异常转成 err 字段。"""
    # Kindle 时区未设置(按 UTC 走),更新时间按北京时间(UTC+8)显示
    upd = time.strftime("%H:%M", time.gmtime(time.time() + 8 * 3600))
    try:
        data = fetch("current.json")
        races = data["MRData"]["RaceTable"]["Races"]
        next_race = find_next_race(races)
        try:
            stand = fetch("current/driverstandings.json")
            drivers = stand["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]
        except Exception:
            drivers = []
        return {"next_race": next_race, "standings": fmt_standings(drivers),
                "upd": upd, "err": None}
    except Exception as exc:
        return {"next_race": None, "standings": [], "upd": upd, "err": exc}


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
            draw(row, "Round %s | %s" % (next_race["round"], next_race["date"]), center=True)
            row += 1
        else:
            draw(row, "SEASON COMPLETE", center=True, invert=True)
            row += 1

        row += 2
        draw(row, "DRIVER STANDINGS", center=True, invert=True)
        row += 1
        if data["standings"]:
            for pos, name, pts in data["standings"]:
                draw(row, "%2s  %-20s %4s" % (pos, name, pts))
                row += 1
        else:
            draw(row, "no data")

    draw(48, "Last update %s" % data["upd"], col=1)


# ---------------- Pillow 屏保图片(png) ----------------

def png_render(data, out=SS_OUT):
    img = Image.new("L", SS_SIZE, 255)
    d = ImageDraw.Draw(img)

    def font(bold, size):
        return ImageFont.truetype(FONT_BOLD if bold else FONT_MED, size)

    def text(x, y, s, f, fill=0, anchor="la"):
        d.text((DX + x, DY + y), s, font=f, fill=fill, anchor=anchor)

    def header(x, y, s):
        f = font(True, 26)
        text(x, y, s, f)
        d.line([(DX + x, DY + y + 6), (DX + 590, DY + y + 6)], fill=0, width=2)

    # 标题栏
    d.rectangle([DX, DY, DX + 600 - 1, DY + 56], fill=0)
    text(300, 28, "F1 DASHBOARD", font(True, 34), fill=255, anchor="mm")

    if data["err"] is not None:
        text(300, 380, "FETCH FAILED", font(True, 32), anchor="mm")
        text(300, 430, str(data["err"])[:60], font(False, 20), anchor="mm")
    else:
        next_race = data["next_race"]
        if next_race is not None:
            header(20, 80, "NEXT RACE")
            text(300, 130, next_race["raceName"], font(True, 30), anchor="mm")
            text(300, 168, "Round %s | %s" % (next_race["round"], next_race["date"]),
                 font(False, 22), anchor="mm")
        else:
            text(300, 120, "SEASON COMPLETE", font(True, 30), anchor="mm")

        header(20, 220, "DRIVER STANDINGS")
        y = 260
        for pos, name, pts in data["standings"]:
            text(20, y, "%2s" % pos, font(True, 22), anchor="lm")
            text(70, y, name, font(False, 22), anchor="lm")
            text(580, y, "%4s" % pts, font(True, 22), anchor="rm")
            y += 40

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

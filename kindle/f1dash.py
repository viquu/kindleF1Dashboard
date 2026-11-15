#!/mnt/us/python3/bin/python3.9
# -*- coding: utf-8 -*-
"""F1 Dashboard for Kindle Paperwhite 1 (600x800 E-Ink).

数据源:Jolpica F1(Ergast 兼容接口,https://api.jolpi.ca/ergast/f1/)
字体:系统 /usr/java/lib/fonts/(只读 rootfs,直接引用);云端/开发机走 fonts/ 后备

用法:
    f1dash.py png       拉数据 → Pillow 渲染 758x1024(开发预览;云端渲染同此函数)
    f1dash.py pull      从云端下载渲染好的屏保图(CLOUD_URL),失败保留旧图
    f1dash.py refresh   同 pull:只下载云端成图,云端不可达保留上一张屏保
    f1dash.py service   守护进程:唤醒即刷新 + 清醒时每 30 分钟刷新 + 可选一键回睡
    f1dash.py alwayson  常显守护:永不入睡,每小时拉图 → 叠加电量角标 → eips 全屏显示
    f1dash.py battreport  汇总常显期间电池采样 CSV,实测电流外推整块电池续航

架构:渲染已上移到 Oracle 云端(systemd timer 出图 + nginx 静态服务),
Kindle 只做"唤醒 → 下载成图"的瘦客户端,把唤醒刷新从几十秒压到几秒;
云端不可达不做本机渲染,直接保留最后一张屏保图(渲染代码保留,供云端与开发预览复用)。
一键回睡(auto_sleep 标志文件):信息牌模式下唤醒 → 刷新 → 模拟电源键
自动入睡,锁屏永远显示最新图;想正常阅读时在 KUAL 菜单关闭开关。
常显模式(alwayson):设备永不入睡,每小时拉云端图 eips 全屏重显示;RTC 无法
从挂起唤醒(见下),常显就是用续航换全天候实时。Wi-Fi 常开不关(2026-11-15
用户决策:实测 wifid 软关无效、硬关有把拉图链路搞挂的风险,代价 ~30-60 mAh/h)。
framework 停了屏上看不到状态栏电量,拉图后本地把电量角标(电池图标+百分比+
实测外推续航,低电加顶部警告条)叠进底部白边,不必 SSH 就知道还能撑多久。

说明:本设备(PW1 5.6.1.1)的外部 RTC 闹钟无法从挂起唤醒内核
(sysfs wakealarm 触发但无效,powerd rtcWakeup 不接受外部设置),
故设备休眠时守护进程冻结,无法在睡眠中刷新;F1 数据变化不频繁,
云端出图 + 唤醒即拉已足够实时。
"""

import calendar
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
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
# 超采样倍数:先在 4x 画布(3032x4096)渲染,再 LANCZOS 缩回 758x1024。
# 云端 CPU 便宜,亚像素级线条/文字/字距全部平滑;设备不再渲染(纯 pull),无算力顾虑。
AA_SCALE = 4
DX, DY = 0, 0
FULL_SCALE = 1.24
FX = round((SS_SIZE[0] - 600 * FULL_SCALE) / 2)   # ≈7
FY = round((SS_SIZE[1] - 800 * FULL_SCALE) / 2)   # ≈16


def fs(x):
    """设计区 x 坐标 → 画布 x 坐标(等比放大 + 居中偏移,× 超采样倍数)。"""
    return round(x * FULL_SCALE * AA_SCALE + FX * AA_SCALE)


def fy(y):
    """设计区 y 坐标 → 画布 y 坐标(等比放大 + 居中偏移,× 超采样倍数)。"""
    return round(y * FULL_SCALE * AA_SCALE + FY * AA_SCALE)


def fbox(x0, y0, x1, y1):
    """设计区 box → 画布 box(传给 draw_track / draw_mark / draw_track_fit)。"""
    return (fs(x0), fy(y0), fs(x1), fy(y1))


def lw(w):
    """设计区线宽 → 超采样画布线宽(缩回后仍是 w 个物理像素,边缘经 AA 平滑)。"""
    return max(1, round(w * AA_SCALE))

FONT_BOLD = "/usr/java/lib/fonts/Futura-Bold.ttf"
FONT_MED = "/usr/java/lib/fonts/Futura-Medium.ttf"
# 云端/开发机字体目录(与 f1dash.py 同级的 fonts/):放 Futura 副本或 Jost 后备,
# 见 _font_candidates()
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
# 仓库里的云端出图字体(cloud/fonts/,deploy.sh 同步到 /opt/f1dash/fonts/)
_CLOUD_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cloud", "fonts")

TOP_DRIVERS = 10   # 计时塔行数(窄体字体 + 24px 行距放得下,积分付薪位全覆盖)
TOP_CONSTRUCTORS = 10
TOP_RESULTS = 5
RACE_POSTER_WINDOW = 3 * 24 * 3600  # 距下一场开赛 <3 天(含比赛进行中)切赛事海报
TRACKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks.json")
ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "f1icon.json")

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# ---------------- 云端成图(Oracle 渲染,Kindle 下载) ----------------

CLOUD_URL = "http://149.118.42.190:8080/f1/latest.png"
PULL_TIMEOUT = 25  # 下载成图(~100KB)的宽限秒数
AUTO_SLEEP_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_sleep")


def pull_image(url=CLOUD_URL, outs=(SS_OUT,)):
    """从云端下载渲染好的图,原子替换写入每个目标;成功 True,失败保留旧图返回 False。

    校验 PNG 魔数,防止错误页/半截响应覆盖;与已有文件字节相同则跳过写盘。
    常显模式一次下载同时更新 eips 显示图与 linkss 锁屏图。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "f1dash-kindle"})
        with urllib.request.urlopen(req, timeout=PULL_TIMEOUT) as resp:
            data = resp.read()
    except Exception as exc:
        print("pull failed:", repr(exc)[:80])
        return False
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        print("pull failed: not a PNG (%d bytes)" % len(data))
        return False
    written = 0
    for out in outs:
        try:
            with open(out, "rb") as f:
                if f.read() == data:
                    continue
        except Exception:
            pass
        tmp = out + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out)
            written += 1
        except Exception as exc:
            print("pull failed: write error %s %r" % (out, repr(exc)[:60]))
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False
    if written:
        print("pull ok: %d bytes -> %s" % (len(data), ",".join(outs)))
    else:
        print("pull ok: unchanged")
    return True


def pull_screensaver(url=CLOUD_URL, out=SS_OUT):
    """从云端下载渲染好的屏保图(单目标,见 pull_image);失败保留旧图返回 False。"""
    return pull_image(url=url, outs=(out,))


def refresh(out=SS_OUT):
    """刷新屏保:下载云端成图(秒级);云端不可达保留上一张屏保图。

    设备端不做本机渲染(渲染代码保留,供云端 render.py 与开发预览复用)。
    返回 "pull" / None(拉图失败,保留上一张屏保)。
    """
    return "pull" if pull_screensaver(out=out) else None


def maybe_autosleep(refreshed):
    """信息牌一键回睡:开关标志存在且本次刷新成功时,模拟电源键入睡。

    刷新失败(断网等)不回睡,留在清醒态方便排查;想正常阅读时在
    KUAL 菜单把 Auto Sleep 切回 OFF。只在"唤醒触发的刷新"后调用,
    清醒中的周期刷新不回睡。
    """
    if not refreshed or not os.path.exists(AUTO_SLEEP_FLAG):
        return
    try:
        subprocess.run(["lipc-set-prop", "com.lab126.powerd", "powerButton", "1"],
                       timeout=5)
        log("auto sleep sent")
    except Exception as exc:
        log("auto sleep failed: %r" % exc)


def fetch_json(url, timeout=12):
    """通用 JSON GET(侧栏数据源共用);失败抛异常,由调用方按块兜底。"""
    req = urllib.request.Request(url, headers={"User-Agent": "f1dash/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return json.load(resp)


def fetch(path):
    return fetch_json(API_BASE + path, timeout=30)


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


_icon = None


def get_icon():
    """页眉头盔图标闭合环数组(0-1000 网格)或 None;f1icon.json 缺失/损坏时静默返回 None。

    (MDI racing-helmet,见 tools/make_icon.py;早期是自绘赛车剪影 car.json)
    """
    global _icon
    if _icon is None:
        try:
            with open(ICON_FILE) as f:
                rings = json.load(f).get("rings")
            _icon = rings if isinstance(rings, list) else None
        except Exception:
            _icon = None
    return _icon


MARK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "f1logo.json")
_mark = None


def get_mark():
    """F1 徽标闭合环数组(0-1000 网格,三个实心形状)或 None;缺失/损坏静默返回 None。"""
    global _mark
    if _mark is None:
        try:
            with open(MARK_FILE) as f:
                rings = json.load(f).get("rings")
            _mark = rings if isinstance(rings, list) else None
        except Exception:
            _mark = None
    return _mark


# ---------------- 侧栏数据源(天气 / 巴萨赛程 / GitHub Trending) ----------------
# 仪表盘底部三个资讯盒各配一个免费免 Key 数据源,独立容错:
# 任一失败只留空自己的盒子,不拖垮 F1 主面板。

WEATHER_CITY = {"name": "HANGZHOU", "lat": 30.25, "lon": 120.17, "tz": "Asia/Shanghai"}
BARCA_TEAM_ID = 83      # ESPN soccer 里的 FC Barcelona
BARCA_LEAGUE = "esp.1"  # LaLiga
TRENDING_URL = "https://github.com/trending"

# WMO 天气码 → 短语(open-meteo daily.weather_code)
WMO_TEXT = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
            45: "Fog", 48: "Fog",
            51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
            56: "Icy drizzle", 57: "Icy drizzle",
            61: "Light rain", 63: "Rain", 65: "Heavy rain",
            66: "Icy rain", 67: "Icy rain",
            71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow",
            80: "Showers", 81: "Showers", 82: "Heavy showers",
            85: "Snow showers", 86: "Snow showers",
            95: "Thunderstorm", 96: "Storms", 99: "Storms"}


def get_weather():
    """Hangzhou 每日预报(当日高低温/天气/降水概率 + 明日概要),Open-Meteo。

    只取 daily 日值,不取 current——屏保一挂一整天,当前气温几分钟后就失真。
    """
    q = WEATHER_CITY
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&daily=temperature_2m_max,temperature_2m_min,weather_code,"
           "precipitation_probability_max&timezone=%s&forecast_days=2"
           % (q["lat"], q["lon"], urllib.parse.quote(q["tz"])))
    daily = fetch_json(url)["daily"]

    def day(i):
        return {"high": round(daily["temperature_2m_max"][i]),
                "low": round(daily["temperature_2m_min"][i]),
                "text": WMO_TEXT.get(int(daily["weather_code"][i]), "—"),
                "pop": int(daily["precipitation_probability_max"][i] or 0)}

    return {"city": q["name"], "today": day(0), "tomorrow": day(1)}


def _laliga_season():
    """LaLiga 赛季年(ESPN 口径):8 月开踢跨年,6 月起算当年。"""
    now = time.gmtime()
    return now.tm_year if now.tm_mon >= 6 else now.tm_year - 1


def _espn_barca_entry(standings):
    """在 ESPN standings 嵌套结构里递归找 Barcelona 的 (rank, points)。"""
    def walk(o):
        if isinstance(o, dict):
            entries = o.get("entries")
            if isinstance(entries, list):
                for ent in entries:
                    if ent.get("team", {}).get("id") == str(BARCA_TEAM_ID):
                        stats = {s.get("name"): s.get("value")
                                 for s in ent.get("stats", [])}
                        return stats.get("rank"), stats.get("points")
            for v in o.values():
                got = walk(v)
                if got:
                    return got
        elif isinstance(o, list):
            for v in o:
                got = walk(v)
                if got:
                    return got
        return None
    return walk(standings)


def get_barca():
    """巴萨下一场 LaLiga 比赛(北京时间)+ 当前榜位积分;ESPN 公开接口。

    fixture=true 只返回未赛日程;取第一场(只看 LaLiga,杯赛不在列)。
    """
    out = {"opponent": None, "venue": None, "when": None, "rank": None, "points": None}
    sched = fetch_json("https://site.api.espn.com/apis/site/v2/sports/soccer/%s"
                       "/teams/%d/schedule?fixture=true" % (BARCA_LEAGUE, BARCA_TEAM_ID))
    now = time.time()
    for e in sched.get("events", []):
        try:
            ts = calendar.timegm(time.strptime(e["date"], "%Y-%m-%dT%H:%MZ"))
        except Exception:
            continue
        if ts < now:
            continue
        venue, opponent = "vs", None
        for c in e["competitions"][0]["competitors"]:
            if c["team"].get("id") == str(BARCA_TEAM_ID):
                venue = "vs" if c.get("homeAway") == "home" else "@"
            else:
                opponent = (c["team"].get("shortDisplayName")
                            or c["team"].get("displayName") or "?")
        out["venue"], out["opponent"] = venue, opponent
        out["when"] = time.strftime("%b %d %H:%M", time.gmtime(ts + 8 * 3600))
        break
    stand = fetch_json("https://site.api.espn.com/apis/v2/sports/soccer/%s/standings?season=%d"
                       % (BARCA_LEAGUE, _laliga_season()))
    got = _espn_barca_entry(stand)
    if got and got[0] is not None:
        out["rank"], out["points"] = _ordinal(got[0]), int(got[1] or 0)
    return out


def get_trending():
    """GitHub Trending 今日第一仓(抓 github.com/trending 首屏 HTML,无官方 API)。"""
    req = urllib.request.Request(TRENDING_URL, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
        html = resp.read().decode("utf-8", "replace")
    for chunk in html.split("<article")[1:]:
        h2 = re.search(r"<h2.*?</h2>", chunk, re.S)
        repo = None
        if h2:
            m = re.search(r'href="/([^"]+)"', h2.group(0))
            if m and "?" not in m.group(1) and "/" in m.group(1):
                repo = m.group(1)
        if not repo:
            continue
        stars = re.search(r"([\d,]+)\s*stars\s*today", chunk)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', chunk)
        total = re.search(r'href="/[^"]+/stargazers"[^>]*>\s*(?:<[^>]+>\s*)*([\d,]+)', chunk)
        return {"repo": repo,
                "stars_today": stars.group(1) if stars else "?",
                "lang": lang.group(1) if lang else None,
                "stars_total": total.group(1) if total else None}
    raise ValueError("no trending repo parsed")



def get_dashboard_data():
    """拉取并整理数据;赛历是唯一主获取,其余各块独立容错(失败只影响对应区块)。"""
    # Kindle 时区未设置(按 UTC 走),更新时间按北京时间(UTC+8)显示
    upd = time.gmtime(time.time() + 8 * 3600)
    out = {"next_race": None, "last_race": None, "standings": [],
           "constructors": [], "last_results": [],
           "progress": None, "season": None, "poster_sessions": [],
           "weather": None, "barca": None, "trending": None,
           "upd": time.strftime("%H:%M", upd), "upd_stamp": time.strftime("%m-%d %H:%M", upd),
           "err": None}
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

    # 底部资讯盒三块(天气/巴萨/GitHub Trending):独立容错,失败留空对应盒子
    for key, getter in (("weather", get_weather), ("barca", get_barca),
                        ("trending", get_trending)):
        try:
            out[key] = getter()
        except Exception as exc:
            print("side panel %s failed: %r" % (key, repr(exc)[:60]))
    return out


# ---------------- Pillow 屏保图片(png) ----------------
# 报纸风面板网格(600x800 设计区,× FULL_SCALE 铺满 758×1024):
#    8-792  外框(2px)
#    8-62   大字标题条 DASHBOARD + 更新时间小牌(MM-DD HH:MM,北京时间)
#   84-258  双积分榜 Top 5(标签牌 + 名次/大写名/点线引导/积分)
#  266-271  粗分隔线
#  288-470  NEXT RACE 面板:赛名大字 + Round N + 开赛时刻反色条 + 赛道轮廓
#  484-606  LAST RACE 领奖台三栏(1st/2nd/3rd)
#  620-778  资讯盒三联:每日天气 / 巴萨下一场 / GitHub Trending
# 赛事海报(距开赛 <3 天)仍走反色海报版式,与面板版式共用字体/标签语法。

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
    d.line(xy, fill=0, width=lw(2), joint="curve")


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
           fill=fill, width=lw(width), joint="curve")
    return (ox, oy, s)


def draw_mark(d, box, rings, fill=0):
    """矢量标志实心渲染:rings 为闭合环数组(0-1000 网格),box 为设计区坐标。

    页眉头盔(f1icon.json)与官方徽标(f1logo.json)共用。按 rings 实际 bbox
    等比装入 box;逐环 polygon 填充而不是合并成一个多边形——Pillow 的填充
    规则在嵌套子路径下会把重叠区挖成孔,分开填同色天然合并。
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


def _font_candidates(bold):
    """字体文件候选顺序:Barlow Semi Condensed(云端出图)→ Futura 副本 → Jost → 系统后备。

    云端 /opt/f1dash/fonts/ 由 deploy.sh 放入 Barlow(另可选 sync_fonts.sh 加 Futura);
    开发机从仓库 cloud/fonts/ 取;都缺则 Jost/系统字体兜底,版式经 _fit 自动适配。
    """
    barlow = "BarlowSemiCondensed-Bold.ttf" if bold else "BarlowSemiCondensed-Regular.ttf"
    futura = "Futura-Bold.ttf" if bold else "Futura-Medium.ttf"
    return [
        os.path.join(_FONT_DIR, barlow),
        os.path.join(_CLOUD_FONT_DIR, barlow),
        FONT_BOLD if bold else FONT_MED,
        os.path.join(_FONT_DIR, futura),
        os.path.join(_FONT_DIR, "Jost.ttf"),
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
    ]


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
    while size > min_size and d.textlength(s, font=font(bold, size)) / (FULL_SCALE * AA_SCALE) > max_w:
        size -= 1
    return size


def _text_tracked(d, font, x, y, s, size, bold=False, fill=0, track=2, anchor="la"):
    """加字距小号大写标签;支持 la/ma/ra 三种水平锚。"""
    f = font(bold, size)
    wd = sum(d.textlength(ch, font=f) for ch in s) / (FULL_SCALE * AA_SCALE) + track * len(s)
    if anchor[0] == "m":
        x -= wd / 2
    elif anchor[0] == "r":
        x -= wd
    cx = fs(x)
    for ch in s:
        d.text((cx, fy(y)), ch, font=f, fill=fill)
        cx += d.textlength(ch, font=f) + track * FULL_SCALE * AA_SCALE


def _masthead(d, font, text):
    """品牌页眉:黑条 + 赛车剪影 + F1 DASHBOARD + F1 官方徽标(两版式共用)。"""
    d.rectangle([fs(0), fy(0), fs(600) - 1, fy(44)], fill=0)
    icon = get_icon()
    if icon:
        draw_mark(d, fbox(14, 8, 66, 38), icon, fill=255)
    text(300, 23, "F1 DASHBOARD", font(True, 21), fill=255, anchor="mm")
    mark = get_mark()
    if mark:
        draw_mark(d, fbox(498, 11, 578, 34), mark, fill=255)


def _season_ruler(d, x0, x1, y, done, total, tick_w=3.4, tick_h=9):
    """圈数计分牌式赛季进度刻度尺。"""
    if not total:
        return
    step = (x1 - x0) / float(total)
    for i in range(total):
        x = x0 + i * step
        d.rectangle([fs(x), fy(y), fs(x + tick_w), fy(y + tick_h)],
                    fill=0 if i < done else GRAY_FAINT)


def _ordinal(n):
    """1 → '1st'(领奖台名次 / 巴萨榜位共用)。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if 11 <= n % 100 <= 13:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _dots(d, x0, x1, cy, fill=0, step=5, r=1.2):
    """点线引导(榜名与积分之间的省略号),设计区坐标。"""
    x = x0
    while x < x1:
        d.ellipse([fs(x - r), fy(cy - r), fs(x + r), fy(cy + r)], fill=fill)
        x += step


def _ellipsize(d, font, s, max_w, size, bold=False):
    """按设计区宽度截断长串(越界加 '...'),资讯盒仓库名用。"""
    if d.textlength(s, font=font(bold, size)) / (FULL_SCALE * AA_SCALE) <= max_w:
        return s
    while s and d.textlength(s + "...", font=font(bold, size)) / (FULL_SCALE * AA_SCALE) > max_w:
        s = s[:-1]
    return s + "..."


def _standings_rows(d, font, text, x0, x1, rows, y0, step=26):
    """积分榜 Top 5:名次 + 大写名 + 点线引导 + 右对齐积分(全黑,报纸语法)。"""
    ry = y0
    for pos, name, val in rows[:5]:
        name, val = str(name), str(val)
        text(x0, ry, str(pos), font(True, 13), anchor="lm")
        text(x0 + 34, ry, name, font(True, 15), anchor="lm")
        nw = d.textlength(name, font=font(True, 15)) / (FULL_SCALE * AA_SCALE)
        pw = d.textlength(val, font=font(True, 15)) / (FULL_SCALE * AA_SCALE)
        _dots(d, x0 + 34 + nw + 8, x1 - 8 - pw - 8, ry)
        text(x1 - 8, ry, val, font(True, 15), anchor="rm")
        ry += step


def _info_box(d, font, text, x0, x1, title, lines, note=None, y0=620, y1=778):
    """底部资讯盒:居中标题 + 下划线 + '- ' 行列(天气/巴萨/Trending 共用)。"""
    cx = (x0 + x1) / 2
    d.rectangle([fs(x0), fy(y0), fs(x1) - 1, fy(y1) - 1], outline=0, width=lw(2))
    text(cx, y0 + 12, title, font(True, 13), anchor="ma")
    d.line([fs(x0 + 14), fy(y0 + 30), fs(x1 - 14), fy(y0 + 30)], fill=0, width=lw(2))
    if not lines:
        lines = ["no data"]
    ly = y0 + 46
    for s in lines:
        if s:
            # 加粗:12px 常规体在 e-ink 上笔画太细,发虚难读
            text(x0 + 12, ly, "- %s" % s, font(True, 12))
        ly += 24
    if note:
        _text_tracked(d, font, cx, y1 - 16, note, 7, fill=GRAY_LABEL, anchor="ma")


def _draw_info_boxes(d, data, font, text):
    """底部三资讯盒:每日天气 / 巴萨下一场 / GitHub Trending(数据已各自容错)。"""
    w, b, t = data.get("weather"), data.get("barca"), data.get("trending")

    wlines = []
    if w:
        today, tom = w["today"], w["tomorrow"]
        rain = " %d%%" % today["pop"] if today["pop"] >= 30 else ""
        wlines = ["High %d°C  Low %d°C" % (today["high"], today["low"]),
                  "%s%s" % (today["text"], rain),
                  "Tomorrow %s %d°C" % (tom["text"], tom["high"])]
    _info_box(d, font, text, 28, 206, "%s TODAY" % (w["city"] if w else "WEATHER"),
              wlines, note="daily forecast — not current temp")

    blines = []
    if b:
        if b["opponent"]:
            blines.append("Next %s %s" % (b["venue"] or "vs", b["opponent"]))
        if b["when"]:
            blines.append(b["when"])
        if b["rank"]:
            blines.append("LaLiga %s · %d points" % (b["rank"], b["points"]))
    _info_box(d, font, text, 218, 392, "FC BARCELONA", blines)

    tlines = []
    if t:
        tlines.append(_ellipsize(d, font, "#1 %s" % t["repo"], 148, 12, bold=True))
        tlines.append("%s stars today" % t["stars_today"])
        if t["lang"]:
            tlines.append("trending in %s" % t["lang"])
    _info_box(d, font, text, 404, 572, "GITHUB TRENDING", tlines)


def _draw_dashboard(d, data, font, text):
    """默认仪表盘版式(非赛事周末):报纸风面板网格(见区块注释)。

    屏保零刷新,一切"相对当前时刻"的动态文案不渲染,只展示静态事实。
    """
    CX = 300

    # 外框
    d.rectangle([fs(8), fy(8), fs(592) - 1, fy(792) - 1], outline=0, width=lw(2))

    # 标题条(顶/右边与外框合并)+ 更新时间小牌(北京时间 MM-DD HH:MM)
    d.rectangle([fs(134), fy(8), fs(592) - 1, fy(62) - 1], outline=0, width=lw(2))
    text(360, 35, "DASHBOARD", font(True, _fit(d, font, "DASHBOARD", 424, 40)),
         anchor="mm")
    d.rectangle([fs(18), fy(8), fs(90) - 1, fy(32) - 1], outline=0, width=lw(2))
    _text_tracked(d, font, 54, 14, data["upd_stamp"], 8, anchor="ma", track=1)

    # 双积分榜 Top 5:标签牌 + 行列(无外框,点线引导)
    for x0, x1, label, rows in (
            (28, 292, "DRIVER STANDINGS — TOP 5",
             [(p, surname(n), v) for p, n, v in data["standings"]]),
            (308, 572, "CONSTRUCTOR STANDINGS — TOP 5",
             [(p, n.upper(), v) for p, n, v in data["constructors"]])):
        d.rectangle([fs(x0), fy(84), fs(x1) - 1, fy(114) - 1], outline=0, width=lw(2))
        text((x0 + x1) / 2, 99, label, font(True, _fit(d, font, label, x1 - x0 - 16, 13)),
             anchor="mm")
        _standings_rows(d, font, text, x0, x1, rows, 132)

    # 粗分隔线
    d.rectangle([fs(28), fy(266), fs(572) - 1, fy(271)], fill=0)

    # NEXT RACE 面板:左文(赛名/轮次/开赛条)右图(赛道轮廓),竖线分隔
    d.rectangle([fs(28), fy(288), fs(572) - 1, fy(470) - 1], outline=0, width=lw(2))
    nr = data["next_race"]
    if nr is None:
        text(CX, 379, "SEASON COMPLETE", font(True, 20), anchor="mm")
    else:
        d.rectangle([fs(44), fy(276), fs(140) - 1, fy(300) - 1],
                    fill=255, outline=0, width=lw(2))
        text(92, 288, "NEXT RACE", font(True, 12), anchor="mm")
        name = nr["raceName"].upper()
        text(44, 314, name, font(True, _fit(d, font, name, 304, 30)))
        text(44, 352, "Round %s" % nr["round"], font(True, 24))
        # 开赛时刻反色条(北京时间)
        d.rectangle([fs(44), fy(428), fs(346) - 1, fy(456) - 1], fill=0)
        when = "LIGHTS OUT  %s  %s" % (short_date(nr["date"]),
                                       format_race_daytime(nr) or "TBD")
        text(58, 442, when, font(True, _fit(d, font, when, 280, 14)), fill=255, anchor="lm")
        d.line([fs(364), fy(302), fs(364), fy(458)], fill=0, width=lw(2))
        pts = get_track(nr["Circuit"]["circuitId"])
        if pts:
            draw_track_fit(d, fbox(378, 302, 560, 420), pts, fill=0, width=3)
        cap = (nr["Circuit"].get("circuitName")
               or nr["Circuit"].get("circuitId") or "").replace("_", " ").upper()
        _text_tracked(d, font, 468, 434, cap,
                      _fit(d, font, cap, 176, 9, bold=False, min_size=7),
                      fill=GRAY_LABEL, anchor="ma", track=1)

    # LAST RACE 领奖台三栏
    d.rectangle([fs(28), fy(484), fs(572) - 1, fy(606) - 1], outline=0, width=lw(2))
    last = data["last_race"]
    if last is not None:
        last_place, _ = split_race_name(last)
        head = "LAST RACE — %s" % last_place
        text(CX, 506, head, font(True, _fit(d, font, head, 420, 21)), anchor="mm")
        d.line([fs(44), fy(528), fs(556), fy(528)], fill=0, width=lw(2))
        d.line([fs(213), fy(540), fs(213), fy(596)], fill=0, width=lw(2))
        d.line([fs(387), fy(540), fs(387), fy(596)], fill=0, width=lw(2))
        for i, (pos, name, _gap) in enumerate(data["last_results"][:3]):
            nm = surname(name)
            cx = (128, 300, 472)[i]
            text(cx, 544, _ordinal(pos), font(False, 13), anchor="ma")
            text(cx, 566, nm, font(True, _fit(d, font, nm, 128, 18)), anchor="ma")
    else:
        text(CX, 545, "NO RACE YET", font(True, 16), anchor="mm")

    # 底部资讯盒:天气 / 巴萨 / GitHub Trending
    _draw_info_boxes(d, data, font, text)


def _poster_footer(d, data, font, text, nr):
    """海报区之下:SEASON·ROUND + 进度刻度(同一基线) + 共用页脚。"""
    done, total = data["progress"] or (0, 0)
    _text_tracked(d, font, 28, 694, "SEASON %s · ROUND %s" % (data["season"], nr["round"]),
                  15, fill=GRAY_INK2, track=2)
    _season_ruler(d, 400, 520, 701, done, total, tick_w=3, tick_h=8)
    text(572, 698, "%d/%d" % (done, total), font(False, 14), fill=GRAY_LABEL, anchor="ra")
    d.line([fs(28), fy(768), fs(572), fy(768)], fill=GRAY_SOFT, width=lw(1))
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
    d.rectangle([fs(0), fy(56), fs(600) - 1, fy(664)], fill=0)
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
    # 编辑感锚点:超大空心轮次数字(填充面板黑、白色描边),吃掉海报下半留白
    # 垂直节奏(画布px):SUNDAY 底→标签 ~23,标签→数字顶 ~22,数字底→面板缘 ~46
    # (两间隙均分是几何上限:数字顶固定后 SUNDAY 底到数字顶只余 ~55px);
    # 标签锚 y(505)大于数字锚 y(489)是正常的——数字用 ma 锚,glyph 顶在锚下 ~46px
    rnd = nr.get("round", "")
    if rnd:
        _text_tracked(d, font, 300, 497, "ROUND", 12, fill=140, anchor="ma", track=4)
        text(300, 489, rnd, font(True, _fit(d, font, rnd, 420, 135)), fill=0,
             anchor="ma", stroke_width=lw(3), stroke_fill=255)
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
        d.line([fs(48), fy(y + 24), fs(552), fy(y + 24)], fill=110, width=lw(1))
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

    img = Image.new("L", (SS_SIZE[0] * AA_SCALE, SS_SIZE[1] * AA_SCALE), 255)
    d = ImageDraw.Draw(img)

    def font(bold, size):
        # 云端/开发机按 _font_candidates 顺序取字体(Barlow → Futura 副本 → Jost/系统后备)
        # 字号按 FULL_SCALE × AA_SCALE 放大到超采样画布,保证缩回后铺满 758×1024
        for path in _font_candidates(bold):
            try:
                f = ImageFont.truetype(path, max(1, round(size * FULL_SCALE * AA_SCALE)))
            except Exception:
                continue
            if os.path.basename(path) == "Jost.ttf":
                # Jost 是可变字体:按名称切到对应字重(失败则保持默认 Regular)
                try:
                    f.set_variation_by_name("Bold" if bold else "Medium")
                except Exception:
                    pass
            return f
        return ImageFont.load_default()

    def text(x, y, s, f, fill=0, anchor="la", **kw):
        # kw 透传 Pillow 原生参数(如 stroke_width/stroke_fill 描边字)
        d.text((fs(x), fy(y)), s, font=f, fill=fill, anchor=anchor, **kw)

    # 赛事周末(距下一场开赛 <3 天)切海报,否则默认仪表盘
    if race_poster_active(data):
        _draw_poster(d, data, font, text)
    else:
        _draw_dashboard(d, data, font, text)

    img = img.resize(SS_SIZE, Image.LANCZOS)
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

    # 启动即刷新一次(确保屏保图是最新的;启动路径不触发回睡)
    try:
        ok = refresh()
        if ok:
            last_refresh = time.time()
            log("initial refresh done (%s)" % ok)
        else:
            log("initial refresh failed; kept previous screensaver")
    except Exception as exc:
        log("initial refresh failed: %r" % exc)

    while True:
        state = powerd_state()
        now = time.time()

        if state != last_state:
            log("state: %s -> %s" % (last_state, state))

        if state in ("active", "ready"):
            if last_state not in ("active", "ready"):
                # 刚唤醒:立即刷新;信息牌开关开启且刷新成功则自动回睡
                try:
                    ok = refresh()
                    if ok:
                        last_refresh = now
                        log("wake refresh done (%s)" % ok)
                    else:
                        log("wake refresh failed; kept previous screensaver")
                except Exception as exc:
                    ok = None
                    log("wake refresh failed: %r" % exc)
                maybe_autosleep(ok)
            elif now - last_refresh >= SERVICE_INTERVAL:
                # 清醒中定期刷新(使用中,不回睡)
                try:
                    ok = refresh()
                    if ok:
                        last_refresh = now
                        log("periodic refresh done (%s)" % ok)
                    else:
                        log("periodic refresh failed; kept previous screensaver")
                except Exception as exc:
                    log("periodic refresh failed: %r" % exc)

        last_state = state
        time.sleep(3)


# ---------------- 常显模式(always-on) ----------------
#
# 与 service 模式互补的信息牌形态:设备永不入睡,每小时开 Wi-Fi 拉一次成图,
# eips 直接全屏显示后关 Wi-Fi。PW1 的 RTC 闹钟无法从挂起唤醒内核(见文件头),
# 睡着就刷不了;常显等于用续航换全天候实时。
# 前置光始终关闭 —— 常显模式下它是最大的单一耗电源。
# 循环内每轮重申防入睡标志并重试熄灯,防框架重置。

ALWAYS_OUT = "/mnt/us/f1dash/latest.png"   # eips 全屏显示用成图(SS_OUT + 电量角标)
ALWAYS_INTERVAL = 3600                     # 常显刷新周期(秒)
BADGE_STRIP_H = 26                         # 底部白边高度(画布物理像素,角标所在)
LOW_BATT_PCT = 15                          # 电量不高于此值时顶部加黑底警告条
BATT_SYSFS = "/sys/devices/system/yoshi_battery/yoshi_battery0"
BATT_LOG = "/mnt/us/f1dash/battery.csv"    # 电池采样 CSV(battreport 消费)
BATT_SAMPLE = 60                           # 采样周期:电芯计量更新缓慢,秒级无意义


def _sysfs_int(name):
    """读 BATT_SYSFS 下整数节点;缺失/异常返回 None。"""
    try:
        with open(os.path.join(BATT_SYSFS, name)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _sysfs_raw(name):
    """读 BATT_SYSFS 原始字符串节点(如 battery_capacity="83%");缺失返回 ""。"""
    try:
        with open(os.path.join(BATT_SYSFS, name)) as f:
            return f.read().strip()
    except Exception:
        return ""


def keep_awake(on=True):
    """阻止入睡与屏保(powerd preventScreenSaver,本机 5.6.1.1 实测可写可读回)。

    每轮循环重申一次,防框架重置;关闭时归零恢复正常睡眠。
    """
    try:
        subprocess.run(["lipc-set-prop", "com.lab126.powerd",
                        "preventScreenSaver", "1" if on else "0"],
                       capture_output=True, timeout=5)
    except Exception as exc:
        log("keep_awake(%s) failed: %r" % (on, exc))


def front_light_off():
    """熄灭前置光(powerd flIntensity/flAuto,本机实测属性名)。

    每轮循环重试,常态下漏关的可能性被压到最低;失败仅记日志不影响刷新。
    """
    try:
        subprocess.run(["lipc-set-prop", "com.lab126.powerd", "flIntensity", "0"],
                       capture_output=True, timeout=5)
        subprocess.run(["lipc-set-prop", "com.lab126.powerd", "flAuto", "0"],
                       capture_output=True, timeout=5)
    except Exception as exc:
        log("front light off failed: %r" % exc)


def is_charging():
    """powerd isCharging(1=充电中);查询失败按未充电处理。BatteryMeter 与电量角标共用。"""
    try:
        out = subprocess.run(["lipc-get-prop", "com.lab126.powerd",
                              "isCharging"], capture_output=True, text=True,
                             timeout=5)
        return out.stdout.strip() == "1"
    except Exception:
        return False


def framework_pids():
    """GUI 框架/状态栏渲染进程 pid 列表(cmdline 含 framework 或 pillowd)。

    本机 ps 列不全进程,必须扫 /proc;排除含 f1dash 的命令行避免误伤自身脚本。
    """
    pids = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as f:
                cmd = f.read()
        except Exception:
            continue
        if (b"framework" in cmd or b"pillowd" in cmd) and b"f1dash" not in cmd:
            pids.append(name)
    return pids


def framework_stop():
    """停掉 GUI 框架与 pillow 渲染器(upstart job):没有它们就没有东西往帧缓冲上画。

    实测(PW1 5.6.1.1):只停 framework 不够 —— pillowd(状态栏/对话框渲染进程)
    还在跑,时钟仍会周期刷新盖屏;两个都停才干净。wifid/powerd 是独立守护,
    不受影响。恢复用 always_on_stop.sh(start framework + start pillow);
    万一都不可用,重启 Kindle 也是干净出口。
    """
    ok = True
    for job in ("framework", "pillow"):
        try:
            r = subprocess.run(["/sbin/stop", job], capture_output=True, timeout=15)
            if r.returncode != 0:
                if b"Unknown instance" in (r.stderr or b""):
                    continue   # 本来就没在跑,正是想要的状态,不算失败
                log("stop %s: %s" % (job, (r.stderr or r.stdout)[:60]))
                ok = False
        except Exception as exc:
            log("stop %s failed: %r" % (job, exc))
            ok = False
    return ok


def framework_ensure_stopped():
    """确保 GUI 进程都停着:每轮循环检查,被系统重新拉起就再停一次。"""
    if framework_pids():
        framework_stop()
        return True
    return False


WLAN_IF = "wlan0"                  # PW1 无线接口(/proc/net/route 实测)
IFCONFIG = "/sbin/ifconfig"


def wifi(on):
    """Wi-Fi 开关:指挥 wifid(独立守护,framework 生死都可用)。

    wirelessEnable(cmd)是 framework 提供的 UI 层开关(会弹框/无人响应),不能用;
    enable 1 关联+拉图实测可用。enable 0 实测并不拆除关联——wifid 会把属性翻回
    1 并 ~15s 内自动重连,软关形同虚设;真要省电须停 wifid job 本体(未上线,
    2026-11-15 用户决策:常显 Wi-Fi 常开不关,避免硬关影响拉图链路)。故 off
    分支为空操作,on 保留自愈语义(接口 up + enable 1,断网后每轮自动恢复)。
    """
    if not on:
        return False
    try:
        subprocess.run([IFCONFIG, WLAN_IF, "up"], capture_output=True, timeout=5)
    except Exception as exc:
        log("ifconfig up failed: %r" % exc)
    try:
        subprocess.run(["lipc-set-prop", "com.lab126.wifid", "enable", "1"],
                       capture_output=True, timeout=5)
        return True
    except Exception as exc:
        log("wifid enable(1) failed: %r" % exc)
        try:
            subprocess.run(["lipc-set-prop", "com.lab126.cmd",
                            "wirelessEnable", "1"], capture_output=True, timeout=5)
        except Exception as exc2:
            log("cmd wirelessEnable(1) failed: %r" % exc2)
        return False


def wait_network(timeout=60):
    """等 Wi-Fi 完成关联并拿到默认路由;超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open("/proc/net/route") as f:
                for line in f.read().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) > 1 and parts[1] == "00000000":
                        return True
        except Exception:
            pass
        time.sleep(2)
    return False


def show_fullscreen(path=ALWAYS_OUT):
    """eips 显示成图:-g 解析 PNG,-f 全刷清残影;758x1024 原图无需缩放定位。

    本机 eips 把第一个非旗标参数当图片路径,-f 必须放在路径之后
    (`eips -g <path> -f`,实测 `eips -g -f <path>` 会去 fopen "-f" 报 rc=1);
    eips 不在默认 PATH(/usr/sbin),必须用绝对路径。
    """
    try:
        r = subprocess.run(["/usr/sbin/eips", "-g", path, "-f"],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            log("eips rc=%d %s; fallback partial update"
                % (r.returncode, r.stderr[:60]))
            subprocess.run(["/usr/sbin/eips", "-g", path],
                           capture_output=True, timeout=30)
    except Exception as exc:
        log("show failed: %r" % exc)


def _fmt_days(days):
    """续航估计(天)→ '~11d' / '~5.3d' / '~14h';None 返回 ''。"""
    if days is None:
        return ""
    if days >= 10:
        return "~%dd" % round(days)
    if days >= 1:
        return "~%.1fd" % days
    return "~%.0fh" % (days * 24.0)


def _badge_font(size_px):
    """角标字体:物理像素字号(角标直接画画布坐标,不乘 FULL_SCALE);候选同 _font_candidates。"""
    for path in _font_candidates(True):
        try:
            return ImageFont.truetype(path, size_px)
        except Exception:
            continue
    return ImageFont.load_default()


def overlay_battery(src=SS_OUT, dst=ALWAYS_OUT, status=None):
    """把电量角标叠加到干净成图副本并原子写 dst:底部白边画电池图标 + 百分比 +
    实测外推续航,电量 <=LOW_BATT_PCT 另加顶部黑底警告条。

    干净源 src 恒为云端原图,叠加每轮从 src 重做,角标电量/续航始终是当刻值
    (拉图失败也一样)。局部 4x 超采样再 LANCZOS 缩回,平滑度与云端成图一致。
    status 供开发机预览注入假数据;读不到电量(sysfs 缺失)时不画角标返回 False。
    """
    try:
        st = status if status is not None else battery_status()
        if not st or st.get("pct") is None:
            return False
        img = Image.open(src).convert("L")
        W, H = img.size
        p = AA_SCALE

        # 底部角标条(白边内右对齐):电池图标(按百分比填充) + "83% CHG  ~11d"
        strip = BADGE_STRIP_H
        patch = Image.new("L", (W * p, strip * p), 255)
        pd = ImageDraw.Draw(patch)
        s = "%d%%" % st["pct"]
        if st.get("charging"):
            s += " CHG"
        days = _fmt_days(st.get("days"))
        if days:
            s += "  " + days
        f = _badge_font(15 * p)
        x_right = W * p - 10 * p
        pd.text((x_right, strip * p // 2), s, font=f, fill=0, anchor="rm")
        tw = pd.textlength(s, font=f)
        bh = 12 * p
        bw = 22 * p
        by = (strip * p - bh) // 2
        bx = x_right - tw - 6 * p - bw - 4 * p   # 身体左缘:文字左缘 - 间隙 - 凸头 - 身宽
        pd.rectangle([bx, by, bx + bw, by + bh], outline=0, width=p)
        pd.rectangle([bx + bw + 2 * p, by + bh // 2 - 2 * p,
                      bx + bw + 4 * p, by + bh // 2 + 2 * p], fill=0)
        fw = max(p, round((bw - 4 * p) * max(0, min(100, st["pct"])) / 100.0))
        pd.rectangle([bx + 2 * p, by + 2 * p, bx + 2 * p + fw, by + bh - 2 * p], fill=0)
        img.paste(patch.resize((W, strip), Image.LANCZOS), (0, H - strip))

        # 低电量警告条:黑底白字盖住页眉顶部,醒目优先于完整性
        if st["pct"] <= LOW_BATT_PCT:
            bh2 = 34
            bpatch = Image.new("L", (W * p, bh2 * p), 0)
            bd = ImageDraw.Draw(bpatch)
            bd.text((W * p // 2, bh2 * p // 2),
                    "LOW BATTERY %d%% - CONNECT CHARGER" % st["pct"],
                    font=_badge_font(16 * p), fill=255, anchor="mm")
            img.paste(bpatch.resize((W, bh2), Image.LANCZOS), (0, 0))

        tmp = dst + ".tmp"
        img.save(tmp, format="PNG")   # tmp 后缀无扩展名,须显式指定格式
        os.replace(tmp, dst)
        return True
    except Exception as exc:
        log("battery overlay failed: %r" % exc)
        return False


def always_cycle():
    """一轮常显刷新:确保 Wi-Fi → 拉图 → 叠加电量角标 → 全屏显示。

    云端原图只进 SS_OUT(锁屏图保持干净,framework 活着时状态栏自有电量),
    eips 显示图由 overlay_battery 从 SS_OUT 现算:即使拉图失败,角标上的
    电量/续航也随每轮更新。无干净源时兜底显示旧显示图,屏面始终有内容。
    """
    t0 = time.time()
    wifi(True)
    net = wait_network()
    pulled = False
    if net:
        pulled = pull_image(outs=(SS_OUT,))
        if not pulled:
            log("cycle: pull failed; keep last image")
    else:
        log("cycle: no network; keep last image")
    shown = None
    if os.path.exists(SS_OUT):
        shown = ALWAYS_OUT if overlay_battery() else SS_OUT
    elif os.path.exists(ALWAYS_OUT):
        shown = ALWAYS_OUT
    if shown:
        show_fullscreen(shown)
    wifi(False)
    cap = _sysfs_raw("battery_capacity") or "?"
    log("cycle done in %ds: net=%s pull=%s battery %s"
        % (time.time() - t0, net, pulled, cap))


def daemonize():
    """双 fork + setsid 脱离会话(设备无 nohup/setsid 命令,在 python 里做)。

    SSH 会话正常关闭、或被周期关 Wi-Fi 强行断连后 sshd 延迟收网,都杀不到
    已 setsid 的守护进程;父进程立即退出,KUAL/SSH 调用秒回。
    调试时设 F1DASH_NO_DAEMON=1 前台跑。
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    if null > 2:
        os.close(null)


def always_on_mode(interval=None):
    """常显主循环:启动即刷一轮,之后每 interval 秒重申防入睡并再刷一轮。

    间隔优先级:--interval 参数 > F1DASH_ALWAYSON_INTERVAL 环境变量 > 默认 3600。
    实际等待超过预期 2 分钟视为发生过挂起(常显模式不该出现),记日志供排查。
    """
    if not interval:
        interval = os.environ.get("F1DASH_ALWAYSON_INTERVAL") or ALWAYS_INTERVAL
    interval = int(interval)
    try:
        with open("/var/run/f1dash_alwayson.pid", "w") as f:
            f.write(str(os.getpid()))
    except Exception as exc:
        log("pidfile write failed: %r" % exc)
    log("always-on started (interval %ds, pid %d)" % (interval, os.getpid()))
    BatteryMeter().start()
    keep_awake(True)
    front_light_off()
    if framework_stop():
        log("framework stopped; screen is ours alone")
    else:
        log("warning: framework stop failed; chrome may repaint over the image")
    always_cycle()
    while True:
        t0 = time.time()
        time.sleep(interval)
        elapsed = time.time() - t0
        if elapsed > interval + 120:
            log("warning: %ds gap in loop — system suspended?" % round(elapsed))
        keep_awake(True)
        front_light_off()
        if framework_ensure_stopped():
            log("framework respawned; stopped it again")
        always_cycle()


class BatteryMeter(threading.Thread):
    """常显内置电池计量:每 BATT_SAMPLE 秒读电芯计量并追加写 CSV 一行。

    常显续航取决于实测放电速率。yoshi 电芯计量的 battery_current 原始单位
    不明,但 battery_mAH/battery_capacity 直接给出荷电状态,用它们随时间的
    衰减算放电速率与续航,不依赖电流单位;插电期间行内 charging=1 供剔除。
    """

    def __init__(self, path=BATT_LOG, sample=BATT_SAMPLE):
        threading.Thread.__init__(self)
        self.daemon = True
        self.path = path
        self.sample = sample
        self._stop = threading.Event()

    def run(self):
        if not os.path.isdir(BATT_SYSFS):
            log("battery meter disabled: no %s" % BATT_SYSFS)
            return
        try:
            if not os.path.exists(self.path):
                with open(self.path, "w") as f:
                    f.write("unix_ts,time,pct,mah,lmd_mah,mv,ua_raw,temp_dC,"
                            "charging\n")
        except Exception as exc:
            log("battery meter disabled: csv %r" % exc)
            return
        # 重启标记:battreport 用它区分"进程重启造成的采样空洞"与"设备挂起"
        try:
            with open(self.path, "a") as f:
                f.write("# restart %d %s\n" % (time.time(),
                                               time.strftime("%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
        while not self._stop.wait(self.sample):
            line = "%d,%s,%s,%s,%s,%s,%s,%s,%d\n" % (
                time.time(), time.strftime("%Y-%m-%d %H:%M:%S"),
                _sysfs_raw("battery_capacity"), _sysfs_raw("battery_mAH"),
                _sysfs_raw("battery_lmd"), _sysfs_raw("battery_voltage"),
                _sysfs_raw("battery_current"),
                _sysfs_raw("battery_temperature"),
                1 if is_charging() else 0)
            try:
                with open(self.path, "a") as f:
                    f.write(line)
            except Exception:
                pass


def batt_rows(path=BATT_LOG):
    """读电池采样 CSV → (rows, restarts);rows 按时间升序,表头/坏行跳过。

    `# restart <ts>` 是 BatteryMeter 进程重启标记(区分采样空洞是重启还是
    挂起);battreport(完整报表)与 battery_status(角标续航)共用本解析。
    """
    rows, restarts = [], []
    try:
        with open(path) as f:
            next(f)
            for line in f:
                if line.startswith("# restart"):
                    try:
                        restarts.append(float(line.split()[2]))
                    except (ValueError, IndexError):
                        pass
                    continue
                p = line.strip().split(",")
                if len(p) != 9:
                    continue
                try:
                    rows.append({"ts": float(p[0]), "t": p[1],
                                 "pct": int(p[2].rstrip("%") or -1),
                                 "mah": float(p[3] or 0),
                                 "lmd": float(p[4] or 0),
                                 "mv": float(p[5] or 0),
                                 "ua": float(p[6] or 0),
                                 "tc": float(p[7] or 0),
                                 "chg": p[8] == "1"})
                except ValueError:
                    continue
    except Exception:
        return [], []
    rows.sort(key=lambda r: r["ts"])
    return rows, restarts


def battery_status(win_h=24):
    """当前电量快照:sysfs 荷电 + BATT_LOG 实测放电速率外推续航(电量角标用)。

    pct/mAh 读 yoshi sysfs 当前真值;续航 = 剩余 mAh ÷ 当前守护进程运行段的
    非充电样本 ΔmAh/Δt(与 battreport 同口径)。只用最后一条 # restart 之后
    的样本:模式切换(测试期/信息牌/常显)必经守护重启,跨段速率口径不一致
    ——framework 活着时的高耗电样本混入会把续航估得离谱。样本不足(<2 行/
    段内 <1h)或速率测不出(gauge 分辨率以下)时 days=None,角标只显示百分比。
    sysfs 缺失(非设备环境)返回 None,调用方跳过角标。
    """
    try:
        pct = int(_sysfs_raw("battery_capacity").rstrip("%"))
    except ValueError:
        return None
    try:
        mah = float(_sysfs_raw("battery_mAH") or 0)
    except ValueError:
        mah = 0.0
    rows, restarts = batt_rows(BATT_LOG)
    now = time.time()
    seg_start = max(now - win_h * 3600, restarts[-1] if restarts else 0.0)
    disch = [r for r in rows if not r["chg"] and r["ts"] >= seg_start]
    days = None
    if len(disch) >= 2:
        span_h = (disch[-1]["ts"] - disch[0]["ts"]) / 3600.0
        used = disch[0]["mah"] - disch[-1]["mah"]
        if span_h >= 1 and used > 0:
            days = disch[-1]["mah"] / (used / span_h) / 24.0
    return {"pct": pct, "mah": mah, "days": days, "charging": is_charging()}


def battreport(path=BATT_LOG):
    """汇总电池采样 CSV:放电速率、平均功率、外推续航,并检测入睡空洞。

    放电速率用 battery_mAH 随时间衰减计算(不依赖电流原始单位,插电行剔除);
    行间空洞 >5 分钟视为发生过挂起(常显模式不该出现),显式列出。
    """
    rows, restarts = batt_rows(path)
    if not rows:
        print("no battery log: %s" % path)
        return 1
    if len(rows) < 2:
        print("not enough samples yet (%d rows)" % len(rows))
        return 1

    print("window: %s .. %s  (%.1f h, %d rows, %d discharging)"
          % (rows[0]["t"], rows[-1]["t"],
             (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0, len(rows),
             sum(1 for r in rows if not r["chg"])))

    gaps = [(rows[i - 1]["ts"], rows[i]["ts"]) for i in range(1, len(rows))
            if rows[i]["ts"] - rows[i - 1]["ts"] > 300]
    slept = False
    for g0, g1 in gaps:
        if any(g0 < rts <= g1 for rts in restarts):
            print("RESTART GAP: %d s — process restarted mid-log, not a suspend"
                  % round(g1 - g0))
        else:
            print("SUSPEND GAP: %d s — device slept, keep-awake failed"
                  % round(g1 - g0))
            slept = True
    if slept:
        return 1

    disch = [r for r in rows if not r["chg"]]
    if len(disch) < 2:
        print("not enough discharging samples (plugged in whole window?)")
        return 1
    span_h = (disch[-1]["ts"] - disch[0]["ts"]) / 3600.0
    used = disch[0]["mah"] - disch[-1]["mah"]
    if span_h <= 0 or used <= 0:
        print("no measurable drain over %.1f h (rate below gauge resolution)"
              % span_h)
        return 0
    rate = used / span_h                       # mAh/h,数值上即 mA
    mv = sum(r["mv"] for r in disch) / len(disch)
    mw = rate * mv / 1000.0
    print("drain: %.2f mAh/h (%.1f mA-equiv) = %.1f mW @ %.0f mV"
          % (rate, rate, mw, mv))
    print("used %.1f mAh; now %s, lmd %.0f mAh (true full capacity)"
          % (used, disch[-1]["pct"], disch[-1]["lmd"]))
    days_empty = disch[-1]["mah"] / rate / 24.0
    lmd = disch[-1]["lmd"] or disch[-1]["mah"]
    days_10 = max(0.0, disch[-1]["mah"] - 0.1 * lmd) / rate / 24.0
    print("projected: %.1f days to empty, %.1f days to 10%% reserve"
          % (days_empty, days_10))
    return 0


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
                                 "circuitName": "Yas Marina Circuit",
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
                      ("7", "Oscar Piastri", "118"), ("8", "Carlos Sainz", "104"),
                      ("9", "Isack Hadjar", "71"), ("10", "Gabriel Bortoleto", "55")],
        "constructors": [("1", "Mercedes", "379"), ("2", "Ferrari", "307"),
                         ("3", "McLaren", "281"), ("4", "Red Bull", "242"),
                         ("5", "Aston Martin", "117"), ("6", "Williams", "48"),
                         ("7", "Alpine", "42"), ("8", "Haas", "21"),
                         ("9", "Racing Bulls", "18"), ("10", "Audi", "12")],
        "last_results": [("1", "Lando Norris", "1:39:56.180"),
                         ("2", "Max Verstappen", "+15.080"),
                         ("3", "Oscar Piastri", "+18.362"),
                         ("4", "Charles Leclerc", "+22.054"),
                         ("5", "George Russell", "Power Unit")],
        "weather": None if err else {
            "city": "HANGZHOU",
            "today": {"high": 21, "low": 16, "text": "Showers", "pop": 100},
            "tomorrow": {"high": 20, "low": 15, "text": "Drizzle", "pop": 18}},
        "barca": None if err else {"opponent": "Villarreal", "venue": "vs",
                                   "when": "Nov 22 21:00", "rank": "1st", "points": 33},
        "trending": None if err else {"repo": "alibaba/open-code-review",
                                      "stars_today": "1,925", "lang": "TypeScript",
                                      "stars_total": "50,985"},
        "progress": None if err else (11, 23),
        "season": None if err else ("2026" if poster else "2026"),
        "poster_sessions": [] if err else poster_sessions,
        "upd": "16:20",
        "upd_stamp": "11-14 16:20",
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
    if mode == "alwayson":
        # 已在运行则不重复启动(防 KUAL/SSH 双开);pid 会被系统回收,须校验属主
        try:
            old = int(open("/var/run/f1dash_alwayson.pid").read().strip())
            if os.path.isdir("/proc/%d" % old):
                with open("/proc/%d/cmdline" % old, "rb") as f:
                    if b"alwayson" in f.read():
                        print("f1dash alwayson already running (pid %d)" % old)
                        return 0
        except Exception:
            pass
        itv = None
        if "--interval" in argv:
            itv = argv[argv.index("--interval") + 1]
        if os.environ.get("F1DASH_NO_DAEMON") != "1":
            daemonize()
        always_on_mode(itv)
        return 0
    if mode == "battreport":
        return battreport()
    if mode == "pull":
        return 0 if pull_screensaver(out=out) else 1
    if mode == "refresh":
        return 0 if refresh(out=out) else 1
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

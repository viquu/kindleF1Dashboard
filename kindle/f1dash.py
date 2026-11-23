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
    f1dash.py telemetry  上报一份遥测 JSON 到 Oracle(电量/拉图/进程,见遥测一节)
    f1dash.py command  拉取并执行 Oracle 上的 command.json(白名单动作,见遥测一节)

架构:渲染已上移到 Oracle 云端(systemd timer 出图 + nginx 静态服务),
Kindle 只做"唤醒 → 下载成图"的瘦客户端,把唤醒刷新从几十秒压到几秒;
云端不可达不做本机渲染,直接保留最后一张屏保图(渲染代码保留,供云端与开发预览复用)。
一键回睡(auto_sleep 标志文件):信息牌模式下唤醒 → 刷新 → 模拟电源键
自动入睡,锁屏永远显示最新图;想正常阅读时在 KUAL 菜单关闭开关。
常显模式(alwayson):设备永不入睡,每小时整点拉云端图 eips 全屏重显示;RTC 无法
从挂起唤醒(见下),常显就是用续航换全天候实时。Wi-Fi 整点硬开、拉图后保持
ALWAYS_WIFI_WINDOW(默认 5 分钟)再硬关 —— 开窗可预期,SSH/运维在 :00-:05 随便
连(2026-11-22 实测:软关 lipc enable 0 会被 wifid ~15s 翻回,形同虚设;硬关
stop wifid + ifconfig down 可靠,off 窗口不回弹,冷启 6s 回连,路由器静态 IP
预留无需等 DHCP;连续 3 轮失败 fail-open 退回 Wi-Fi 常开,宁可多耗电不把屏
死在旧图上;/mnt/us/f1dash/wifi_hold 标志存在时本轮豁免硬关,供长时运维)。
framework 停了屏上看不到状态栏电量,拉图后本地把电量角标(电池图标+百分比+
实测外推续航,低电加顶部警告条)叠进底部白边,不必 SSH 就知道还能撑多久。

说明:本设备(PW1 5.6.1.1)的外部 RTC 闹钟无法从挂起唤醒内核
(sysfs wakealarm 触发但无效,powerd rtcWakeup 不接受外部设置),
故设备休眠时守护进程冻结,无法在睡眠中刷新;F1 数据变化不频繁,
云端出图 + 唤醒即拉已足够实时。
"""

import base64
import calendar
import hashlib
import json
import os
import re
import shutil
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
    """通用 JSON GET(侧栏数据源共用);失败抛异常,由调用方按块兜底。

    必须带 Accept: */* —— Jolpica 在 Cloudflare 后面且 Vary: Accept,urllib
    默认不带 Accept 的请求会命中边缘缓存里粘性的空数据变体(2026-11-22 实测
    /2026/21/results.json 恒返回 total=0,拉斯维加斯赛果整小时不上屏;带
    Accept: */* 或任意新缓存键即恢复。application/json 也是坏档,唯一与源站
    一致的是 */*)。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "f1dash/1.0",
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return json.load(resp)


def fetch(path):
    return fetch_json(API_BASE + path, timeout=30)


# ---------------- 最近一次好数据缓存(TTL + 降级) ----------------
# 规则(Ken 2026-11-23):渲染定时器仍每 10 分钟跑,但每个源只在缓存过期
# (TTL)时才联网拉取;拉取失败用上次成功的数据渲染,既不跳过该区块、也不
# 让整张图失败;失败的拉取永不覆盖缓存。缓存文件与 f1dash.py 同目录
# (云端 /opt/f1dash/f1_cache.json,不在 nginx 静态目录下,不对外公开)。
# TTL 按数据源变化速度定:赛历/积分/赛果/排位/冲刺只在比赛周末变(6h),
# 天气是日频预报(1h),巴萨赛程/榜位变化慢(6h),GitHub Trending 日更(6h)。
# MAX_AGE 是缓存作为"上次好数据"的最长可用期,超期按无缓存处理。
F1_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "f1_cache.json")
_CACHE_TTL_S = {"calendar": 6 * 3600, "standings": 6 * 3600,
                "constructors": 6 * 3600, "last_results": 6 * 3600,
                "qualifying": 6 * 3600, "sprint": 6 * 3600,
                "weather": 3600, "barca": 6 * 3600, "trending": 6 * 3600}
_CACHE_MAX_AGE_S = {"calendar": 7 * 86400, "standings": 7 * 86400,
                    "constructors": 7 * 86400, "last_results": 7 * 86400,
                    "qualifying": 7 * 86400, "sprint": 7 * 86400,
                    "weather": 2 * 86400, "barca": 7 * 86400,
                    "trending": 2 * 86400}
# 缓存键前缀决定组名(图片角标用):排位/冲刺的键是 "qualifying-<赛季>-<轮次>",
# 前缀仍归 F1 组。
_CACHE_GROUP_LABEL = {"calendar": "F1", "standings": "F1",
                      "constructors": "F1", "last_results": "F1",
                      "qualifying": "F1", "sprint": "F1",
                      "weather": "WEATHER", "barca": "BARCA",
                      "trending": "TRENDING"}


def _cache_read():
    try:
        with open(F1_CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cache_entry(key):
    """缓存条目原文(不判年龄);无/损坏返回 None。"""
    try:
        entry = _cache_read().get(key)
        return entry if isinstance(entry, dict) and "data" in entry else None
    except Exception:
        return None


def _cache_put(key, data):
    """仅成功拉取后调用:原子写;写失败不影响渲染,也不动旧缓存。"""
    try:
        cache = _cache_read()
        cache[key] = {"fetched_at": time.time(), "data": data}
        tmp = F1_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, F1_CACHE_FILE)
        return True
    except Exception as exc:
        print("cache write failed: %r" % (exc,)[:60])
        return False


def _cache_date(entry):
    """缓存条目的抓取日期(北京时间 MM-DD),供图片角标展示过期数据。"""
    try:
        bj = time.gmtime(float(entry["fetched_at"]) + 8 * 3600)
        return time.strftime("%m-%d", bj)
    except Exception:
        return "?"


def _cached_fetch(key, fetch_fn, format_fn=None):
    """带 TTL 的缓存拉取,返回 (value, stale_entry)。

    - 缓存未过期(TTL 内):直接用缓存,不联网,stale_entry=None。
    - 缓存过期:联网拉取;成功则更新缓存并返回新值;失败则用未超期
      (MAX_AGE 内)的上次好数据并返回其缓存条目(调用方据此记 stale)。
    - 都失败返回 (None, None)。失败的拉取永不写缓存。
    format_fn 把原始拉取结果整成渲染要用的值;缓存里存的是原始结果,
    所以 TTL 命中与兜底都走同一条 format 路径。
    """
    now = time.time()
    # 排位/冲刺的键带赛季-轮次后缀("qualifying-2026-23"),TTL/MAX_AGE 按前缀取。
    ttl_key = key.split("-")[0]
    entry = _cache_entry(key)
    if entry is not None:
        try:
            age = now - float(entry.get("fetched_at", 0))
        except Exception:
            age = None
        if age is not None and 0 <= age <= _CACHE_TTL_S.get(ttl_key, 3600):
            try:
                raw = entry["data"]
                return (format_fn(raw) if format_fn else raw), None
            except Exception:
                entry = None  # 缓存损坏:视为无缓存,走联网拉取
    try:
        raw = fetch_fn()
        value = format_fn(raw) if format_fn else raw
    except Exception as exc:
        if entry is not None:
            try:
                age = now - float(entry.get("fetched_at", 0))
            except Exception:
                age = None
            if age is not None and 0 <= age <= _CACHE_MAX_AGE_S.get(ttl_key, 7 * 86400):
                try:
                    raw = entry["data"]
                    return (format_fn(raw) if format_fn else raw), entry
                except Exception:
                    pass
        print("%s fetch failed, no usable cache: %r" % (key, repr(exc)[:60]))
        return None, None
    _cache_put(key, raw)
    return value, None


def _mark_stale(out, key, entry):
    """把一次缓存兜底记到 out["stale"]:组名 → (最旧那份缓存的 epoch, 日期)。"""
    label = _CACHE_GROUP_LABEL.get(key.split("-")[0], key.upper())
    try:
        fetched = float(entry.get("fetched_at", 0))
    except Exception:
        return
    prev = out["stale"].get(label)
    if prev is None or fetched < prev[0]:
        out["stale"][label] = (fetched, _cache_date(entry))


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
    """海报结果块列表 [{key,label,rows}...] + 最旧的一份缓存兜底条目(无则 None)。

    仅拉已结束会话,每块独立容错:拉取失败用上次好数据(TTL 缓存,键按
    "会话-赛季-轮次"),无可用缓存才空 rows、跳过该块。返回 (blocks, stale_entry)。
    """
    blocks = []
    stale_entry = None
    for key, label, _ in completed_sessions(next_race, now):
        cache_key = "%s-%s-%s" % (key, season, next_race["round"])
        if key == "qualifying":
            def _fmt(res):
                arr = res["MRData"]["RaceTable"]["Races"][0]["QualifyingResults"]
                return fmt_qualifying(arr)
            fetch_fn = lambda: fetch("%s/%s/qualifying.json" % (season, next_race["round"]))
        elif key == "sprint":
            def _fmt(res):
                arr = res["MRData"]["RaceTable"]["Races"][0]["SprintResults"]
                return fmt_sprint(arr)
            fetch_fn = lambda: fetch("%s/%s/sprint.json" % (season, next_race["round"]))
        else:
            continue
        rows, entry = _cached_fetch(cache_key, fetch_fn, _fmt)
        if entry is not None:
            if (stale_entry is None
                    or float(entry.get("fetched_at", 0)) < float(stale_entry.get("fetched_at", 0))):
                stale_entry = entry
        if rows:
            blocks.append({"key": key, "label": label, "rows": rows})
    return blocks, stale_entry


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

# WMO 天气码 → 中文短语(床头屏用;仪表盘沿用英文 WMO_TEXT)
WMO_CN = {0: "晴", 1: "多云", 2: "多云", 3: "阴",
          45: "雾", 48: "雾",
          51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
          56: "冻毛毛雨", 57: "冻毛毛雨",
          61: "小雨", 63: "雨", 65: "大雨",
          66: "冻雨", 67: "冻雨",
          71: "小雪", 73: "雪", 75: "大雪", 77: "雪",
          80: "阵雨", 81: "阵雨", 82: "大阵雨",
          85: "阵雪", 86: "阵雪",
          95: "雷阵雨", 96: "雷阵雨", 99: "雷阵雨"}


def _wx_cn(day):
    """一天预报的中文天气词(优先 WMO 码查表,缺码用英文原文兜底)。"""
    if not day:
        return "—"
    return WMO_CN.get(day.get("code"), day.get("text") or "—")


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


_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _hhmm(iso):
    """'2026-11-23T06:30' → '06:30';解析失败返回 None。"""
    try:
        return str(iso)[11:16]
    except Exception:
        return None


def get_weather():
    """Hangzhou 每日预报(当日高低温/天气/降水概率 + 明日概要 + 未来 7 天),Open-Meteo。

    只取 daily 日值,不取 current——屏保一挂一整天,当前气温几分钟后就失真。
    forecast_days=7:晨报要明日预告、晚报要后天预告、周末屏要周六/周日/
    下周一三天,2 天不够。旧缓存(2 天、无 days/humidity 字段)仍能读:
    days 按实际天数生成,缺失字段用 None 兜底,渲染时跳过。
    """
    q = WEATHER_CITY
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&daily=temperature_2m_max,temperature_2m_min,weather_code,"
           "precipitation_probability_max,precipitation_probability_min,"
           "relative_humidity_2m_max,"
           "sunrise,sunset&timezone=%s&forecast_days=7"
           % (q["lat"], q["lon"], urllib.parse.quote(q["tz"])))
    daily = fetch_json(url)["daily"]

    def day(i):
        try:
            d = {"high": round(daily["temperature_2m_max"][i]),
                 "low": round(daily["temperature_2m_min"][i]),
                 "text": WMO_TEXT.get(int(daily["weather_code"][i]), "—"),
                 "pop": int(daily["precipitation_probability_max"][i] or 0),
                 "code": int(daily["weather_code"][i])}
        except Exception:
            return None
        try:
            d["pop_min"] = int(daily["precipitation_probability_min"][i] or 0)
        except Exception:
            d["pop_min"] = None
        try:
            d["date"] = str(daily["time"][i])
            d["weekday"] = time.strptime(d["date"], "%Y-%m-%d").tm_wday
        except Exception:
            d["date"], d["weekday"] = None, None
        try:
            d["humidity"] = int(daily["relative_humidity_2m_max"][i])
        except Exception:
            d["humidity"] = None
        try:
            d["sunrise"] = _hhmm(daily["sunrise"][i])
            d["sunset"] = _hhmm(daily["sunset"][i])
        except Exception:
            d["sunrise"], d["sunset"] = None, None
        return d

    days = [d for d in (day(i) for i in range(7)) if d is not None]
    out = {"city": q["name"], "days": days}
    out["today"] = days[0] if days else None
    out["tomorrow"] = days[1] if len(days) > 1 else None
    return out


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
    """拉取并整理数据;每个源走 _cached_fetch(TTL 缓存 + 失败用上次好数据)。

    赛历仍是主获取,但不再是单点失败:拉取失败且无可用缓存时才置 err、
    整张图保留上一张;有缓存时用缓存渲染并记 stale。积分/赛果/资讯盒
    各自容错,失败时用缓存,无缓存才留空。out["stale"] 记组名 → (最旧
    缓存 epoch, 抓取日期 MM-DD),渲染时在页脚/区块标题处展示。
    """
    # Kindle 时区未设置(按 UTC 走),更新时间按北京时间(UTC+8)显示
    upd = time.gmtime(time.time() + 8 * 3600)
    out = {"next_race": None, "last_race": None, "standings": [],
           "constructors": [], "last_results": [],
           "progress": None, "season": None, "poster_sessions": [],
           "weather": None, "barca": None, "trending": None,
           "upd": time.strftime("%H:%M", upd), "upd_stamp": time.strftime("%m-%d %H:%M", upd),
           "err": None, "stale": {}}
    cal, entry = _cached_fetch("calendar", lambda: fetch("current.json"))
    if cal is None:
        out["err"] = RuntimeError("calendar fetch failed, no usable cache")
        return out
    if entry is not None:
        _mark_stale(out, "calendar", entry)
    rt = cal["MRData"]["RaceTable"]
    season = rt["season"]
    races = rt["Races"]
    out["next_race"] = nr = find_next_race(races)
    out["last_race"] = lr = find_last_race(races)
    out["season"] = season
    if nr is not None:
        # 仅赛事周末:拉已结束会话的排位/冲刺赛结果,供海报展示
        if race_poster_active(out):
            out["poster_sessions"], entry = weekend_poster_sessions(nr, season)
            if entry is not None:
                _mark_stale(out, "qualifying", entry)
    out["progress"] = season_progress(races, nr, lr)

    # 用赛季号派生路径(而非 current):1 月跨赛季窗口时 current/24/results 会 404,
    # 2026/24/results 永远可查
    def _standings_fmt(stand):
        drivers = stand["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]
        return fmt_standings(drivers)

    val, entry = _cached_fetch(
        "standings", lambda: fetch(season + "/driverstandings.json"),
        _standings_fmt)
    if val is not None:
        out["standings"] = val
    if entry is not None:
        _mark_stale(out, "standings", entry)

    def _constructors_fmt(cons):
        cs = cons["MRData"]["StandingsTable"]["StandingsLists"][0]["ConstructorStandings"]
        return fmt_constructors(cs)

    val, entry = _cached_fetch(
        "constructors", lambda: fetch(season + "/constructorstandings.json"),
        _constructors_fmt)
    if val is not None:
        out["constructors"] = val
    if entry is not None:
        _mark_stale(out, "constructors", entry)

    def _results_raw():
        if lr is not None:
            return fetch("%s/%s/results.json" % (season, lr["round"]))
        # 休赛期:赛历已翻到次年,回退上一赛季收官战
        return fetch(str(int(season) - 1) + "/last/results.json")

    def _results_fmt(res):
        races_res = res["MRData"]["RaceTable"]["Races"]
        if races_res:
            return fmt_results(races_res[0]["Results"])
        return []

    val, entry = _cached_fetch("last_results", _results_raw, _results_fmt)
    if val is not None:
        out["last_results"] = val
    if entry is not None:
        _mark_stale(out, "last_results", entry)

    # 底部资讯盒三块(天气/巴萨/GitHub Trending):同样走缓存,失败用上次好数据
    for key, getter in (("weather", get_weather), ("barca", get_barca),
                        ("trending", get_trending)):
        val, entry = _cached_fetch(key, getter)
        if val is not None:
            out[key] = val
        if entry is not None:
            _mark_stale(out, key, entry)
    return out


# ---------------- 床头屏(晨报 / 晚报 / 周末,设计稿 v2 无日程) ----------------
# 新闻纯 Muse 生成,不做 RSS 兜底:云端只读 headlines.json(Muse 每天
# 06:30/18:00 生成并 scp 到 /opt/f1dash/out/)。缺失/过期时新闻块只显示
# 无数据,其余块正常出图。收盘/模拟盘/本周回顾等钱数绝不写死在代码里,
# 只读 Muse 提供的 briefing.json(没有就留 TODO 占位,见 read_briefing)。

def _read_json_file(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_headlines(path):
    """读 headlines.json,返回 {"morning": [...], "evening": [...],
    "fresh_morning": bool, "fresh_evening": bool, "updated": str}。

    格式 {"updated": "2026-11-23T06:30:00+08:00", "date": "2026-11-23",
    "morning": [5 条], "evening": [4 条]};每条 ≤20 字、无 emoji。
    新鲜度按 updated 的年龄算:晨报 >6h、晚报 >12h 即过期;文件缺失、
    损坏、updated 解析失败一律按过期处理(新闻块显示无数据)。
    """
    out = {"morning": [], "evening": [], "fresh_morning": False,
           "fresh_evening": False, "updated": None}
    raw = _read_json_file(path) if path else {}
    if not raw:
        return out
    out["updated"] = raw.get("updated")
    for key in ("morning", "evening"):
        val = raw.get(key)
        if isinstance(val, list):
            out[key] = [str(s)[:40] for s in val if s]
    try:
        upd = time.strptime(str(raw["updated"])[:19], "%Y-%m-%dT%H:%M:%S")
        age = time.time() - (calendar.timegm(upd) - 8 * 3600)
    except Exception:
        return out
    if 0 <= age <= 6 * 3600 and out["morning"]:
        out["fresh_morning"] = True
    if 0 <= age <= 12 * 3600 and out["evening"]:
        out["fresh_evening"] = True
    return out


def read_briefing(path):
    """读 Muse 提供的 briefing.json(可选补充数据),没有/损坏返回 {}。

    字段全是可选的,缺哪个渲染时就留 TODO 占位,绝不把钱数写死在代码里:
    {"polymarket": {"cash": "$1,022.51", "pnl": "+$172.51"},
     "weekly": {"total": 569990.49, "pnl": -18705.94, "pct": -3.23,
                "date": "2026-11-21", "note": "中证500周跌7.11%"},
     "close": {"510500": {"price": 6.053, "chg": -3.71, "date": "11-20"},
               "grid_note": "中证500草稿待定·周一开盘确认"},
     "tonight": "F1 Verstappen夺冠已结算 · 巴萨21:00开球"}
    """
    return _read_json_file(path) if path else {}


def _beijing_now(now=None):
    """北京时间 struct_time;now 为 epoch 秒(测试用),缺省取当前时间。"""
    return time.gmtime((time.time() if now is None else now) + 8 * 3600)


def select_briefing_template(now=None):
    """按北京时间 + 星期几选模板(设计稿 §1)。

    工作日 06:35–09:00 晨报;工作日 18:00–23:00 晚报;周五 18:00 起及
    周末 07:00–23:00 周末屏;其余时段(白天工作日不在家、深夜)保留原
    仪表盘/海报,不刷床头屏。
    """
    t = _beijing_now(now)
    wd, hhmm = t.tm_wday, t.tm_hour * 60 + t.tm_min
    if wd >= 5:  # 周六/周日
        return "weekend" if 7 * 60 <= hhmm < 23 * 60 else "dashboard"
    if wd == 4 and hhmm >= 18 * 60:  # 周五 18:00 起
        return "weekend"
    if 6 * 60 + 35 <= hhmm < 9 * 60:
        return "morning"
    if 18 * 60 <= hhmm < 23 * 60:
        return "evening"
    return "dashboard"


def get_briefing_data(headlines_path=None, briefing_path=None, now=None):
    """床头屏数据:get_dashboard_data() + 头条 + 补充数据 + 模板选择。

    err 不为 None 时 render.py 沿用旧逻辑(保留上一张图,exit 1)——
    但床头屏(morning/evening/weekend)例外:它们不依赖 F1 赛历,赛历
    拉不到且无缓存时也出图。此时补拉天气/巴萨/Trending(各自走缓存容错),
    F1 相关块留空或显示无数据;仪表盘/海报仍保留旧逻辑(赛历是主数据)。
    """
    out = get_dashboard_data()
    out["template"] = select_briefing_template(now)
    if out["err"] is not None and out["template"] in ("morning", "evening", "weekend"):
        # 首跑空缓存 + Jolpica 挂了也会出图:只丢 F1 块,不丢整张屏。
        for key, getter in (("weather", get_weather), ("barca", get_barca),
                            ("trending", get_trending)):
            val, entry = _cached_fetch(key, getter)
            if val is not None:
                out[key] = val
            if entry is not None:
                _mark_stale(out, key, entry)
        out["err"] = None
    out["headlines"] = read_headlines(headlines_path)
    out["briefing"] = read_briefing(briefing_path)
    out["now_bj"] = _beijing_now(now)
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


def _standings_rows(d, font, text, x0, x1, rows, y0, step=28):
    """积分榜 Top 5:名次 + 大写名 + 点线引导 + 右对齐积分(全黑,报纸语法)。"""
    ry = y0
    for pos, name, val in rows[:5]:
        name, val = str(name), str(val)
        text(x0, ry, str(pos), font(True, 13), anchor="lm")
        text(x0 + 34, ry, name, font(True, 17), anchor="lm")
        nw = d.textlength(name, font=font(True, 17)) / (FULL_SCALE * AA_SCALE)
        pw = d.textlength(val, font=font(True, 17)) / (FULL_SCALE * AA_SCALE)
        _dots(d, x0 + 34 + nw + 8, x1 - 8 - pw - 8, ry)
        text(x1 - 8, ry, val, font(True, 17), anchor="rm")
        ry += step


def _info_box(d, font, text, x0, x1, title, lines, y0=620, y1=778):
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


def _draw_info_boxes(d, data, font, text):
    """底部三资讯盒:每日天气 / 巴萨下一场 / GitHub Trending(数据已各自容错)。

    数据过期时盒内末行附 "data as of MM-DD"(盒内最多 3 行正文,第 4 行
    738px 处仍在盒内 778px 底边之上);无数据时不附(避免无中生有)。
    """
    w, b, t = data.get("weather"), data.get("barca"), data.get("trending")
    stale = data.get("stale") or {}

    wlines = []
    if w:
        today, tom = w["today"], w["tomorrow"]
        rain = " %d%%" % today["pop"] if today["pop"] >= 30 else ""
        wlines = ["High %d°C  Low %d°C" % (today["high"], today["low"]),
                  "%s%s" % (today["text"], rain),
                  "Tomorrow %s %d°C" % (tom["text"], tom["high"])]
        if stale.get("WEATHER"):
            wlines.append("data as of %s" % stale["WEATHER"][1])
    _info_box(d, font, text, 28, 206, "%s TODAY" % (w["city"] if w else "WEATHER"),
              wlines)

    blines = []
    if b:
        if b["opponent"]:
            blines.append("Next %s %s" % (b["venue"] or "vs", b["opponent"]))
        if b["when"]:
            blines.append(b["when"])
        if b["rank"]:
            blines.append("LaLiga %s · %d points" % (b["rank"], b["points"]))
        if stale.get("BARCA"):
            blines.append("data as of %s" % stale["BARCA"][1])
    _info_box(d, font, text, 218, 392, "FC BARCELONA", blines)

    tlines = []
    if t:
        tlines.append(_ellipsize(d, font, "#1 %s" % t["repo"], 148, 12, bold=True))
        tlines.append("%s stars today" % t["stars_today"])
        if t["lang"]:
            tlines.append("trending in %s" % t["lang"])
        if stale.get("TRENDING"):
            tlines.append("data as of %s" % stale["TRENDING"][1])
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
    _text_tracked(d, font, 54, 14, data["upd_stamp"], 9, bold=True,
                  anchor="ma", track=1)

    # 双积分榜 Top 5:标签牌 + 行列(无外框,点线引导)
    # F1 数据过期时标签后附抓取日期(如 "· 11-20"),_fit 会缩字适配(最小 9)。
    f1_stale = (data.get("stale") or {}).get("F1")
    for x0, x1, label, rows in (
            (28, 292, "DRIVER STANDINGS — TOP 5",
             [(p, surname(n), v) for p, n, v in data["standings"]]),
            (308, 572, "CONSTRUCTOR STANDINGS — TOP 5",
             [(p, n.upper(), v) for p, n, v in data["constructors"]])):
        if f1_stale:
            label = "%s · %s" % (label, f1_stale[1])
        d.rectangle([fs(x0), fy(84), fs(x1) - 1, fy(114) - 1], outline=0, width=lw(2))
        text((x0 + x1) / 2, 99, label,
             font(True, _fit(d, font, label, x1 - x0 - 16, 13, min_size=9)),
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
                      fill=0, anchor="ma", track=1)

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
                  15, fill=0, track=2)
    _season_ruler(d, 400, 520, 701, done, total, tick_w=3, tick_h=8)
    text(572, 698, "%d/%d" % (done, total), font(False, 14), fill=0, anchor="ra")
    d.line([fs(28), fy(768), fs(572), fy(768)], fill=GRAY_SOFT, width=lw(1))
    # 页脚:渲染时间 + 过期数据源角标(如 "· F1 11-20",即该组数据截至 11-20);
    # 按实测宽度截断,超 544 设计区宽则不再加组(无过期数据时就是原来的行)。
    line = "LAST UPDATE %s" % data["upd"]
    stale = data.get("stale") or {}
    for label in ("F1", "WEATHER", "BARCA", "TRENDING"):
        if label not in stale:
            continue
        cand = "%s · %s %s" % (line, label, stale[label][1])
        w = (sum(d.textlength(ch, font=font(False, 12)) for ch in cand)
             / (FULL_SCALE * AA_SCALE) + 1 * len(cand))
        if w <= 544:
            line = cand
        else:
            break
    _text_tracked(d, font, 28, 776, line, 12, fill=0, track=1)


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
        _text_tracked(d, font, 300, 497, "ROUND", 12, fill=255, anchor="ma", track=4)
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
        # lw(1) 细线经 4x→1x 缩回后常跨两个输出行,LANCZOS 摊薄到 ~127 灰会被
        # 1-bit 阈值吃掉( QUALIFYING 线实测消失);lw(2) 与资讯盒分隔线同宽,必现。
        d.line([fs(48), fy(y + 24), fs(552), fy(y + 24)], fill=255, width=lw(2))
        yy = y + 38
        for pos, name, val in b["rows"]:
            lead = pos == "1"
            # 灰阶(180/210/240)在 e-ink 上量化发虚,一律纯白,P1 层级靠加粗区分
            text(48, yy, pos, font(lead, 15), fill=255, anchor="lm")
            text(84, yy, surname(name), font(lead, 17), fill=255, anchor="lm")
            text(552, yy, val, font(lead, 15), fill=255, anchor="rm")
            yy += 28
        y = yy + 16

    _poster_footer(d, data, font, text, nr)


def _cjk_candidates(bold):
    """中文字体候选:Noto Sans CJK SC(简体中文面,TTC 里 index=2)。

    仓库 Barlow/Futura/Jost 都没有 CJK 字形,中文标题/头条必须走这套。
    Oracle 上由 deploy.sh 从 notofonts/noto-cjk 下载到 /opt/f1dash/fonts/;
    开发机用系统字体;都没有时 cjk() 返回 None,ctext 退回 Barlow(中文
    会缺字,测试会显式断言 CJK 可用)。
    """
    name = "NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc"
    return [os.path.join(_FONT_DIR, name),
            os.path.join(_CLOUD_FONT_DIR, name),
            os.path.join("/opt/f1dash/fonts", name),
            "/usr/share/fonts/opentype/noto/" + name]


def _fit_cjk(d, cjk, s, max_w, start, bold=True, min_size=12):
    """中文动态缩字直到宽度 <= max_w(设计区坐标);无 CJK 字体时返回 start。"""
    if cjk(bold, start) is None:
        return start
    size = start
    while size > min_size and d.textlength(s, font=cjk(bold, size)) / (FULL_SCALE * AA_SCALE) > max_w:
        size -= 1
    return size


def _cjk_ellipsize(d, cjk, s, max_w, size, bold=True):
    """中文按设计区宽度截断(越界加 '…');无 CJK 字体时原样返回。"""
    if cjk(bold, size) is None:
        return s
    if d.textlength(s, font=cjk(bold, size)) / (FULL_SCALE * AA_SCALE) <= max_w:
        return s
    while s and d.textlength(s + "…", font=cjk(bold, size)) / (FULL_SCALE * AA_SCALE) > max_w:
        s = s[:-1]
    return s + "…"


def _new_canvas():
    """超采样画布 + 字体闭包(font/text 沿用旧逻辑,新增 cjk/ctext 画中文)。

    返回 (img, d, font, text, cjk, ctext)。ctext 用 Noto Sans CJK SC
    画整串(含串里的拉丁/数字/°C,中文字体自带这些字形);无 CJK 字体时
    退回 Barlow(中文缺字,仅作兜底不崩溃)。
    """
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

    def cjk(bold, size):
        for path in _cjk_candidates(bold):
            try:
                # TTC 里简体中文面 index=2(JP 0 / KR 1 / SC 2 / TC 3 / HK 4)
                return ImageFont.truetype(path, max(1, round(size * FULL_SCALE * AA_SCALE)),
                                         index=2)
            except Exception:
                continue
        return None

    def text(x, y, s, f, fill=0, anchor="la", **kw):
        # kw 透传 Pillow 原生参数(如 stroke_width/stroke_fill 描边字)
        d.text((fs(x), fy(y)), s, font=f, fill=fill, anchor=anchor, **kw)

    def ctext(x, y, s, size, bold=False, fill=0, anchor="la"):
        f = cjk(bold, size)
        if f is None:
            f = font(bold, size)
        d.text((fs(x), fy(y)), s, font=f, fill=fill, anchor=anchor)

    return img, d, font, text, cjk, ctext


def _finish_canvas(img, out):
    """超采样缩回 + 两端钳位 + 落盘(与旧 png_render 完全一致的输出处理)。"""
    img = img.resize(SS_SIZE, Image.LANCZOS)
    # 两端钳位:硬阈值(全拍纯黑白)把 AA 边缘像素也拍成锯齿,实测字体锯齿过重;
    # 改为笔画芯(<64)拍纯黑防发虚、背景(>160)拍纯白,64-160 的 1px AA 过渡带
    # 保留中深灰阶 —— 深灰在 e-ink 量化稳定(发虚的是 180-240 浅灰,已被排除),
    # 电量角标同为灰度直出,屏上边缘实测平滑。出图仍 758x1024 8-bit 灰度。
    img = img.point(lambda p: 0 if p < 64 else (255 if p > 160 else p))
    img.save(out)
    print("saved:", out, img.size, img.mode)
    return True


def png_render(data, out=SS_OUT):
    # 主数据源(current.json)拉取失败时不覆盖屏保图,保留上一张成功的画面;
    # 仅当显式 --out 指向调试路径时才允许把失败态渲染出来。
    if data["err"] is not None and out == SS_OUT:
        print("fetch failed; keeping previous screensaver:", repr(data["err"])[:80])
        return False

    img, d, font, text, _cjk, _ctext = _new_canvas()

    # 赛事周末(距下一场开赛 <3 天)切海报,否则默认仪表盘
    if race_poster_active(data):
        _draw_poster(d, data, font, text)
    else:
        _draw_dashboard(d, data, font, text)

    return _finish_canvas(img, out)


# ---------------- 床头屏渲染(晨报 / 晚报 / 周末,设计稿 v2 无日程) ----------------
# 全部在 600x800 设计区画,fs/fy 与旧版式同一套坐标;状态栏右侧留空,
# Kindle 端 overlay_battery 会在那里叠加电量角标(云端不知道电量)。

def _bj_date_cn(now_bj):
    """北京时间 struct_time → '11月23日 星期一'。"""
    return "%d月%d日 %s" % (now_bj.tm_mon, now_bj.tm_mday,
                          _WEEKDAY_CN[now_bj.tm_wday])


def _clothing_tip(day):
    """穿衣提示(规则来自气温/降雨概率,纯中文):"""
    if not day:
        return None
    tips = []
    if day["low"] <= 10:
        tips.append("早晚凉，午间暖，出门加外套")
    elif day["high"] - day["low"] >= 10:
        tips.append("昼夜温差大，注意添衣")
    if day["pop"] >= 50:
        tips.append("降雨概率高，记得带伞")
    return "；".join(tips) if tips else "气温适宜"


def _commute_line(day):
    """通勤黑条文案:早上看白天降雨概率(pop_min),晚上看全天最高(pop)。

    日最高概率常是夜里那阵雨,用它决定早上会误判成开车(设计稿示例:
    白天 20% 骑车去、夜间 80% 返程有雨)。
    """
    if not day:
        return "早上骑 Ninebot 去 · 晚上返程"
    day_pop = day.get("pop_min") if day.get("pop_min") is not None else day["pop"]
    morning = "早上骑 Ninebot 去" if day_pop < 50 else "早上开车去"
    evening = "晚上返程有雨" if day["pop"] >= 50 else "晚上返程无雨"
    return "%s · %s" % (morning, evening)


def _weather_day(data, date_str):
    """按 'YYYY-MM-DD' 在 weather.days 里找一天;找不到返回 None。"""
    for dd in ((data.get("weather") or {}).get("days") or []):
        if dd.get("date") == date_str:
            return dd
    return None


def _brief_header(d, font, text, ctext, cjk, now_bj, title_en, upd_hhmm):
    """床头屏页眉:左中文日期、右英文标题、第二行城市 + 更新时间,下划线。"""
    ctext(28, 22, _bj_date_cn(now_bj), 20, bold=True)
    text(572, 24, title_en, font(True, 15), anchor="ra")
    text(28, 52, "HANGZHOU", font(False, 11))
    ctext(572, 52, "更新 %s" % upd_hhmm, 11, anchor="ra")
    d.line([fs(28), fy(78), fs(572), fy(78)], fill=0, width=lw(2))


def _status_bar(d, font, text, label):
    """状态栏:分隔线 + 左侧小字(模板名·更新时间);右侧留空给电量角标。"""
    d.line([fs(28), fy(758), fs(572), fy(758)], fill=0, width=lw(2))
    text(28, 766, label, font(False, 11))


def _black_bar(d, ctext, cjk, y, s, h=44):
    """通勤黑条(反色):黑底白字,中文按宽缩字,居中。"""
    d.rectangle([fs(28), fy(y), fs(572) - 1, fy(y + h) - 1], fill=0)
    size = _fit_cjk(d, cjk, s, 544 - 24, 16)
    ctext(300, y + h / 2, s, size, bold=True, fill=255, anchor="mm")


def _brief_box(d, font, ctext, cjk, x0, x1, title, lines, y0, y1):
    """床头屏小盒:中文标题居中 + 下划线 + '- ' 行列(限 3 行,超长截断)。"""
    cx = (x0 + x1) / 2
    d.rectangle([fs(x0), fy(y0), fs(x1) - 1, fy(y1) - 1], outline=0, width=lw(2))
    ctext(cx, y0 + 10, title, 14, bold=True, anchor="ma")
    d.line([fs(x0 + 14), fy(y0 + 28), fs(x1 - 14), fy(y0 + 28)], fill=0, width=lw(2))
    ly = y0 + 42
    for s in (lines or ["—"])[:3]:
        line = _cjk_ellipsize(d, cjk, "- %s" % s, x1 - x0 - 24, 12, bold=True)
        ctext(x0 + 12, ly, line, 12, bold=True)
        ly += 22


def _headline_rows(d, font, text, ctext, cjk, items, y0, step, size=17):
    """头条行列:序号(Barlow 加粗)+ 中文一行,超宽截断;返回末行 y。"""
    y = y0
    for i, s in enumerate(items):
        text(28, y, "%d." % (i + 1), font(True, size))
        line = _cjk_ellipsize(d, cjk, s, 544 - 30, size, bold=True)
        ctext(58, y, line, size, bold=True)
        y += step
    return y


def _section_title(d, font, text, ctext, y, title_cn, title_en):
    """区块标题:中文 18 加粗 + 英文 13,下划线在 y+28。"""
    ctext(28, y, title_cn, 18, bold=True)
    text(28 + 4 + len(title_cn) * 20, y + 4, title_en, font(False, 13))
    d.line([fs(28), fy(y + 28), fs(572), fy(y + 28)], fill=0, width=lw(2))


def _weather_lines(day):
    """一天预报的中文行列(湿度/日出日落缺失时跳过该段)。"""
    if not day:
        return []
    lines = []
    pop = day["pop"]
    if day.get("pop_min") is not None and day["pop_min"] != pop:
        lines.append("降雨概率 %d%%–%d%%" % (day["pop_min"], pop))
    else:
        lines.append("降雨概率 %d%%" % pop)
    sun = []
    if day.get("sunrise"):
        sun.append("日出 %s" % day["sunrise"])
    if day.get("sunset"):
        sun.append("日落 %s" % day["sunset"])
    hum = ("湿度 %d%%" % day["humidity"]) if day.get("humidity") is not None else None
    tail = " · ".join([x for x in ([hum] if hum else []) + sun if x])
    if tail:
        lines.append(tail)
    return lines


def _draw_morning(d, data, font, text, cjk, ctext):
    """晨报屏 v2(设计稿 §2):页眉 70 / 天气 266 / 头条 297 / 底部三联 117 / 状态栏 34。"""
    now_bj = data.get("now_bj") or _beijing_now()
    upd = data.get("upd", "%02d:%02d" % (now_bj.tm_hour, now_bj.tm_min))
    d.rectangle([fs(8), fy(8), fs(592) - 1, fy(792) - 1], outline=0, width=lw(2))
    _brief_header(d, font, text, ctext, cjk, now_bj, "MORNING BRIEFING", upd)

    # 天气 78–344:大字温差 + 天气词 + 降雨/湿度/日出日落 + 穿衣提示 + 明日预告 + 通勤黑条
    w = data.get("weather") or {}
    today, tomorrow = w.get("today"), w.get("tomorrow")
    if today:
        text(28, 92, "%d°–%d°" % (today["low"], today["high"]), font(True, 46))
        ctext(210, 98, _wx_cn(today), 20, bold=True)
        for i, line in enumerate(_weather_lines(today)):
            ctext(210, 130 + i * 26, line, 15)
        tip = _clothing_tip(today)
        if tip:
            ctext(28, 200, tip, 15, bold=True)
    else:
        ctext(28, 92, "天气暂无数据", 16, bold=True)
    if tomorrow:
        # 旧缓存(7 天预报之前)的 tomorrow 没有 date 字段:缺了只不显示日期
        tdate = tomorrow.get("date")
        if tdate:
            label = "明日 %d月%d日 %d°–%d° %s" % (
                int(tdate[5:7]), int(tdate[8:10]),
                tomorrow["low"], tomorrow["high"], _wx_cn(tomorrow))
        else:
            label = "明日 %d°–%d° %s" % (
                tomorrow["low"], tomorrow["high"], _wx_cn(tomorrow))
        ctext(28, 230, label, 15)
    _black_bar(d, ctext, cjk, 286, _commute_line(today))

    # 头条 344–641:5 条 Muse 生成,过期/缺失只显示无数据
    _section_title(d, font, text, ctext, 360, "头条", "HEADLINES")
    hl = data.get("headlines") or {}
    items = hl.get("morning") if hl.get("fresh_morning") else []
    if items:
        _headline_rows(d, font, text, ctext, cjk, items[:5], 404, 48)
    else:
        ctext(28, 404, "头条暂无更新", 16, bold=True)
        text(28, 440, "HEADLINES NOT UPDATED YET", font(False, 12))

    # 底部三联 641–758:F1 下一场 / 巴萨 / 模拟盘(钱数只读 briefing.json)
    br = data.get("briefing") or {}
    nr = data.get("next_race")
    f1_lines = []
    if nr is not None:
        f1_lines.append(nr["raceName"])
        f1_lines.append("%s %s" % (short_date(nr["date"]),
                                   format_race_daytime(nr) or "TBD"))
    if not f1_lines:
        f1_lines = ["F1 数据暂无更新"]
    b = data.get("barca") or {}
    barca_lines = []
    if b.get("opponent"):
        barca_lines.append("%s %s" % (b["venue"] or "vs", b["opponent"]))
    if b.get("when"):
        barca_lines.append(b["when"])
    if b.get("rank"):
        barca_lines.append("LaLiga %s · %d 分" % (b["rank"], b["points"]))
    pm = br.get("polymarket") or {}
    pm_lines = []
    if pm.get("cash"):
        pm_lines.append("现金 %s" % pm["cash"])
    if pm.get("pnl"):
        pm_lines.append("本周 %s" % pm["pnl"])
    if not pm_lines:
        pm_lines = ["模拟盘数据待接入"]
    _brief_box(d, font, ctext, cjk, 28, 206, "F1 下一场", f1_lines, 648, 752)
    _brief_box(d, font, ctext, cjk, 218, 392, "巴萨", barca_lines, 648, 752)
    _brief_box(d, font, ctext, cjk, 404, 572, "模拟盘", pm_lines, 648, 752)

    _status_bar(d, font, text, "MORNING BRIEFING · %s" % upd)


def _draw_evening(d, data, font, text, cjk, ctext):
    """晚报屏 v2(设计稿 §3):页眉 70 / 明日天气 250 / 今日收盘 188 / 头条回顾 219 / 今晚一行 39 / 状态栏 34。"""
    now_bj = data.get("now_bj") or _beijing_now()
    upd = data.get("upd", "%02d:%02d" % (now_bj.tm_hour, now_bj.tm_min))
    d.rectangle([fs(8), fy(8), fs(592) - 1, fy(792) - 1], outline=0, width=lw(2))
    _brief_header(d, font, text, ctext, cjk, now_bj, "EVENING BRIEFING", upd)

    # 明日天气 78–328:明日大字 + 降雨/湿度/日出日落 + 后天预告 + 明早通勤黑条
    w = data.get("weather") or {}
    tomorrow = w.get("tomorrow")
    days = w.get("days") or []
    day_after = days[2] if len(days) > 2 else None
    if tomorrow:
        text(28, 92, "%d°–%d°" % (tomorrow["low"], tomorrow["high"]), font(True, 46))
        ctext(210, 98, _wx_cn(tomorrow), 20, bold=True)
        for i, line in enumerate(_weather_lines(tomorrow)):
            ctext(210, 130 + i * 26, line, 15)
    else:
        ctext(28, 92, "明日天气暂无数据", 16, bold=True)
    if day_after:
        adate = day_after.get("date")
        if adate:
            label = "后天 %d月%d日 %d°–%d° %s，更冷注意保暖" % (
                int(adate[5:7]), int(adate[8:10]),
                day_after["low"], day_after["high"], _wx_cn(day_after))
        else:
            label = "后天 %d°–%d° %s，更冷注意保暖" % (
                day_after["low"], day_after["high"], _wx_cn(day_after))
        ctext(28, 200, label, 15)
    _black_bar(d, ctext, cjk, 270,
               "明早" + _commute_line(tomorrow).replace("早上", "", 1))

    # 今日收盘 328–516:只读 briefing.json,缺了留 TODO 占位,不写死钱数
    _section_title(d, font, text, ctext, 344, "今日收盘", "MARKET CLOSE")
    br = data.get("briefing") or {}
    close = br.get("close") or {}
    lines = []
    c5105 = close.get("510500") or {}
    if c5105.get("price") is not None:
        lines.append("中证500ETF %s %+.2f%%（%s收）"
                     % (c5105["price"], c5105.get("chg", 0),
                        c5105.get("date", "")))
    if close.get("grid_note"):
        lines.append(close["grid_note"])
    pm = br.get("polymarket") or {}
    if pm.get("pnl") and pm.get("cash"):
        lines.append("模拟盘 %s · 现金 %s" % (pm["pnl"], pm["cash"]))
    b = data.get("barca") or {}
    if b.get("opponent") and b.get("when"):
        lines.append("巴萨今晚 %s %s" % (b["when"], b["opponent"]))
    if not lines:
        lines = ["收盘数据待接入", "CLOSE DATA NOT AVAILABLE"]
    y = 388
    for s in lines[:4]:
        ctext(28, y, _cjk_ellipsize(d, cjk, s, 544, 15, bold=True), 15, bold=True)
        y += 30

    # 头条回顾 516–735:晚报 4 条;没有就用晨报前 4 条;都没有显示无数据
    _section_title(d, font, text, ctext, 532, "头条回顾", "RECAP")
    hl = data.get("headlines") or {}
    if hl.get("fresh_evening"):
        items = hl.get("evening") or []
    elif hl.get("fresh_morning"):
        items = (hl.get("morning") or [])[:4]
    else:
        items = []
    if items:
        _headline_rows(d, font, text, ctext, cjk, items[:4], 576, 38, size=16)
    else:
        ctext(28, 576, "头条回顾暂无更新", 16, bold=True)

    # 今晚一行 735–758
    tonight = br.get("tonight")
    if not tonight:
        nr = data.get("next_race")
        tonight = "今晚 F1 %s · 巴萨 %s %s" % (
            nr["raceName"] if nr else "—",
            b.get("when", ""), b.get("opponent", "")) if nr or b.get("opponent") else None
    if tonight:
        ctext(28, 730, _cjk_ellipsize(d, cjk, tonight, 544, 14, bold=True),
              14, bold=True)

    _status_bar(d, font, text, "EVENING BRIEFING · %s" % upd)


def _weekend_dates(now_bj):
    """本周六/周日/下周一的 struct_time(北京时间)。

    周五 18:00 看即将到的周六日;周六看今天/明天;周日看昨天/今天;
    周一–周四(仅 --weekend 强制渲染时走到)看本周六/日。周六偏移:
    周一 +5、周二 +4、周三 +3、周四 +2、周五 +1、周六 0、周日 -1。
    """
    base = calendar.timegm(now_bj) - 8 * 3600
    wd = now_bj.tm_wday
    sat_off = -1 if wd == 6 else 5 - wd
    sat = time.gmtime(base + sat_off * 86400)
    sun = time.gmtime(base + (sat_off + 1) * 86400)
    mon = time.gmtime(base + (sat_off + 2) * 86400)
    return sat, sun, mon


def _draw_weekend(d, data, font, text, cjk, ctext):
    """周末屏 v2(设计稿 §4):页眉 70 / 两天天气 210 / 本周回顾 170 / 周末比赛 150 / 下周一预告 150 / 状态栏 34。"""
    now_bj = data.get("now_bj") or _beijing_now()
    upd = data.get("upd", "%02d:%02d" % (now_bj.tm_hour, now_bj.tm_min))
    d.rectangle([fs(8), fy(8), fs(592) - 1, fy(792) - 1], outline=0, width=lw(2))
    _brief_header(d, font, text, ctext, cjk, now_bj, "WEEKEND", upd)

    sat, sun, mon = _weekend_dates(now_bj)

    def _day_box(x0, x1, day_gmt, label_cn):
        date_str = time.strftime("%Y-%m-%d", day_gmt)
        day = _weather_day(data, date_str)
        d.rectangle([fs(x0), fy(92), fs(x1) - 1, fy(278) - 1], outline=0, width=lw(2))
        ctext((x0 + x1) / 2, 102, "%s %d月%d日" % (label_cn, day_gmt.tm_mon, day_gmt.tm_mday),
              16, bold=True, anchor="ma")
        if not day:
            ctext((x0 + x1) / 2, 150, "天气暂无数据", 14, anchor="ma")
            return
        text((x0 + x1) / 2, 132, "%d°–%d°" % (day["low"], day["high"]),
             font(True, 30), anchor="ma")
        ctext((x0 + x1) / 2, 172, _wx_cn(day), 15, bold=True, anchor="ma")
        ctext((x0 + x1) / 2, 198, "降雨 %d%%" % day["pop"], 13, anchor="ma")
        sun_line = " · ".join([x for x in (
            ("日出 %s" % day["sunrise"]) if day.get("sunrise") else None,
            ("日落 %s" % day["sunset"]) if day.get("sunset") else None) if x])
        if sun_line:
            ctext((x0 + x1) / 2, 220, sun_line, 13, anchor="ma")
        if day["pop"] <= 20 and day.get("code") in (0, 1, 2, 3):
            ctext((x0 + x1) / 2, 244, "适合外出", 13, bold=True, anchor="ma")

    _day_box(28, 292, sat, "周六")
    _day_box(308, 572, sun, "周日")

    # 本周回顾 288–458:只读 briefing.json 的 weekly,缺了留 TODO 占位
    _section_title(d, font, text, ctext, 304, "本周回顾", "WEEKLY REVIEW")
    br = data.get("briefing") or {}
    wk = br.get("weekly") or {}
    lines = []
    if wk.get("total") is not None and wk.get("pnl") is not None:
        lines.append("总资产 ¥%s · 本期 %s%.2f"
                     % ("{:,.2f}".format(wk["total"]),
                        "+" if wk["pnl"] >= 0 else "", wk["pnl"]))
    if wk.get("note"):
        lines.append(wk["note"])
    pm = br.get("polymarket") or {}
    if pm.get("pnl"):
        lines.append("模拟盘本周 %s" % pm["pnl"])
    if not lines:
        lines = ["本周回顾待接入", "WEEKLY REVIEW NOT AVAILABLE"]
    y = 348
    for s in lines[:3]:
        ctext(28, y, _cjk_ellipsize(d, cjk, s, 544, 15, bold=True), 15, bold=True)
        y += 30

    # 周末比赛 458–608:F1 下一场 + 巴萨 + Trending 第一
    _section_title(d, font, text, ctext, 474, "周末比赛", "MATCHES & RACES")
    nr = data.get("next_race")
    b = data.get("barca") or {}
    t = data.get("trending") or {}
    mlines = []
    if nr is not None:
        mlines.append("F1 下一场 %s %s" % (nr["raceName"], short_date(nr["date"])))
    if b.get("opponent"):
        mlines.append("巴萨 %s %s %s" % (b["venue"] or "vs", b["opponent"], b["when"] or ""))
    if t.get("repo"):
        mlines.append("Trending #1 %s" % t["repo"])
    y = 518
    for s in mlines[:3]:
        ctext(28, y, _cjk_ellipsize(d, cjk, s, 544, 15, bold=True), 15, bold=True)
        y += 28

    # 下周一预告 608–758:周一天气 + 通勤 + 开盘确认(briefing 的 grid_note)
    _section_title(d, font, text, ctext, 624, "下周一预告", "MONDAY PREVIEW")
    mon_str = time.strftime("%Y-%m-%d", mon)
    mday = _weather_day(data, mon_str)
    mlines = []
    if mday:
        mlines.append("%d月%d日 %d°–%d° %s" % (mon.tm_mon, mon.tm_mday,
                                             mday["low"], mday["high"], _wx_cn(mday)))
    mlines.append("早上骑 Ninebot、送尧尧上学")
    close = br.get("close") or {}
    if close.get("grid_note"):
        mlines.append(close["grid_note"])
    elif close.get("510500"):
        c5105 = close["510500"]
        mlines.append("周一开盘确认中证500：收 %s" % c5105.get("price", ""))
    y = 668
    for s in mlines[:3]:
        ctext(28, y, _cjk_ellipsize(d, cjk, s, 544, 15, bold=True), 15, bold=True)
        y += 28

    _status_bar(d, font, text, "WEEKEND · %s" % upd)


def png_render_briefing(data, out=SS_OUT):
    """床头屏渲染:按 data["template"] 选晨报/晚报/周末;dashboard 走旧 png_render。

    data["err"] 不为 None 时与旧逻辑一致(只在 --out 调试路径才画)。
    """
    if data.get("err") is not None and out == SS_OUT:
        print("fetch failed; keeping previous screensaver:", repr(data["err"])[:80])
        return False
    template = data.get("template") or "dashboard"
    img, d, font, text, cjk, ctext = _new_canvas()
    if template == "morning":
        _draw_morning(d, data, font, text, cjk, ctext)
    elif template == "evening":
        _draw_evening(d, data, font, text, cjk, ctext)
    elif template == "weekend":
        _draw_weekend(d, data, font, text, cjk, ctext)
    else:
        if race_poster_active(data):
            _draw_poster(d, data, font, text)
        else:
            _draw_dashboard(d, data, font, text)
    return _finish_canvas(img, out)


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
# 与 service 模式互补的信息牌形态:设备永不入睡,每小时整点拉一次成图(Wi-Fi
# 整点开、保持 ALWAYS_WIFI_WINDOW 再硬关,见 wifi()/always_cycle),成图或电量
# 角标有变才 eips 全屏重显示。PW1 的 RTC 闹钟
# 无法从挂起唤醒内核(见文件头),睡着就刷不了;常显等于用续航换全天候实时。
# 前置光始终关闭 —— 常显模式下它是最大的单一耗电源。
# 循环内每轮重申防入睡标志并重试熄灯,防框架重置。

ALWAYS_OUT = "/mnt/us/f1dash/latest.png"   # eips 全屏显示用成图(SS_OUT + 电量角标)
ALWAYS_INTERVAL = 3600                     # 常显刷新周期(秒;循环对齐到周期网格,3600 即每小时整点)
ALWAYS_WIFI_WINDOW = 300                   # 整点开窗保持秒数:拉图后保持 Wi-Fi 供 SSH/运维,到点硬关
WIFI_HOLD_FLAG = "/mnt/us/f1dash/wifi_hold"  # 存在则本轮不硬关(长时运维豁免;删掉即恢复)
BADGE_STRIP_H = 26                         # 底部白边高度(画布物理像素,角标所在)
LOW_BATT_PCT = 15                          # 电量不高于此值时顶部加黑底警告条
BATT_SYSFS = "/sys/devices/system/yoshi_battery/yoshi_battery0"
BATT_LOG = "/mnt/us/f1dash/battery.csv"    # 电池采样 CSV(battreport 消费)
BATT_SAMPLE = 300                          # 采样周期:电芯计量更新缓慢,5 分钟足够,省去无谓的 lipc/sysfs 读与 CSV 追加
FL_SYSFS = "/sys/devices/system/fl_tps6116x/fl_tps6116x0/fl_intensity"
# 前光硬件档位(0-254):唯一能真写 0 的口。cat 返回 'FrontLight(Intensity) = N'
# 非纯整数;lipc flIntensity 0 只是把 UI 档钳到硬件 1(假灭,LED 仍微亮)
WIFI_FAIL_OPEN = 3    # 连续断网/拉图失败达到此轮数 → fail-open,Wi-Fi 保持常开
_wifi_fail_streak = 0  # 连续失败轮计数(首个成功拉图复位,见 always_cycle)


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


def _fl_sysfs_level():
    """读前光硬件档位;节点值是 'FrontLight(Intensity) = N' 非纯整数,取尾部数字。

    缺失/异常返回 None。lipc 读回对硬件状态撒谎(lipc 0 硬件 1),以这里为准。
    """
    try:
        with open(FL_SYSFS) as f:
            m = re.search(r"(\d+)\s*$", f.read())
        return int(m.group(1)) if m else None
    except Exception:
        return None


def front_light_off():
    """熄灭前置光:lipc + sysfs 双层,顺序与时机都不能错(PW1 5.6.1.1 实测)。

    lipc flIntensity 0 不是真灭——powerd 把 UI 0 钳到硬件最低亮档 1(KOReader
    对 KPW1 标 canTurnFrontlightOff=no),lipc 读回 0 而 LED 仍微亮;能写 0 的
    只有 fl_tps6116x sysfs。顺序:先 lipc 后 sysfs(先 sysfs 会被 powerd 落硬件
    时覆回 1),sleep 2 等落定;framework 停机会重套滑块值,故必须在停框架之后
    执行(调用方保证),且每次绘制后再重申。写 0 用裸 "0" 不带换行。读回以
    sysfs 尾部数字为准,未归零记 warning。
    """
    rc_auto = rc_int = -1
    errs = []
    try:
        for prop in ("flAuto", "flIntensity"):
            r = subprocess.run(["lipc-set-prop", "-i", "com.lab126.powerd",
                                prop, "0"], capture_output=True, text=True,
                               timeout=5)
            if prop == "flAuto":
                rc_auto = r.returncode
            else:
                rc_int = r.returncode
            if r.returncode != 0 and r.stderr:
                errs.append("%s: %s" % (prop, r.stderr.strip()))
        time.sleep(2)       # powerd 把 lipc 0 落硬件(钳到 1)后再压 sysfs
        with open(FL_SYSFS, "w") as f:
            f.write("0")    # 裸 0 不带换行;print() 会补换行,不用
    except Exception as exc:
        log("front light off failed: lipc rc=%d/%d %s %r"
            % (rc_auto, rc_int, "; ".join(errs), exc))
        return
    hw = _fl_sysfs_level()
    lip = {}
    for prop in ("flIntensity", "flRawIntensity"):
        try:
            r = subprocess.run(["lipc-get-prop", "com.lab126.powerd", prop],
                               capture_output=True, text=True, timeout=5)
            lip[prop] = r.stdout.strip() if r.returncode == 0 else "n/a"
        except Exception:
            lip[prop] = "n/a"   # flRawIntensity 本机 FW 不存在,按 n/a 记
    if hw == 0:
        log("front light off: hw=0 lipc flIntensity=%s flRawIntensity=%s rc=%d/%d"
            % (lip["flIntensity"], lip["flRawIntensity"], rc_auto, rc_int))
    else:
        log("warning: front light NOT off (hw=%s) lipc flIntensity=%s "
            "flRawIntensity=%s rc=%d/%d %s"
            % (hw, lip["flIntensity"], lip["flRawIntensity"],
               rc_auto, rc_int, "; ".join(errs)))


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
WIFI_JOB = "wifid"                 # Wi-Fi 主控 upstart job(2026-11-22 initctl 实测)
# Wi-Fi 链路(2026-11-22 设备实测):wifid(主控,respawn)按需拉起 wifim
# (驱动 modprobe ar6003)与 wifis(wpa_supplicant)。stop wifid 不会停 wifis
# (supplicant 幸存,但 wlan0 已 down 无关联);start wifid 冷启自带 enable=1
# 并自动关联,静态 IP 预留下 ~6s 拿到默认路由。

def _route_up():
    """默认路由是否在 /proc/net/route(wait_network 与 wifi 硬关验证共用)。"""
    try:
        with open("/proc/net/route") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "00000000":
                    return True
    except Exception:
        pass
    return False


def _wlan_up():
    """wlan0 UP 标志是否还在(ifconfig flags 行);查不了按仍开着处理,让验证
    失败显式告警,而不是静默当成已关。"""
    try:
        out = subprocess.run([IFCONFIG, WLAN_IF], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines():
            flags = line.split()
            if any(tok.startswith("MTU") for tok in flags):
                # flags 行:"UP BROADCAST ... MTU:1500 Metric:1";MTU 带值,
                # 整 token 相等永远匹配不上(2026-11-22 实测踩坑)
                return "UP" in flags
    except Exception:
        pass
    return True


def wifi(on):
    """Wi-Fi 硬开关(2026-11-22 上线;此前 off 分支是空操作)。

    软关无效实测:lipc-set-prop wifid enable 0 后 wifid 自己把属性翻回 1 并
    ~15s 内自动重连,形同虚设。真关必须停 wifid job 本体(见 WIFI_JOB)+ 接口
    down:wifid 停止后 lipc 源消失(get/set 报 lipcErrNoSuchSource),属性不可能
    翻回;off 窗口 60s 实测不回弹,无默认路由、wlan0 无 UP/RUNNING。
    off 序列:lipc enable 0 → stop wifid → ifconfig down(每步记 rc,收尾验证
    路由+接口,不完全即返回 False 交上层告警);已停/源已消失都算目标状态。
    on 序列:ifconfig up → start wifid → lipc enable 1;start 后立刻 lipc set
    会撞源未注册竞态,失败隔 2s 重试共 3 次,全失败也继续——冷启 wifid 自带
    enable=1 会自己回连(实测 6s 拿到路由)。已运行的 job start 报 already
    running(fail-open 常开时每轮都走这里),按成功计。
    """
    if on:
        return _wifi_up()
    return _wifi_down()


def _wifi_down():
    """硬关三层依次执行并验证;返回 True=验证已关(路由与 UP 标志都消失)。"""
    rc_lipc = rc_stop = rc_ifdown = -1
    try:
        r = subprocess.run(["lipc-set-prop", "com.lab126.wifid", "enable", "0"],
                           capture_output=True, timeout=5)
        rc_lipc = r.returncode
    except Exception as exc:
        log("wifi off: lipc enable 0 failed: %r" % exc)
    try:
        r = subprocess.run(["/sbin/stop", WIFI_JOB], capture_output=True, timeout=15)
        rc_stop = r.returncode
        if rc_stop != 0 and (b"Unknown instance" in (r.stderr or b"")
                             or b"Unknown job" in (r.stderr or b"")):
            rc_stop = 0   # 本来就没在跑,正是想要的状态
    except Exception as exc:
        log("wifi off: stop %s failed: %r" % (WIFI_JOB, exc))
    try:
        r = subprocess.run([IFCONFIG, WLAN_IF, "down"], capture_output=True, timeout=5)
        rc_ifdown = r.returncode
    except Exception as exc:
        log("wifi off: ifconfig down failed: %r" % exc)
    route_gone = not _route_up()
    wlan_down = not _wlan_up()
    log("wifi off: rc lipc=%d stop=%d ifdown=%d route_gone=%s wlan_down=%s"
        % (rc_lipc, rc_stop, rc_ifdown, route_gone, wlan_down))
    return route_gone and wlan_down


def _wifi_up():
    """硬开三层依次执行(ifconfig up → start wifid → lipc enable 1)。

    返回 True=start 或 lipc 至少一个成功;拿不到路由由调用方 wait_network +
    重试序列兜底。
    """
    rc_ifup = rc_start = rc_lipc = -1
    try:
        r = subprocess.run([IFCONFIG, WLAN_IF, "up"], capture_output=True, timeout=5)
        rc_ifup = r.returncode
    except Exception as exc:
        log("wifi on: ifconfig up failed: %r" % exc)
    try:
        r = subprocess.run(["/sbin/start", WIFI_JOB], capture_output=True, timeout=15)
        rc_start = r.returncode
        if rc_start != 0 and b"already running" in ((r.stderr or b"") + (r.stdout or b"")):
            rc_start = 0   # fail-open 常开时每轮 start 都会撞上,按成功计
    except Exception as exc:
        log("wifi on: start %s failed: %r" % (WIFI_JOB, exc))
    for _ in range(3):   # start 后 lipc 源注册有竞态(rc=3 NoSuchSource),隔 2s 再试
        try:
            r = subprocess.run(["lipc-set-prop", "com.lab126.wifid", "enable", "1"],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                rc_lipc = 0
                break
            rc_lipc = r.returncode
        except Exception as exc:
            log("wifi on: lipc enable 1 failed: %r" % exc)
            rc_lipc = -1
        time.sleep(2)
    log("wifi on: rc ifup=%d start=%d lipc=%d" % (rc_ifup, rc_start, rc_lipc))
    return rc_start == 0 or rc_lipc == 0


def wait_network(timeout=90):
    """等 Wi-Fi 完成关联并拿到默认路由;超时返回 False。

    默认 90s:硬关后冷启 wifid 要重走驱动→supplicant→关联→DHCP 全流程
    (实测 6s,路由器静态 IP 预留;60s 是软关时代老值,对冷启留足余量)。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _route_up():
            return True
        time.sleep(2)
    return False


def show_fullscreen(path=ALWAYS_OUT, flash=True):
    """eips 显示成图:758x1024 原图无需缩放定位;eips 不在 PATH(/usr/sbin)。

    PW1 实测(2026-11-21 排位赛成图更新后旧图痕迹仍在):`-g -f` 只是
    "full update",并不能整屏翻转清残影;清残影必须用 usage 明示的整屏
    flash `eips -s w=758,h=1024 -f`(以当前 fb 内容闪翻全屏,rc=0 已验证)。
    flash=True(成图有变)先 -g 绘制再补整屏 flash;flash=False(仅电量
    角标变)沿用 -g -f 重绘,不额外闪屏。本机 eips 把第一个非旗标参数当
    图片路径,路径必须紧跟 -g。
    """
    try:
        if flash:
            r = subprocess.run(["/usr/sbin/eips", "-g", path],
                               capture_output=True, timeout=30)
            if r.returncode != 0:
                log("eips rc=%d %s; skip fullscreen flash"
                    % (r.returncode, r.stderr[:60]))
                return
            subprocess.run(["/usr/sbin/eips", "-s", "w=758,h=1024", "-f"],
                           capture_output=True, timeout=30)
        else:
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


def _read_bytes(path):
    """整读文件字节;缺失/异常返回 None(always_cycle 比对成图是否变化用)。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


_last_shown_pct = None   # 上一轮真正刷上屏的角标电量 %(None = 尚未显示过)


# ---------------- dropbear 看门狗(SSH 入站失联自愈) ----------------

DROPBEAR_BIN = "/usr/bin/dropbear"                # usbnet 包软链 → bin/dropbearmulti(libkh5 SSH_DAEMON)
DROPBEAR_PIDFILE = "/mnt/us/usbnet/run/sshd.pid"  # libkh5 SSH_PID
DROPBEAR_OPTS = ["-P", DROPBEAR_PIDFILE, "-K", "15"]  # libkh5 SSH_DAEMON_OPTS
# 本机 usbnet config 为 USE_WIFI=true / QUIET_DROPBEAR=false / USE_OPENSSH=false,
# 不再附加 -n/-b,故上述三项即开机 usbnet 脚本启动 dropbear 的完整参数。
START_STOP_DAEMON = "/sbin/start-stop-daemon"


def _dropbear_listener_pid():
    """活着的 dropbear 监听进程 pid(按 pidfile 校验);没有则 None。

    不能扫 /proc 找 dropbear:监听进程与每个 SSH 连接子进程的 cmdline 完全
    相同,监听死后旧连接子进程仍活着,会误判成"服务还在"——这正是 SSH
    "连不上但会话在"的病灶。监听进程是 pidfile 的唯一写入者,按
    libkh5/start-stop-daemon 同款机制校验:pid 存在且 cmdline 是 dropbear
    才算活(顺带排除 pid 复用)。
    """
    try:
        with open(DROPBEAR_PIDFILE) as f:
            pid = int(f.read().strip())
    except Exception:
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().replace(b"\0", b" ")
    except Exception:
        return None
    return pid if b"dropbear" in cmd else None


def dropbear_watchdog():
    """每小时巡检 dropbear(SSH 入站):活着只记一行,死了按 usbnet 原装方式拉起。

    背景:常显模式下出站拉图正常、SSH 数小时后失联,dropbear 被杀(OOM/框架
    重启误伤)无从知晓,而 KUAL 在常显下不可用,只能由本守护自查自愈。
    重启命令与 /mnt/us/usbnet/bin/usbnetwork 对 sshd 的启动完全一致
    (start-stop-daemon -q -p <pidfile> -x /usr/bin/dropbear -S -- -P <pidfile>
    -K 15);不跑整个 usbnetwork,它会切换 USB 网络模式并重配 Wi-Fi,副作用太大。
    """
    pid = _dropbear_listener_pid()
    if pid:
        log("dropbear alive (pid %d)" % pid)
        return
    try:   # 残留死 pid 的 pidfile:usbnet 脚本同款处理,清掉再拉起
        os.remove(DROPBEAR_PIDFILE)
    except OSError:
        pass
    if not os.access(DROPBEAR_BIN, os.X_OK):
        try:   # usbnetwork 同款兜底(软链目标在 /mnt/us,权限可能随包丢失)
            os.chmod(DROPBEAR_BIN, 0o755)
        except Exception as exc:
            log("dropbear chmod failed: %r" % exc)
    try:
        subprocess.run([START_STOP_DAEMON, "-q", "-p", DROPBEAR_PIDFILE,
                        "-x", DROPBEAR_BIN, "-S", "--"] + DROPBEAR_OPTS,
                       capture_output=True, timeout=10)
    except Exception as exc:
        log("dropbear missing, restart error: %r" % exc)
    time.sleep(2)   # dropbear 自守护,等 pidfile/proc 就绪再验证
    pid = _dropbear_listener_pid()
    if pid:
        log("dropbear missing, restarted (pid %d)" % pid)
    else:
        log("dropbear missing, restart FAILED; SSH stays down until next check")


def current_ip():
    """wlan0 当前 IPv4(解析 /sbin/ifconfig 输出;不在 PATH 须绝对路径)。

    不用 hostname -I:PW1 的 busybox 版本不支持 -I;无 IP(未关联)返回 None。
    """
    try:
        out = subprocess.run([IFCONFIG, WLAN_IF], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    m = re.search(r"inet addr:((\d+\.){3}\d+)", out)
    return m.group(1) if m else None


# ---------------- 遥测上报 + 远程指令 + 自更新 ----------------
#
# 背景:Muse 无法直接 SSH 到 Kindle(家里内网 NAT,无公网 IP,Tailscale 未接),
# 而 Kindle 每小时都会主动找 Oracle 拉图 —— 方向反过来就通了:
#   上报:Kindle 每轮拉图后 POST 一份遥测 JSON 到 Oracle,Muse 用普通 HTTP
#     GET 就能看到电量/拉图结果,不用 SSH。
#   指令:Muse 把 command.json 写到 Oracle 的 /opt/f1dash/out/(nginx 现成
#     静态服务),Kindle 每轮拉图时顺手 GET,只有 id 变了才执行,执行结果下
#     次上报带回。只允许白名单动作,永远不执行任意 shell。
#   自更新:首装仍需人工 scp(当前跑着的旧代码里没有自更新逻辑,自己拉不到
#     自己);装上这一版后,以后改 Kindle 端代码只需把新 f1dash.py + 版本
#     清单放到 Oracle 的 out/ 目录,Kindle 下一轮自己校验、备份、替换、
#     重启生效。校验失败/下载失败一律保留旧代码继续跑。
#   代码签名(Ed25519):自更新走明文 HTTP,清单里的 sha 和文件都在同一台
#     主机上,光验 sha 防不住 Oracle 被黑或中间人同时换文件+清单——所以
#     清单里还有一份签名,私钥只在 Muse 的 VM 上(Ken 的 Mac 不参与),公
#     钥写死在这份代码里(见 SIGNING_PUBLIC_KEY_HEX)。Kindle 先验签,签名
#     不对直接拒绝,旧代码继续跑;验签通过才继续验 sha→编译→烟测→替换。
#     公钥轮换只能人工重装(新公钥随新代码一起装),旧密钥签的版本一律拒绝。
# 安全说明(v1):遥测接口与 latest.png 同等公开暴露(无鉴权),只收固定字
# 段的 JSON、不执行任何客户端提供的内容、不把客户端给的路径当文件用;
# 若以后要收紧,给 /api/ 加 token 再收。

TELEMETRY_URL = "http://149.118.42.190:8080/api/telemetry"
COMMAND_URL = "http://149.118.42.190:8080/f1/command.json"
SELF_UPDATE_MANIFEST_URL = "http://149.118.42.190:8080/f1/f1dash.version.json"
SELF_UPDATE_URL = "http://149.118.42.190:8080/f1/f1dash.py"
LAST_COMMAND_FILE = "/mnt/us/f1dash/last_command.json"
INTERVAL_OVERRIDE_FILE = "/mnt/us/f1dash/interval_override"
_DAEMON_START = time.time()
_REQUEST_EXIT = False
# 最近一次自更新失败的原因(供 update_code 指令带回;成功/未开始时为 None)。
_LAST_UPDATE_ERROR = None


# ---------------- 自更新代码签名(Ed25519,纯标准库) ----------------
# 信任根:公钥直接写进这份代码,首次人工安装时一并装上;私钥只在 Muse 的
# VM 上(~/.config/kindle-signing/ed25519_private.key,0600),永不进仓库、
# 永不上 Oracle、永不发给 Ken 的 Mac。Kindle 每次自更新先验签,签名不对
# 直接拒绝,旧代码继续跑;验签通过才继续验 sha→编译→烟测→替换。公钥轮
# 换只能人工重装(新公钥进新代码),旧密钥签的版本一律拒绝。
# 验签代码是 cloud/ed25519.py 的 verify 子集(设备上只有验签,没有签名逻
# 辑),test_signing.py 会拿 RFC 8032 向量同时测两边,改动要同步两处。
# 验签只在真有新版时跑一次(sha 不一致才下载),平时每小时的检查到"已是
# 最新"就返回,不验签、不耗电。
SIGNING_PUBLIC_KEY_HEX = "f8d5f3ddbd402230710272f40380cf12c0a06519dabb4882e1347ebf539f0928"
SIGNING_KEY_FINGERPRINT = "92f57f56f96f532d"  # sha256(公钥)前 16 位,供日志/清单核对
_ED_Q = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493


def _ed25519_verify(pubkey, message, sig):
    """Ed25519 验签(纯 hashlib,无第三方库):签名有效 True,否则 False,永不抛异常。

    签名是对文件字节本身的 Ed25519 签名,与清单/URL 无关——攻击者就算换
    了清单里的 url、sha,也伪造不了签名。"""
    try:
        if len(sig) != 64 or len(pubkey) != 32:
            return False
        q = _ED_Q
        d = -121665 * pow(121666, q - 2, q) % q
        I = pow(2, (q - 1) // 4, q)

        def inv(x):
            return pow(x, q - 2, q)

        def xrecover(y):
            xx = (y * y - 1) * inv(d * y * y + 1)
            x = pow(xx, (q + 3) // 8, q)
            if (x * x - xx) % q != 0:
                x = (x * I) % q
            if x % 2 != 0:
                x = q - x
            return x

        def edwards(P, Qpt):
            x1, y1 = P
            x2, y2 = Qpt
            x3 = (x1 * y2 + x2 * y1) * inv(1 + d * x1 * x2 * y1 * y2)
            # 曲线是 a=-1 的 twisted Edwards,y3 分子是 y1*y2 + x1*x2
            # (a=+1 的公式是减号,写错会把所有点算出曲线外)。
            y3 = (y1 * y2 + x1 * x2) * inv(1 - d * x1 * x2 * y1 * y2)
            return [x3 % q, y3 % q]

        def scalarmult(P, e):
            if e == 0:
                return [0, 1]
            Qpt = scalarmult(P, e // 2)
            Qpt = edwards(Qpt, Qpt)
            if e & 1:
                Qpt = edwards(Qpt, P)
            return Qpt

        def bit(h, i):
            return (h[i // 8] >> (i % 8)) & 1

        def decodepoint(s):
            y = sum(2 ** i * bit(s, i) for i in range(0, 255))
            x = xrecover(y)
            if x & 1 != bit(s, 255):
                x = q - x
            if (-x * x + y * y - 1 - d * x * x * y * y) % q != 0:
                raise ValueError("point not on curve")
            return [x, y]

        def Hint(m):
            return int.from_bytes(hashlib.sha512(m).digest(), "little")

        By = 4 * inv(5)
        Bx = xrecover(By)
        B = [Bx % q, By % q]
        R = decodepoint(sig[:32])
        A = decodepoint(pubkey)
        S = int.from_bytes(sig[32:], "little")
        if S >= _ED_L:
            return False
        h = Hint(sig[:32] + pubkey + message)
        return scalarmult(B, S) == edwards(R, scalarmult(A, h))
    except Exception:
        return False


def _sha256_file(path):
    """文件 SHA-256 hex;缺失/异常返回 None。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _http_json(url, payload=None, timeout=15):
    """GET(无 payload)或 POST JSON;返回解析后的 dict,失败返回 None(不抛异常)。"""
    try:
        data = None
        headers = {"User-Agent": "f1dash-telemetry/1.0", "Accept": "*/*"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:
        log("http json %s failed: %r" % (url, repr(exc)[:80]))
        return None


def _telemetry_snapshot(extra=None):
    """本轮遥测快照:电量/放电速率/Wi-Fi/拉图/进程/成图,全部 best-effort。"""
    snap = {"ts": int(time.time()),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600)),
            "mode": "alwayson",
            "pid": os.getpid(),
            "uptime_s": int(time.time() - _DAEMON_START),
            "code_sha": (_sha256_file(os.path.abspath(__file__)) or "")[:12],
            "wifi_up": _wlan_up(),
            "ip": current_ip(),
            "image_sha": (_sha256_file(SS_OUT) or "")[:12]}
    try:
        st = battery_status()
    except Exception:
        st = None
    if st:
        snap["battery_pct"] = st.get("pct")
        snap["battery_mah"] = st.get("mah")
        snap["charging"] = bool(st.get("charging"))
        snap["days_left"] = st.get("days")
    try:
        snap["battery_mv"] = _sysfs_raw("battery_voltage")
        snap["battery_lmd"] = _sysfs_raw("battery_lmd")
    except Exception:
        pass
    # 放电速率:与 battery_status 同口径(最后一次守护重启后的非充电样本)
    try:
        rows, restarts = batt_rows(BATT_LOG)
        now = time.time()
        seg_start = max(now - 24 * 3600, restarts[-1] if restarts else 0.0)
        disch = [r for r in rows if not r["chg"] and r["ts"] >= seg_start]
        if len(disch) >= 2:
            span_h = (disch[-1]["ts"] - disch[0]["ts"]) / 3600.0
            used = disch[0]["mah"] - disch[-1]["mah"]
            snap["drain_mah_h"] = round(used / span_h, 2) if span_h >= 1 and used > 0 else None
        else:
            snap["drain_mah_h"] = None
    except Exception:
        snap["drain_mah_h"] = None
    try:
        du = shutil.disk_usage("/mnt/us")
        snap["disk_free_mb"] = round(du.free / 1024.0 / 1024.0, 1)
    except Exception:
        pass
    if extra:
        snap.update(extra)
    return snap


def _post_telemetry(extra=None):
    """POST 一份遥测到 Oracle;成功 True,失败只记日志返回 False,永不抛异常。"""
    try:
        snap = _telemetry_snapshot(extra)
        res = _http_json(TELEMETRY_URL, payload=snap, timeout=15)
        if isinstance(res, dict) and res.get("ok"):
            log("telemetry posted: pct=%s pull=%s" % (snap.get("battery_pct"),
                                                      (extra or {}).get("pull_ok")))
            return True
        log("telemetry failed: bad response %r" % (res,)[:60])
        return False
    except Exception as exc:
        log("telemetry failed: %r" % exc)
        return False


def _read_last_command():
    try:
        with open(LAST_COMMAND_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_last_command(cmd):
    try:
        tmp = LAST_COMMAND_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cmd, f)
        os.replace(tmp, LAST_COMMAND_FILE)
        return True
    except Exception as exc:
        log("last-command write failed: %r" % exc)
        return False


def _read_interval_override(base):
    """每轮重读间隔覆盖文件(300-7200s 有效);无效/缺失返回 base。"""
    try:
        with open(INTERVAL_OVERRIDE_FILE) as f:
            v = int(f.read().strip())
        if 300 <= v <= 7200:
            return v
    except Exception:
        pass
    return base


def _execute_command(cmd):
    """执行一条白名单指令,返回结果 dict;未知/非法动作只返回错误,永不抛异常。

    动作:refresh_now | set_interval_seconds | set_mode | reboot |
    update_code(别名 pull_latest_code)。
    set_mode= screensaver 与 reboot 必须带 confirm=true(前者会结束守护进程、
    后者会重启设备),缺失直接拒绝。update_code 不需要 confirm:它只走
    _check_self_update 的同一条校验链(验签→验 sha→py_compile→import 烟测),
    任一步失败保留旧代码,签名失败时原因记 bad_signature;已是最新版时
    返回 already_latest,不重启。
    注意:update 成功时 _check_self_update 会 exec 重启,本函数永不返回,
    所以成功结果要靠 on_before_restart 在重启前写进 last_command.json,
    新进程首轮 _poll_and_execute_command 会把它当作本轮指令结果带回上报。
    """
    global _REQUEST_EXIT
    action = cmd.get("action")
    if not action or action == "none":
        # 占位指令:静默 no-op,不执行、不报错。首装时 command.json 就是
        # {"id": 0, "action": "none"},不能把它当成未知动作(2026-11-23
        # 实测:首轮轮询把它当新指令执行,返回 unknown action 并写入失败)。
        return {"action": "none", "ok": True, "note": "no-op"}
    try:
        if action == "refresh_now":
            t0 = time.time()
            ok = pull_image(outs=(SS_OUT,))
            return {"action": action, "ok": bool(ok),
                    "seconds": round(time.time() - t0, 1)}
        if action == "set_interval_seconds":
            try:
                v = int(cmd.get("seconds", 0))
            except (TypeError, ValueError):
                return {"action": action, "ok": False, "error": "bad seconds"}
            if not 300 <= v <= 7200:
                return {"action": action, "ok": False, "error": "seconds out of range 300-7200"}
            with open(INTERVAL_OVERRIDE_FILE, "w") as f:
                f.write(str(v) + "\n")
            return {"action": action, "ok": True, "seconds": v,
                    "note": "applies from next cycle"}
        if action == "set_mode":
            if cmd.get("confirm") is not True:
                return {"action": action, "ok": False, "error": "confirm=true required"}
            mode = cmd.get("mode")
            if mode == "alwayson":
                return {"action": action, "ok": True, "mode": mode, "note": "already alwayson"}
            if mode == "screensaver":
                # 与 always_on_stop.sh 同语义:撤防入睡、拉回框架/pillow,退出守护;
                # 退出后设备恢复正常睡眠,锁屏显示 SS_OUT(信息牌模式)。
                keep_awake(False)
                for job in ("framework", "pillow"):
                    try:
                        subprocess.run(["/sbin/start", job], capture_output=True, timeout=10)
                    except Exception:
                        try:
                            subprocess.run(["start", job], capture_output=True, timeout=10)
                        except Exception as exc:
                            log("set_mode screensaver: start %s failed: %r" % (job, exc))
                try:
                    subprocess.run(["lipc-set-prop", "com.lab126.powerd",
                                    "preventScreenSaver", "0"],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass
                _REQUEST_EXIT = True
                return {"action": action, "ok": True, "mode": mode,
                        "note": "daemon exits after this cycle"}
            return {"action": action, "ok": False, "error": "unknown mode %r" % (mode,)}
        if action in ("update_code", "pull_latest_code"):
            # 按需拉最新代码:先查清单,已是最新直接返回不重启;有新版才走
            # _check_self_update(同一条校验链,不绕过任何一步)。成功时对方
            # exec 重启,本函数永不返回——成功结果已由 on_before_restart
            # 在重启前写进 last_command.json(带 pending_report,新进程首轮
            # 上报时带回;见 _poll_and_execute_command)。
            if os.path.exists(WIFI_HOLD_FLAG):
                return {"action": action, "ok": False, "updated": False,
                        "reason": "wifi_hold"}
            local = os.path.abspath(__file__)
            cur = _sha256_file(local)
            manifest = _http_json(SELF_UPDATE_MANIFEST_URL, timeout=15)
            if not isinstance(manifest, dict):
                return {"action": action, "ok": False, "updated": False,
                        "old_sha": cur, "reason": "manifest fetch failed"}
            want_sha = manifest.get("sha256")
            if not want_sha:
                return {"action": action, "ok": False, "updated": False,
                        "old_sha": cur, "reason": "manifest has no sha256"}
            if cur == want_sha:
                return {"action": action, "ok": True, "updated": False,
                        "old_sha": cur, "new_sha": want_sha,
                        "version": manifest.get("version"),
                        "reason": "already_latest"}
            result = {"action": action, "ok": True, "updated": True,
                      "old_sha": cur, "new_sha": want_sha,
                      "version": manifest.get("version"),
                      "id": cmd.get("id"), "pending_report": True}

            def _record_update_result():
                _write_last_command(result)

            ok = _check_self_update(allow_restart=True,
                                    on_before_restart=_record_update_result)
            if not ok:
                return {"action": action, "ok": False, "updated": False,
                        "old_sha": cur, "new_sha": want_sha,
                        "version": manifest.get("version"),
                        "reason": _LAST_UPDATE_ERROR or "update failed"}
            return result  # 成功时上面已 exec,到不了这里;仅作兜底
        if action == "reboot":
            if cmd.get("confirm") is not True:
                return {"action": action, "ok": False, "error": "confirm=true required"}
            log("command: reboot confirmed, rebooting now")
            try:
                subprocess.run(["reboot"], timeout=10)
            except Exception as exc:
                return {"action": action, "ok": False, "error": repr(exc)[:60]}
            return {"action": action, "ok": True}
        return {"action": action, "ok": False, "error": "unknown action"}
    except Exception as exc:
        return {"action": action, "ok": False, "error": repr(exc)[:80]}


def _poll_and_execute_command():
    """GET command.json;id 未变返回 {"action": "none"};新 id 执行并记录结果。

    占位指令 action=="none"(或缺失/空串)是静默 no-op:不执行、不写
    last_command、不报错——首装 command.json 就是 {"id": 0, "action":
    "none"}。拉取失败返回 None(不断网重试,不断线)。执行结果写入
    last_command.json,供下次上报带回;执行本身永不抛异常。
    """
    try:
        cmd = _http_json(COMMAND_URL, timeout=15)
    except Exception:
        return None
    if not isinstance(cmd, dict) or "id" not in cmd:
        return None
    last = _read_last_command()
    if last.get("id") == cmd.get("id"):
        # update_code 成功时守护已 exec 重启,本轮上报没走成:上轮结果还
        # 挂着 pending_report,本轮把它当作指令结果带回上报一次,之后清掉,
        # 避免每小时重复报。其它指令仍按老语义返回 none(不重复执行)。
        if (last.get("action") in ("update_code", "pull_latest_code")
                and last.get("pending_report")):
            last["pending_report"] = False
            _write_last_command(last)
            return dict(last)
        return {"action": "none", "id": cmd.get("id")}
    # 新 id:先把上轮 update_code 挂着的 pending 结果带回一次(若有),
    # 再处理本轮指令;避免 Muse 已把 command.json 复位成 none 时丢结果。
    # 注意:下面返回 pending 结果时不执行本轮指令,本轮指令下轮仍是新 id,
    # 会正常执行,不会丢。
    if (last.get("action") in ("update_code", "pull_latest_code")
            and last.get("pending_report")):
        last["pending_report"] = False
        _write_last_command(last)
        return dict(last)
    if not cmd.get("action") or cmd.get("action") == "none":
        # 占位指令(首装 command.json 就是 {"id": 0, "action": "none"}):
        # 不执行、不写 last_command、不报错;下次同 id 轮询仍走 no-op。
        return {"action": "none", "id": cmd.get("id")}
    res = _execute_command(cmd)
    res["id"] = cmd.get("id")
    _write_last_command(res)
    log("command executed: %r" % (res,)[:120])
    return res


def _check_self_update(allow_restart=True, on_before_restart=None):
    """自更新:清单 sha 与本地不一致时,下载→验签→验 sha→py_compile→import 烟测→
    备份(.bak)→原子替换→重启守护。任一步失败保留旧代码,只记日志,并把原因
    记到 _LAST_UPDATE_ERROR(供 update_code 指令带回)。

    验签是第一道门,而且是唯一防中间人/Oracle 被黑的门:签名是对文件字节
    的 Ed25519 签名,私钥只在 Muse 的 VM 上;清单缺签名、签名损坏、验签
    失败一律记 bad_signature 并拒绝,连 sha 都不比。已是最新版时在下载前
    就返回,不验签(没有新代码可装,也就没有可伪造的东西)。

    wifi_hold 存在时跳过(运维豁免)。allow_restart=False 时只做到"校验通过、
    已验证可替换"即返回 True(供测试),不替换、不重启。
    重启前删 pidfile,否则新进程启动时 main() 的 pid 检查会以为旧守护还在
    跑而直接退出。on_before_restart 在替换成功、exec 重启之前调用一次:
    update_code 靠它在重启前把执行结果写进 last_command.json,否则 exec
    后本轮永远走不到 _post_telemetry,结果就丢了;自动检查路径不传它。
    """
    global _LAST_UPDATE_ERROR
    _LAST_UPDATE_ERROR = None
    if os.path.exists(WIFI_HOLD_FLAG):
        _LAST_UPDATE_ERROR = "wifi_hold"
        return False
    manifest = _http_json(SELF_UPDATE_MANIFEST_URL, timeout=15)
    if not isinstance(manifest, dict):
        _LAST_UPDATE_ERROR = "manifest fetch failed"
        return False
    want_sha = manifest.get("sha256")
    if not want_sha:
        log("self-update: manifest has no sha256")
        _LAST_UPDATE_ERROR = "manifest has no sha256"
        return False
    local = os.path.abspath(__file__)
    cur = _sha256_file(local)
    if cur == want_sha:
        return False
    url = manifest.get("url") or SELF_UPDATE_URL
    log("self-update: %s -> %s" % ((cur or "")[:8], want_sha[:8]))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "f1dash-telemetry/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            blob = resp.read(2 * 1024 * 1024 + 1)
        if len(blob) > 2 * 1024 * 1024:
            log("self-update: download too large, abort")
            _LAST_UPDATE_ERROR = "download too large"
            return False
    except Exception as exc:
        log("self-update: download failed: %r" % exc)
        _LAST_UPDATE_ERROR = "download failed"
        return False
    # 签名门:清单里的 signature 是对刚下载到的文件字节的 Ed25519 签名
    # (与清单同主机、明文 HTTP 也无妨——伪造清单+文件仍过不了验签,因
    # 为签名私钥不在 Oracle 上)。缺失/损坏/验签失败一律拒绝,旧代码继续
    # 跑,原因记 bad_signature(供 update_code 带回)。
    sig_b64 = manifest.get("signature")
    if not sig_b64:
        log("self-update: manifest has no signature, abort")
        _LAST_UPDATE_ERROR = "bad_signature"
        return False
    try:
        sig = base64.b64decode(sig_b64, validate=True)
    except Exception:
        log("self-update: signature is not valid base64, abort")
        _LAST_UPDATE_ERROR = "bad_signature"
        return False
    try:
        pubkey = bytes.fromhex(SIGNING_PUBLIC_KEY_HEX)
    except Exception:
        pubkey = b""
    if not _ed25519_verify(pubkey, blob, sig):
        log("self-update: bad signature, abort")
        _LAST_UPDATE_ERROR = "bad_signature"
        return False
    if hashlib.sha256(blob).hexdigest() != want_sha:
        log("self-update: sha256 mismatch, abort")
        _LAST_UPDATE_ERROR = "sha256 mismatch"
        return False
    new_path = local + ".new"
    try:
        with open(new_path, "wb") as f:
            f.write(blob)
        r = subprocess.run([sys.executable, "-m", "py_compile", new_path],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            log("self-update: py_compile failed: %r" % (r.stderr[:100],))
            os.remove(new_path)
            _LAST_UPDATE_ERROR = "py_compile failed"
            return False
        # import 烟测:在隔离目录以 f1dash.py 名导入新文件(顶层只定义常量/导入,
        # 不联网不碰硬件);失败同样丢弃。
        test_dir = "/tmp/_upd_test"
        try:
            os.makedirs(test_dir, exist_ok=True)
            shutil.copyfile(new_path, os.path.join(test_dir, "f1dash.py"))
            r = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, '/tmp/_upd_test');"
                 "import f1dash; print('import ok', f1dash.SS_SIZE)"],
                capture_output=True, timeout=60, cwd=test_dir)
            if r.returncode != 0:
                log("self-update: import smoke test failed: %r" % (r.stderr[:100],))
                os.remove(new_path)
                _LAST_UPDATE_ERROR = "import smoke test failed"
                return False
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)
    except Exception as exc:
        log("self-update: verify failed: %r" % exc)
        try:
            os.remove(new_path)
        except Exception:
            pass
        _LAST_UPDATE_ERROR = "verify failed"
        return False
    if not allow_restart:
        try:
            os.remove(new_path)
        except Exception:
            pass
        log("self-update: verified, restart deferred (test mode)")
        return True
    # 备份→原子替换(第二步失败把 .bak 搬回),写 marker,删 pidfile 后 exec 重启。
    bak = local + ".bak"
    try:
        os.replace(local, bak)
        try:
            os.replace(new_path, local)
        except Exception:
            os.replace(bak, local)
            raise
        with open(local + ".update.json", "w") as f:
            json.dump({"from": cur, "to": want_sha,
                       "version": manifest.get("version"),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.gmtime(time.time() + 8 * 3600))}, f)
    except Exception as exc:
        log("self-update: replace failed: %r" % exc)
        _LAST_UPDATE_ERROR = "replace failed"
        return False
    log("self-update: replaced %s -> %s, restarting daemon" % ((cur or "")[:8], want_sha[:8]))
    if on_before_restart is not None:
        try:
            on_before_restart()
        except Exception as exc:
            log("self-update: on_before_restart failed: %r" % exc)
    try:
        os.remove("/var/run/f1dash_alwayson.pid")
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable, local] + sys.argv[1:])
    return True


def always_cycle():
    """一轮常显刷新:巡检 dropbear → Wi-Fi 硬开 → 拉图 → 图/电量有变才显示 → finally 保持开窗后硬关。

    Wi-Fi 整点硬开,拉图完成后保持到 ALWAYS_WIFI_WINDOW 截止(整点后 5 分钟,
    SSH/运维的可预期窗口),到点 finally 硬关(拉图失败也照关,失败的一轮绝不
    把 Wi-Fi 常开整小时)。连续 WIFI_FAIL_OPEN 轮拿不到网/拉图失败则 fail-open:
    Wi-Fi 保持常开继续每小时重试拉图,直到首个成功轮复位计数——宁可退回软关
    时代的耗电,也不能让屏面永远死在旧图上;wifi_hold 标志存在时本轮豁免硬关。
    首次硬开拿不到路由时重试一次完整启动序列(lipc set 的源注册竞态由
    _wifi_up 内部重试)。开头先巡检 dropbear(SSH 入站自愈,见 dropbear_watchdog);
    IP 在 Wi-Fi 还开着时记录(硬关后 inet addr 仍在,读到的是假象)。云端原图
    只进 SS_OUT(锁屏图保持干净,framework 活着时状态栏自有电量),eips 显示图
    由 overlay_battery 从 SS_OUT 现算:即使拉图失败,角标上的电量/续航也随每轮
    更新。成图没变(pull_image 对相同字节不写盘,比对前后内容即知)且角标
    电量没变时跳过重显,静等下一轮;成图有变走整屏 flash 清残影(-g -f 的
    full update 实测清不掉,见 show_fullscreen)。
    """
    global _last_shown_pct, _wifi_fail_streak
    t0 = time.time()
    dropbear_watchdog()
    wifi(True)
    net = pulled = False
    cap = "?"
    t_reconnect = None
    t_pull = None
    try:
        t_net = time.time()
        net = wait_network()
        if net:
            t_reconnect = round(time.time() - t_net, 1)
            log("cycle: wifi up, route in %.0fs" % (time.time() - t_net))
        else:
            log("cycle: no network; retry wifi start sequence")
            wifi(True)
            t_net = time.time()
            net = wait_network(45)
            if net:
                t_reconnect = round(time.time() - t_net, 1)
                log("cycle: wifi up on retry, route in %.0fs" % (time.time() - t_net))
        prev = _read_bytes(SS_OUT)
        if net:
            _t_pull0 = time.time()
            pulled = pull_image(outs=(SS_OUT,))
            t_pull = round(time.time() - _t_pull0, 1)
            if not pulled:
                log("cycle: pull failed; keep last image")
        else:
            log("cycle: no network; keep last image")
        image_changed = _read_bytes(SS_OUT) != prev
        cap = _sysfs_raw("battery_capacity")
        try:
            pct = int(cap.rstrip("%"))
        except ValueError:
            pct = None
        if image_changed or (pct is not None and pct != _last_shown_pct):
            shown = None
            if os.path.exists(SS_OUT):
                shown = ALWAYS_OUT if overlay_battery() else SS_OUT
            elif os.path.exists(ALWAYS_OUT):
                shown = ALWAYS_OUT
            if shown:
                show_fullscreen(shown, flash=image_changed)   # 成图有变才整屏 flash 清残影
                if image_changed:
                    log("cycle: image changed; refresh with fullscreen flash")
                _last_shown_pct = pct
                front_light_off()   # 绘制/eips 后重申:单次熄灯是已知脆弱模式,会爬回 1
        else:
            log("cycle: image and battery unchanged; skip eips refresh")
        log("ip: %s" % (current_ip() or "none"))
        # 遥测 + 远程指令 + 自更新(仅本轮联网时;任一步失败只记日志,不影响显示)。
        # 自更新版本有变且校验通过时直接 exec 重启,本函数不再返回。
        # 自动自更新与 update_code 指令并存:前者每轮静默检查,后者按需立即
        # 触发,走同一条校验链(update_code 只是多了显式结果和立即执行)。
        cmd_result = None
        if net:
            cmd_result = _poll_and_execute_command()
            if pulled:
                _check_self_update()
        _post_telemetry({"pull_ok": pulled, "net_ok": net,
                         "reconnect_s": t_reconnect, "pull_s": t_pull,
                         "command": cmd_result, "battery": cap or "?"})
    finally:
        if pulled:
            _wifi_fail_streak = 0
        else:
            _wifi_fail_streak += 1
        if _wifi_fail_streak >= WIFI_FAIL_OPEN:
            log("fail-open: leaving wifi on after %d failed cycles" % _wifi_fail_streak)
        elif os.path.exists(WIFI_HOLD_FLAG):
            log("wifi hold flag present; leaving wifi on this cycle")
        else:
            remain = t0 + ALWAYS_WIFI_WINDOW - time.time()
            if remain > 0:
                time.sleep(remain)   # 开窗保持:整点后 5 分钟留给 SSH/运维,到点再关
            log("wifi hard-off %s" % ("ok" if wifi(False) else "NOT verified"))
        log("cycle done in %ds: net=%s pull=%s battery %s"
            % (time.time() - t0, net, pulled, cap or "?"))


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


def _next_grid_sleep(interval):
    """睡到 interval 网格的下一个对齐点(epoch 整倍数;3600 → 每小时整点,测试
    短间隔 → 对齐分钟网格),让 SSH/运维的开窗时刻可预期。"""
    now = time.time()
    return max(1.0, (int(now // interval) + 1) * interval - now)


def always_on_mode(interval=None):
    """常显主循环:启动即刷一轮,之后睡到周期网格对齐点再重申防入睡并刷一轮。

    间隔优先级:--interval 参数 > F1DASH_ALWAYSON_INTERVAL 环境变量 > 默认 3600。
    循环对齐到网格(3600 → 整点拉图,Wi-Fi 开窗 :00-:05 可预期)。
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
    if framework_stop():
        log("framework stopped; screen is ours alone")
    else:
        log("warning: framework stop failed; chrome may repaint over the image")
    front_light_off()   # 停框架之后:停机会重套滑块亮度,先停再灭
    always_cycle()
    while True:
        if _REQUEST_EXIT:
            log("always-on exiting on remote command (set_mode screensaver)")
            return
        interval = _read_interval_override(interval)
        nap = _next_grid_sleep(interval)
        t0 = time.time()
        time.sleep(nap)
        elapsed = time.time() - t0
        if elapsed > nap + 120:
            log("warning: %ds gap in loop — system suspended?" % round(elapsed))
        keep_awake(True)
        if framework_ensure_stopped():
            log("framework respawned; stopped it again")
        front_light_off()
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
    行间空洞 >1.5× 采样周期视为发生过挂起(常显模式不该出现),显式列出。
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

    gap_limit = BATT_SAMPLE * 1.5   # 正常行距 ~BATT_SAMPLE(读数开销另加),超 1.5 倍才算挂起空洞
    gaps = [(rows[i - 1]["ts"], rows[i]["ts"]) for i in range(1, len(rows))
            if rows[i]["ts"] - rows[i - 1]["ts"] > gap_limit]
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
        "stale": {},
    }


def mock_briefing_data(template="morning"):
    """床头屏离线假数据:--mock-morning/--mock-evening/--mock-weekend 用。

    天气/头条取设计稿 v2 的示例值(2026-11-23 实报天气 + 当日示例头条),
    仅供版式验证,不是实时数据;briefing.json 的钱数示例也只在 mock 里出现,
    生产渲染缺 briefing.json 时留 TODO 占位。
    """
    import calendar as _cal

    def _bj_epoch(y, mo, dd, hh, mm):
        return _cal.timegm((y, mo, dd, hh, mm, 0, 0, 0, 0)) - 8 * 3600

    def _day(date, high, low, text, pop, pop_min=10, hum=64,
             sunrise="06:30", sunset="16:59"):
        wd = time.strptime(date, "%Y-%m-%d").tm_wday
        code = {"Clear": 0, "Mostly clear": 1, "Partly cloudy": 2,
                "Cloudy": 3}.get(text, 3)
        return {"date": date, "weekday": wd, "high": high, "low": low,
                "text": text, "code": code, "pop": pop, "pop_min": pop_min,
                "humidity": hum, "sunrise": sunrise, "sunset": sunset}

    days = [_day("2026-11-23", 17, 9, "Cloudy", 80, 20, 64, "06:30", "16:59"),
            _day("2026-11-24", 14, 3, "Partly cloudy", 10, 10, 55, "06:31", "16:58"),
            _day("2026-11-25", 13, 4, "Partly cloudy", 10, 10, 55, "06:32", "16:58"),
            _day("2026-11-26", 15, 5, "Clear", 10, 10, 50, "06:33", "16:58"),
            _day("2026-11-27", 17, 6, "Clear", 10, 10, 50, "06:33", "16:58"),
            _day("2026-11-28", 20, 4, "Clear", 10, 10, 45, "06:34", "16:58"),
            _day("2026-11-29", 19, 7, "Clear", 10, 10, 45, "06:35", "16:58"),
            _day("2026-11-30", 23, 9, "Clear", 10, 10, 45, "06:36", "16:57")]
    data = mock_data(err=False)
    if template == "morning":
        today, tomorrow = days[0], days[1]
        now_bj = _beijing_now(_bj_epoch(2026, 11, 23, 6, 35))
    elif template == "evening":
        today, tomorrow = days[0], days[1]
        now_bj = _beijing_now(_bj_epoch(2026, 11, 23, 18, 5))
    else:  # weekend:周六 11-28 当天
        today, tomorrow = days[5], days[6]
        now_bj = _beijing_now(_bj_epoch(2026, 11, 28, 9, 0))
    data["weather"] = {"city": "HANGZHOU", "today": today,
                       "tomorrow": tomorrow, "days": days}
    data["template"] = template
    data["now_bj"] = now_bj
    data["upd"] = "%02d:%02d" % (now_bj.tm_hour, now_bj.tm_min)
    data["upd_stamp"] = "%02d-%02d %s" % (now_bj.tm_mon, now_bj.tm_mday, data["upd"])
    data["headlines"] = {
        "morning": ["美股科技股周五暴跌纳指跌5.3%", "英伟达跌14% AMD跌13%",
                    "Anthropic称完成黎曼猜想证明", "中证500周五跌3.71%创52周新低",
                    "F1拉斯维加斯 Verstappen夺冠"],
        "evening": ["美股科技股周五暴跌纳指跌5.3%", "英伟达跌14% AMD跌13%",
                    "Anthropic称完成黎曼猜想证明", "中证500周五跌3.71%创52周新低"],
        "fresh_morning": True, "fresh_evening": True,
        "updated": "2026-11-23T06:30:00+08:00"}
    data["briefing"] = {
        "polymarket": {"cash": "$1,022.51", "pnl": "+$172.51"},
        "weekly": {"total": 569990.49, "pnl": -18705.94, "pct": -3.23,
                   "date": "2026-11-21", "note": "中证500周跌7.11% · 收6.053"},
        "close": {"510500": {"price": 6.053, "chg": -3.71, "date": "11-20"},
                  "grid_note": "中证500草稿待定 · 周一开盘确认"},
        "tonight": "F1 Verstappen夺冠已结算 · 巴萨21:00开球"}
    return data


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
    if mode == "telemetry":
        return 0 if _post_telemetry({"manual": True}) else 1
    if mode == "command":
        res = _poll_and_execute_command()
        print(res)
        return 0 if res is not None else 1
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

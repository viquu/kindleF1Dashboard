#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F1 Dashboard 云端渲染(Oracle Cloud):拉数据 → Pillow 出图 → nginx 静态服务。

部署布局(/opt/f1dash/):
    f1dash.py   与 kindle/ 同一份代码,直接 import 复用取数与渲染(零逻辑分叉)
    render.py   本文件,systemd timer 每 10 分钟调用
    fonts/      Barlow 出图字体 + Noto Sans CJK SC(中文标题/头条,见 deploy.sh)
    out/        latest.png,nginx 服务 http://<host>:8080/f1/latest.png
                headlines.json(Muse 06:30/18:00 生成,新闻唯一来源,无 RSS 兜底)
                briefing.json(Muse 提供的收盘/模拟盘/本周回顾,可选,缺了留占位)

模板选择(北京时间):工作日 06:35–09:00 晨报、18:00–23:00 晚报、
周五 18:00 起及周末 07:00–23:00 周末屏,其余时段保留原仪表盘/海报。
显式 --morning/--evening/--weekend 覆盖时间选择(调试用);--mock-*
用离线假数据渲染对应模板(版式验证,不联网)。

Kindle 侧 f1dash.py pull 模式下载成图;主数据源失败时不覆盖 latest.png
(与设备端"保留上一张屏保"的兜底语义一致),exit 1 让 systemd 记录失败。
各源失败时用 _cached_fetch 的上次好数据渲染(见 kindle/f1dash.py)。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f1dash

OUT_DIR = os.environ.get("F1DASH_OUT_DIR", "/opt/f1dash/out")


def main():
    argv = sys.argv[1:]
    out = os.path.join(OUT_DIR, "latest.png")
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]

    mock_template = None
    for flag, tpl in (("--mock-morning", "morning"),
                      ("--mock-evening", "evening"),
                      ("--mock-weekend", "weekend")):
        if flag in argv:
            mock_template = tpl
    if mock_template is not None:
        data = f1dash.mock_briefing_data(mock_template)
        ok = f1dash.png_render_briefing(data, out=out)
        return 0 if ok else 1

    if "--mock-race" in argv:
        # 离线预览赛事海报版式(含排位/冲刺结果块)
        data = f1dash.mock_data(err=False, poster=True)
        ok = f1dash.png_render(data, out=out)
        return 0 if ok else 1
    if "--mock" in argv:
        # 离线预览仪表盘版式
        data = f1dash.mock_data(err=False)
        ok = f1dash.png_render(data, out=out)
        return 0 if ok else 1

    headlines = os.path.join(OUT_DIR, "headlines.json")
    briefing = os.path.join(OUT_DIR, "briefing.json")
    data = f1dash.get_briefing_data(headlines_path=headlines,
                                    briefing_path=briefing)
    if data["err"] is not None:
        print("fetch failed; keeping previous:", repr(data["err"])[:80])
        return 1

    # 显式模板覆盖(调试用),缺省按时间自动选
    for flag, tpl in (("--morning", "morning"), ("--evening", "evening"),
                      ("--weekend", "weekend")):
        if flag in argv:
            data["template"] = tpl
    ok = f1dash.png_render_briefing(data, out=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

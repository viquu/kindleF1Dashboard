#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F1 Dashboard 云端渲染(Oracle Cloud):拉数据 → Pillow 出图 → nginx 静态服务。

部署布局(/opt/f1dash/):
    f1dash.py   与 kindle/ 同一份代码,直接 import 复用取数与渲染(零逻辑分叉)
    render.py   本文件,systemd timer 每 10 分钟调用
    fonts/      Futura 副本(优先)或 Jost 后备,见 f1dash._font_candidates()
    out/        latest.png,nginx 服务 http://<host>:8080/f1/latest.png

Kindle 侧 f1dash.py pull 模式下载成图;主数据源失败时不覆盖 latest.png
(与设备端"保留上一张屏保"的兜底语义一致),exit 1 让 systemd 记录失败。
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

    if "--mock-race" in argv:
        # 离线预览赛事海报版式(含排位/冲刺结果块)
        data = f1dash.mock_data(err=False, poster=True)
    elif "--mock" in argv:
        # 离线预览仪表盘版式
        data = f1dash.mock_data(err=False)
    else:
        data = f1dash.get_dashboard_data()
        if data["err"] is not None:
            print("fetch failed; keeping previous:", repr(data["err"])[:80])
            return 1

    os.makedirs(os.path.dirname(out), exist_ok=True)
    ok = f1dash.png_render(data, out=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

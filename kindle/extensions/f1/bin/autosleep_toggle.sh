#!/bin/sh
# 切换信息牌"一键回睡"开关:写/删 /mnt/us/f1dash/auto_sleep 标志文件。
# 开启后:设备唤醒 → 守护进程刷新屏保图(云端拉图,秒级)→ 模拟电源键自动
# 回睡,锁屏始终显示最新图;想正常阅读时切回 OFF。
# 同步改写 menu.json 标签让菜单直接显示当前状态(config.xml dynamic=true,
# KUAL 检测到菜单文件变化会重载;若未立即重载,状态以标志文件为准)。
FLAG=/mnt/us/f1dash/auto_sleep
MENU=/mnt/us/extensions/f1/menu.json

LABEL=$(/mnt/us/python3/bin/python3.9 - "$FLAG" "$MENU" <<'EOF'
import json
import os
import sys

flag, menu_path = sys.argv[1], sys.argv[2]
if os.path.exists(flag):
    os.remove(flag)
    label = "Auto Sleep: OFF"
else:
    with open(flag, "w") as f:
        f.write("on\n")
    label = "Auto Sleep: ON"
try:
    with open(menu_path) as f:
        menu = json.load(f)

    def walk(items):
        for it in items:
            if str(it.get("name", "")).startswith("Auto Sleep:"):
                it["name"] = label
            if "items" in it:
                walk(it["items"])

    walk(menu["items"])
    with open(menu_path, "w") as f:
        json.dump(menu, f, indent=4, ensure_ascii=False)
except Exception:
    pass
print(label)
EOF
)
echo "auto sleep -> $LABEL"

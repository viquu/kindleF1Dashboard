#!/bin/sh
# 切换常显模式:按 pidfile 实际运行状态启动/停止,并把菜单标签改写为当前状态
# (KUAL 检测到 menu.json 变化会重载;未重载时以 pidfile 为准)。
PIDFILE=/var/run/f1dash_alwayson.pid
MENU=/mnt/us/extensions/f1/menu.json

RUNNING=0
OLDPID=$(cat "$PIDFILE" 2>/dev/null)
if [ -n "$OLDPID" ] && [ -d "/proc/$OLDPID" ] && \
   grep -q "f1dash.py alwayson" "/proc/$OLDPID/cmdline" 2>/dev/null; then
    RUNNING=1
fi

if [ "$RUNNING" = 1 ]; then
    sh /mnt/us/extensions/f1/bin/always_on_stop.sh
    LABEL="Always On: OFF"
else
    sh /mnt/us/extensions/f1/bin/always_on_start.sh
    LABEL="Always On: ON"
fi

/mnt/us/python3/bin/python3.9 - "$MENU" "$LABEL" <<'EOF'
import json
import sys

menu_path, label = sys.argv[1], sys.argv[2]
try:
    with open(menu_path) as f:
        menu = json.load(f)

    def walk(items):
        for it in items:
            if str(it.get("name", "")).startswith("Always On:"):
                it["name"] = label
            if "items" in it:
                walk(it["items"])

    walk(menu["items"])
    with open(menu_path, "w") as f:
        json.dump(menu, f, indent=4, ensure_ascii=False)
except Exception:
    pass
EOF
echo "always on -> $LABEL"

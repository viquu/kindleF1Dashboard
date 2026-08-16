#!/bin/sh
# 停止 F1 Dashboard 自动刷新服务
PIDFILE=/var/run/f1dash.pid

if [ -f "$PIDFILE" ]; then
    # 杀死守护进程和它的外层循环
    kill "$(cat "$PIDFILE")" 2>/dev/null
    pkill -f "f1dash.py service" 2>/dev/null
    rm -f "$PIDFILE"
    echo "f1dash service stopped"
else
    echo "f1dash service not running"
fi

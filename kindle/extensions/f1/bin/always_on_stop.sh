#!/bin/sh
# 停止常显模式并恢复正常睡眠行为(撤掉防入睡标志)
PIDFILE=/var/run/f1dash_alwayson.pid

if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
fi
pkill -f "f1dash.py alwayson" 2>/dev/null
# 恢复 GUI 框架与状态栏渲染器(KUAL/正常阅读回来;守护已完全脱离会话,pkill 能抓到它)
/sbin/start framework 2>/dev/null || start framework 2>/dev/null
/sbin/start pillow 2>/dev/null || start pillow 2>/dev/null
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
echo "f1dash alwayson stopped"

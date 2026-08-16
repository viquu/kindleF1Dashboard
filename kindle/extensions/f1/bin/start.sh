#!/bin/sh
# 启动 F1 Dashboard 自动刷新服务:
#   设备唤醒时立即刷新屏保图,清醒时每 30 分钟刷新一次
PIDFILE=/var/run/f1dash.pid

# 已在运行则跳过
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "f1dash service already running"
    exit 0
fi

# 立即生成一次屏保图
/mnt/us/python3/bin/python3.9 /mnt/us/f1dash/f1dash.py png

# 后台守护进程(设备无 nohup,用 stdin 隔离 + 普通后台运行)
sh -c 'while true; do /mnt/us/python3/bin/python3.9 /mnt/us/f1dash/f1dash.py service; sleep 5; done' </dev/null >/dev/null 2>&1 &
echo $! > "$PIDFILE"
echo "f1dash service started, pid: $(cat "$PIDFILE")"

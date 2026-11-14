#!/bin/sh
# 启动 F1 Dashboard 自动刷新服务:
#   设备唤醒时立即刷新屏保图,清醒时每 30 分钟刷新一次
PIDFILE=/var/run/f1dash.pid

# 已在运行则跳过(校验 pid 确实是 f1dash:pid 会被系统回收,裸 kill -0 会误判)
OLDPID=$(cat "$PIDFILE" 2>/dev/null)
if [ -n "$OLDPID" ] && [ -d "/proc/$OLDPID" ] && \
   grep -q f1dash "/proc/$OLDPID/cmdline" 2>/dev/null; then
    echo "f1dash service already running"
    exit 0
fi
rm -f "$PIDFILE"

# 立即刷新一次屏保图(下载云端成图;失败保留上一张)
/mnt/us/python3/bin/python3.9 /mnt/us/f1dash/f1dash.py refresh

# 后台守护进程(设备无 nohup/setsid;KUAL 从 GUI 启动即可常驻,
# 从 SSH 启动需先 python 双 fork 脱离会话,否则断开会话时被 SIGHUP 带走)
sh -c 'while true; do /mnt/us/python3/bin/python3.9 /mnt/us/f1dash/f1dash.py service; sleep 5; done' </dev/null >/dev/null 2>&1 &
echo $! > "$PIDFILE"
echo "f1dash service started, pid: $(cat "$PIDFILE")"

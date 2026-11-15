#!/bin/sh
# 启动常显模式(never sleep):停掉睡眠刷新服务(互斥)。
# f1dash.py alwayson 在 python 内双 fork+setsid 自守护:父进程秒退,守护进程
# 完全脱离会话 —— 会话无论正常关闭还是被周期关 Wi-Fi 强行断连收网都杀不到它。
# 用法: always_on_start.sh [刷新间隔秒]  (缺省 3600;传 180 之类短周期便于测试)
PIDFILE=/var/run/f1dash_alwayson.pid

# 已在运行则跳过(校验 pid 确实是 alwayson:pid 会被系统回收,裸 kill -0 会误判)
OLDPID=$(cat "$PIDFILE" 2>/dev/null)
if [ -n "$OLDPID" ] && [ -d "/proc/$OLDPID" ] && \
   grep -q "f1dash.py alwayson" "/proc/$OLDPID/cmdline" 2>/dev/null; then
    echo "f1dash alwayson already running"
    exit 0
fi
rm -f "$PIDFILE"

# 与睡眠刷新服务互斥:常显自己拉图显示,旧服务的自动回睡逻辑会打架
sh /mnt/us/extensions/f1/bin/stop.sh >/dev/null 2>&1

INTERVAL_ARGS=""
if [ -n "$1" ]; then
    INTERVAL_ARGS="--interval $1"
fi
# python fork 出守护后父进程秒退,前台等 ~1 秒即可拿到 pidfile
/mnt/us/python3/bin/python3.9 /mnt/us/f1dash/f1dash.py alwayson $INTERVAL_ARGS \
    </dev/null >/dev/null 2>&1
echo "f1dash alwayson started (daemon pid: $(cat "$PIDFILE" 2>/dev/null))"

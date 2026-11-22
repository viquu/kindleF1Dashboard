#!/bin/bash
# 部署 F1 Dashboard 到 Kindle(依赖 ~/.ssh/config 别名 kindle)。
# 幂等可重跑:代码变更后再次执行即可增量更新。
#
# 常显模式注意:每小时整点拉图,Wi-Fi 开窗保持 5 分钟(:00-:05),SSH 在窗口内
# 随便连;要更长的运维时间,先在窗口内 touch /mnt/us/f1dash/wifi_hold(守护本轮
# 豁免硬关,用完删掉即恢复)。脚本第一件事仍是探测并杀掉常显守护 —— 守护死了
# 没人关 Wi-Fi,连接保住,后续上传不慌。
# 用法: bash kindle/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=kindle

echo "==> 探测当前模式(常显则先杀守护,保住 Wi-Fi 连接)"
# cmdline argv 以 NUL 分隔,须先 tr 转空格再 grep;pkill 用 [.] 防止匹配到
# 携带同样字符串的本脚本远程 shell
MODE=$(ssh -o ConnectTimeout=10 "$HOST" 'if test -f /var/run/f1dash_alwayson.pid && tr "\0" " " \
    < "/proc/$(cat /var/run/f1dash_alwayson.pid 2>/dev/null)/cmdline" 2>/dev/null \
    | grep -q "f1dash.py alwayson"; then
  echo alwayson
  pkill -f "f1dash[.]py alwayson"
  rm -f /var/run/f1dash_alwayson.pid
fi' 2>/dev/null || true)
if [ "${MODE:-}" = "alwayson" ]; then
  echo "    常显模式:守护已停(Wi-Fi 保持开启),上传后重启常显"
else
  echo "    非常显(或未连上):按睡眠刷新服务路径重启"
fi

echo "==> 上传主程序与扩展脚本"
scp -q kindle/f1dash.py                             "$HOST:/mnt/us/f1dash/f1dash.py"
scp -q kindle/extensions/f1/menu.json               "$HOST:/mnt/us/extensions/f1/menu.json"
scp -q kindle/extensions/f1/bin/refresh.sh          "$HOST:/mnt/us/extensions/f1/bin/refresh.sh"
scp -q kindle/extensions/f1/bin/start.sh            "$HOST:/mnt/us/extensions/f1/bin/start.sh"
scp -q kindle/extensions/f1/bin/autosleep_toggle.sh "$HOST:/mnt/us/extensions/f1/bin/autosleep_toggle.sh"
scp -q kindle/extensions/f1/bin/always_on_start.sh  "$HOST:/mnt/us/extensions/f1/bin/always_on_start.sh"
scp -q kindle/extensions/f1/bin/always_on_stop.sh   "$HOST:/mnt/us/extensions/f1/bin/always_on_stop.sh"
scp -q kindle/extensions/f1/bin/always_on_toggle.sh "$HOST:/mnt/us/extensions/f1/bin/always_on_toggle.sh"
ssh "$HOST" "chmod +x /mnt/us/extensions/f1/bin/*.sh"

echo "==> 重启守护进程(按部署前的模式)"
if [ "${MODE:-}" = "alwayson" ]; then
  ssh "$HOST" 'sh /mnt/us/extensions/f1/bin/always_on_start.sh'
else
  ssh "$HOST" "sh /mnt/us/extensions/f1/bin/stop.sh; sh /mnt/us/extensions/f1/bin/start.sh"
fi

echo "==> 验证"
ssh "$HOST" 'ls -la /mnt/us/linkss/screensavers/bg_ss00.png; \
  tail -5 /mnt/us/f1dash/service.log 2>/dev/null; \
  grep -o "Auto Sleep: [A-Z]*" /mnt/us/extensions/f1/menu.json | head -1'

echo "==> 完成"

#!/bin/bash
# 部署 F1 Dashboard 到 Kindle(依赖 ~/.ssh/config 别名 kindle)。
# 幂等可重跑:代码变更后再次执行即可增量更新。
#
# 用法: bash kindle/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=kindle

echo "==> 上传主程序与扩展脚本"
scp -q kindle/f1dash.py                             "$HOST:/mnt/us/f1dash/f1dash.py"
scp -q kindle/extensions/f1/menu.json               "$HOST:/mnt/us/extensions/f1/menu.json"
scp -q kindle/extensions/f1/bin/refresh.sh          "$HOST:/mnt/us/extensions/f1/bin/refresh.sh"
scp -q kindle/extensions/f1/bin/start.sh            "$HOST:/mnt/us/extensions/f1/bin/start.sh"
scp -q kindle/extensions/f1/bin/autosleep_toggle.sh "$HOST:/mnt/us/extensions/f1/bin/autosleep_toggle.sh"
ssh "$HOST" "chmod +x /mnt/us/extensions/f1/bin/*.sh"

echo "==> 重启守护进程(启动即拉一次云端图刷新屏保)"
ssh "$HOST" "sh /mnt/us/extensions/f1/bin/stop.sh; sh /mnt/us/extensions/f1/bin/start.sh"

echo "==> 验证"
ssh "$HOST" 'ls -la /mnt/us/linkss/screensavers/bg_ss00.png; \
  tail -5 /mnt/us/f1dash/service.log 2>/dev/null; \
  grep -o "Auto Sleep: [A-Z]*" /mnt/us/extensions/f1/menu.json | head -1'

echo "==> 完成"

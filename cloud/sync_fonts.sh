#!/bin/bash
# 把 Kindle 系统的 Futura 字体经本机中转拷到 Oracle 云端作为出图字体
# (几何风格与版式最搭;没拷时云端退回 Jost,版式自动适配)。
# Kindle 需在 WiFi 且 usbnet 可达(ssh kindle)。
set -euo pipefail

HOST=ubuntu@oracle
DIR=/opt/f1dash/fonts

ssh "$HOST" "mkdir -p $DIR"

echo "==> Futura-Bold"
ssh kindle 'cat /usr/java/lib/fonts/Futura-Bold.ttf' | \
  ssh "$HOST" "cat > $DIR/Futura-Bold.ttf"
echo "==> Futura-Medium"
ssh kindle 'cat /usr/java/lib/fonts/Futura-Medium.ttf' | \
  ssh "$HOST" "cat > $DIR/Futura-Medium.ttf"

# 校验 TTF 魔数(00 01 00 00),防止拿到错误页内容
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
for f in /opt/f1dash/fonts/Futura-Bold.ttf /opt/f1dash/fonts/Futura-Medium.ttf; do
  [ "$(head -c 4 "$f" | xxd -p)" = "00010000" ] || { echo "BAD TTF: $f"; exit 1; }
done
echo "fonts OK: $(ls -la /opt/f1dash/fonts/Futura-*.ttf)"
REMOTE
echo "==> 下次 timer 触发(或 systemctl restart f1dash-render.service)后即用 Futura 出图"

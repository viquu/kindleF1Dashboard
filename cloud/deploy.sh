#!/bin/bash
# 部署云端渲染服务到 Oracle Cloud(依赖 ~/.ssh/config 别名 oracle / oracle-root)。
# 幂等可重跑:代码变更后再次执行即可增量更新。
#
# 用法: bash cloud/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=ubuntu@oracle          # 普通用户(有免密 sudo)
DIR=/opt/f1dash

echo "==> 上传渲染代码(复用 kindle/f1dash.py,渲染逻辑零分叉)"
ssh "$HOST" "sudo mkdir -p $DIR/fonts $DIR/out && sudo chown -R \$(id -u):\$(id -g) $DIR"
rsync -q kindle/f1dash.py  "$HOST:$DIR/f1dash.py"
rsync -q kindle/tracks.json "$HOST:$DIR/tracks.json"   # 赛道轮廓(缺了轮廓区留白)
rsync -q kindle/f1icon.json "$HOST:$DIR/f1icon.json"   # 页眉头盔图标(缺了页眉无标)
rsync -q kindle/f1logo.json "$HOST:$DIR/f1logo.json"   # F1 官方徽标(缺了页眉无标)
rsync -q cloud/render.py   "$HOST:$DIR/render.py"
rsync -q cloud/fonts/*.ttf "$HOST:$DIR/fonts/"         # Barlow 出图字体(OFL)

echo "==> Python venv + 依赖"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/f1dash
if [ ! -x venv/bin/python ]; then
  dpkg -s python3.14-venv >/dev/null 2>&1 || sudo apt-get install -y -q python3.14-venv
  python3 -m venv venv
fi
venv/bin/pip install --quiet --disable-pip-version-check pillow unidecode certifi

# 字体:Barlow Semi Condensed(deploy.sh 已从 cloud/fonts/ 同步)为主,
# Futura 副本可选(sync_fonts.sh);都没有才下载 Jost(OFL 可变字体)作后备
if [ ! -f fonts/BarlowSemiCondensed-Bold.ttf ]; then
  if [ ! -f fonts/Jost.ttf ]; then
    echo "==> 下载 Jost 后备字体"
    curl -fsSL -o fonts/Jost.ttf \
      "https://raw.githubusercontent.com/google/fonts/main/ofl/jost/Jost%5Bwght%5D.ttf"
  fi
fi
REMOTE

echo "==> systemd service + timer(每 10 分钟渲染一次)"
rsync -q cloud/systemd/f1dash-render.service cloud/systemd/f1dash-render.timer "$HOST:/tmp/"
ssh "$HOST" "sudo mv /tmp/f1dash-render.service /tmp/f1dash-render.timer /etc/systemd/system/ && \
  sudo systemctl daemon-reload && \
  sudo systemctl enable --now f1dash-render.timer && \
  sudo systemctl restart f1dash-render.service"

echo "==> nginx 静态服务(:8080 -> /opt/f1dash/out/)"
ssh "$HOST" "dpkg -s nginx >/dev/null 2>&1 || sudo apt-get install -y -q nginx"
rsync -q cloud/nginx/f1dash.conf "$HOST:/tmp/"
ssh "$HOST" "sudo mv /tmp/f1dash.conf /etc/nginx/sites-available/f1dash.conf && \
  sudo ln -sf /etc/nginx/sites-available/f1dash.conf /etc/nginx/sites-enabled/f1dash.conf && \
  sudo rm -f /etc/nginx/sites-enabled/default && \
  sudo nginx -t && sudo systemctl reload nginx"

echo "==> iptables 放行 8080(VCN Security List 若未放行需在 Oracle 控制台加规则)"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
sudo iptables -C INPUT -p tcp -m tcp --dport 8080 -j ACCEPT 2>/dev/null || \
  sudo iptables -I INPUT 6 -p tcp -m tcp --dport 8080 -j ACCEPT
# 持久化(Oracle Ubuntu 镜像用 iptables-persistent)
if command -v netfilter-persistent >/dev/null 2>&1; then
  sudo netfilter-persistent save
fi
REMOTE

echo "==> 遥测接收端(telemetry_server.py + systemd + nginx /api/ 代理)"
rsync -q cloud/telemetry_server.py "$HOST:$DIR/telemetry_server.py"
rsync -q cloud/systemd/f1dash-telemetry.service "$HOST:/tmp/"
ssh "$HOST" "sudo mv /tmp/f1dash-telemetry.service /etc/systemd/system/ && \
  sudo systemctl daemon-reload && \
  sudo systemctl enable --now f1dash-telemetry.service"
# nginx 配置已含 /api/ 代理(上面已同步并 reload);下面只做一次配置校验
ssh "$HOST" "sudo nginx -t"

echo "==> 自更新发布件(Kindle 下一轮自检用;不碰生产渲染代码)"
# f1dash.py 与版本清单放到 nginx 静态目录:Kindle 按清单验签→验 sha 自检,
# 不同才下载。清单由 cloud/sign_release.py 生成(Ed25519 签名,私钥只在
# Muse 的 VM 上,Ken 的 Mac 不参与签名——没有私钥的机器上跑这一步会直接
# 失败退出,绝不发无签名版本,因为 Kindle 会以 bad_signature 拒绝它)。
# command.json 只在缺失时建初始文件,已有指令绝不覆盖。
REV=$(git rev-parse HEAD 2>/dev/null || echo unknown)
python3 cloud/sign_release.py --in kindle/f1dash.py \
  --out /tmp/f1dash.version.json --version "$REV"
rsync -q kindle/f1dash.py "$HOST:$DIR/out/f1dash.py"
rsync -q /tmp/f1dash.version.json "$HOST:$DIR/out/f1dash.version.json"
ssh "$HOST" "test -f $DIR/out/command.json || printf '%s' '{\"id\": 0, \"action\": \"none\"}' > $DIR/out/command.json && \
  chmod 644 $DIR/out/f1dash.py $DIR/out/f1dash.version.json $DIR/out/command.json && \
  ls -l $DIR/out/f1dash.py $DIR/out/f1dash.version.json $DIR/out/command.json"

echo "==> 完成。验证:"
ssh "$HOST" "systemctl list-timers f1dash-render.timer --no-pager | head -3; \
  ls -la /opt/f1dash/out/; systemctl status f1dash-render.service --no-pager -n 5 | tail -8"
echo "公网地址: http://149.118.42.190:8080/f1/latest.png"

#!/usr/bin/env bash
# ⛔⛔⛔ 本脚本已废弃，执行即退出（2026-10-28 硬警告） ⛔⛔⛔
#
# 原因：本脚本硬写 `PANEL_SCHEDULER=0` + `LIGHTER_LIVE_TRADING=0`，照它重部署会把
# **真钱模式静默降级成纯面板**（调度停摆 + 永不下单，而 healthcheck 依旧全绿）。
#
# 唯一权威的重部署路径：oracle1 上的 `sudo python3 /opt/lighter-ma-bot/deploy.py`
# （从运行中容器继承 env/CMD，旧容器留作回滚点，详见 docs/DEPLOY.md §3）。
echo "⛔ scripts/deploy_oracle1.sh 已废弃：它会静默把真钱模式降级为纯面板。" >&2
echo "   重部署请用：sudo python3 /opt/lighter-ma-bot/deploy.py --expect-rev <sha前缀>" >&2
echo "   说明见 docs/DEPLOY.md §3。本脚本保留仅为历史记录，请勿删除这段警告。" >&2
exit 1

# 在 oracle1 上部署 lighter-ma-bot
# 用法: sudo bash deploy_lighter.sh
#
# 前提（都已就位）：
#   * 站点 lt.222677.xyz 已建，证书有效
#   * sing-box 代理在 127.0.0.1:8118（只把 Lighter 域名分流到日本节点）
#   * /opt/lighter-ma-bot/.env 已就位（权限 600，root only）
#   * 反代 root.conf + htpasswd.txt 已放好
#
# 端口：
#   公网 443 -> nginx -> 127.0.0.1:8110 -> 容器 8100
#   8110 而不是 8100：8100 已被旧的 crypto-ma-alert 容器占用。
#   容器用 --network host，这样容器内的 127.0.0.1:8118 直通宿主 sing-box。
set -euo pipefail

APP=lighter-ma-bot
DIR=/opt/$APP
IMG=ghcr.io/larkspur8506/lighter-ma-bot:latest
PORT=8110
SITE=/opt/1panel/www/sites/lt.222677.xyz

echo "== 1/5 检查前置 =="
[ -f "$DIR/.env" ] || { echo "缺少 $DIR/.env"; exit 2; }
chmod 600 "$DIR/.env"
ss -tlnp 2>/dev/null | grep -q ":8118" || { echo "sing-box 未监听 8118"; exit 2; }
ss -tlnp 2>/dev/null | grep -q ":8110 " && { echo "8110 已被占用"; exit 2; } || true
mkdir -p "$DIR"/{data,logs}
# 容器以 uid 10001(bot) 运行：data/logs 必须归它，否则 store 建库 PermissionError。
chown -R 10001:10001 "$DIR/data" "$DIR/logs"
chmod 750 "$DIR/data" "$DIR/logs"
# 目录本身要可遍历（bot 不是 root，也不在 root 组），但 .env 保持 600 root ⇒ bot 读不到私钥。
chmod 755 "$DIR"
echo "  .env ✅  代理 ✅  目录权限 ✅"

echo "== 2/5 拉镜像 =="
sudo docker pull "$IMG"

echo "== 3/5 停旧容器 =="
sudo docker rm -f "$APP" 2>/dev/null || true

echo "== 4/5 启动 =="
# PANEL_SCHEDULER=0  → 不跑 tick（纯面板）。上线前再改成 1。
# LIGHTER_LIVE_TRADING=0 → 真实下单三道门的第一道，闭合。
# DB_PATH 用容器内路径 /srv/data（宿主同名目录被挂进去）——
# 用宿主路径会因容器看不到 /opt/lighter-ma-bot 而 PermissionError。
sudo docker run -d --name "$APP" --restart unless-stopped \
  --network host \
  --env-file "$DIR/.env" \
  -e PANEL_SCHEDULER=0 \
  -e LIGHTER_LIVE_TRADING=0 \
  -v "$DIR/data:/srv/data" \
  -v "$DIR/logs:/srv/logs" \
  "$IMG" \
  uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --no-access-log >/dev/null

sleep 14
echo "  容器: $(sudo docker inspect -f '{{.State.Status}} | health={{.State.Health.Status}}' $APP)"
echo "  health: $(curl -s --max-time 10 http://127.0.0.1:$PORT/health)"

echo "== 5/5 reload nginx =="
sudo docker exec 1Panel-openresty-MKvt openresty -t
sudo docker exec 1Panel-openresty-MKvt openresty -s reload
sleep 2

echo
echo "=== 验证（需要 Basic Auth）==="
echo "  curl -u panel:<password> https://lt.222677.xyz/health"

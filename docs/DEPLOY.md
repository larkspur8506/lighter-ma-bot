# 部署（oracle1）

部署目标：`oracle1`（64.181.225.174，Ubuntu，美国圣何塞）。
面板入口：<https://lt.222677.xyz>（Basic Auth 保护）。

------------------------------------------------------------------------
## 0. 一句话现状

镜像 `ghcr.io/larkspur8506/lighter-ma-bot:latest` 由 CI 构建推送，
oracle1 上以容器运行；nginx（1Panel openresty）反代到 `127.0.0.1:8110`，
前面挂 Basic Auth；容器走宿主上的 sing-box 代理访问 Lighter。

------------------------------------------------------------------------
## 1. 端口与路径

| 层 | 值 | 说明 |
|---|---|---|
| 公网 | `https://lt.222677.xyz` (443) | 证书 CN=lt.222677.xyz，至 2027-01-25 |
| nginx | → `http://127.0.0.1:8110` | `/opt/1panel/www/sites/lt.222677.xyz/proxy/root.conf` |
| 宿主机 | `127.0.0.1:8110` → 容器 `8100` | docker-proxy；容器 `--network host` |
| 容器内 | `8100` | uvicorn，`--host 127.0.0.1`（不直接暴露公网） |
| 代理 | `127.0.0.1:8118` | sing-box HTTP inbound，**只在宿主上** listen |

**为什么是 8110 不是 8100**：8100 已被旧的 `crypto-ma-alert` 容器占用
（`127.0.0.1:8100->8000`）。

**为什么用 `--network host`**：容器内要访问宿主 sing-box 的
`127.0.0.1:8118`。sing-box 只 listen `127.0.0.1`，容器用 bridge 网络时
`host.docker.internal` 也连不上；共享网络栈最直接。代价是容器不能做
端口映射，所以 uvicorn 端口必须避开已占用的 8100。

------------------------------------------------------------------------
## 2. 凭据与文件

| 文件 | 权限 | 内容 |
|---|---|---|
| `/opt/lighter-ma-bot/.env` | `600 root:root` | API 私钥、`ADMIN_TOKEN`、`DB_PATH` 等 |
| `/opt/lighter-ma-bot/data/` | `750 10001:10001` | `state.db`（SQLite） |
| `/opt/lighter-ma-bot/logs/` | `750 10001:10001` | 容器日志落盘 |
| `/opt/1panel/www/sites/lt.222677.xyz/htpasswd.txt` | `640 root:root` | Basic Auth 账号 |

**目录权限的坑**：容器以 `uid 10001(bot)` 运行。
`/opt/lighter-ma-bot` 若是 `750 root:root`，bot **进不去**（不是 root 也不在
root 组）⇒ 报 `PermissionError: '/opt/lighter-ma-bot'`。
所以目录本身 `755`，`data`/`logs` `750` 且属主是 bot，而 `.env` 保持
`600 root` ⇒ bot 能进目录、能写 DB，但**读不到私钥**。

**`DB_PATH` 必须是容器内路径**（`/srv/data/state.db`）：宿主路径
`/opt/lighter-ma-bot/...` 在容器里不存在 ⇒ 又一个 `PermissionError`。

------------------------------------------------------------------------
## 3. 部署步骤

```bash
# 1) 镜像
sudo docker pull ghcr.io/larkspur8506/lighter-ma-bot:latest

# 2) 停旧容器
sudo docker rm -f lighter-ma-bot 2>/dev/null || true

# 3) 起容器
sudo docker run -d --name lighter-ma-bot --restart unless-stopped \
  --network host \
  --env-file /opt/lighter-ma-bot/.env \
  -e PANEL_SCHEDULER=0 \
  -e LIGHTER_LIVE_TRADING=0 \
  -v /opt/lighter-ma-bot/data:/srv/data \
  -v /opt/lighter-ma-bot/logs:/srv/logs \
  ghcr.io/larkspur8506/lighter-ma-bot:latest \
  uvicorn app.main:app --host 127.0.0.1 --port 8110

# 4) nginx
sudo docker exec 1Panel-openresty-MKvt openresty -t
sudo docker exec 1Panel-openresty-MKvt openresty -s reload
```

**`--env-file` 只在 `docker run` 时读一次** —— 改了 `.env` 必须重建容器，
`docker restart` 不生效（会读到旧值）。

------------------------------------------------------------------------
## 4. 当前的安全开关

| 开关 | 值 | 含义 |
|---|---|---|
| `PANEL_SCHEDULER` | `0` | 不跑 tick —— 只有面板，不产生任何交易动作 |
| `LIGHTER_LIVE_TRADING` | `0` | 真实下单三道门的第一道闭合 |
| Basic Auth | 开 | nginx 层，保护**只读**端点 |
| `ADMIN_TOKEN` | 设 | 应用层，保护**写**端点（`/admin/*`） |

**上线前**需要改的只有 `PANEL_SCHEDULER=1`（和真实下单的三道门）。
改法：改 `.env` 不够（那是 `--env-file` 读的），要在 `docker run` 的
`-e` 里改，或者改完重建容器。

------------------------------------------------------------------------
## 5. 代理（sing-box）

宿主 `127.0.0.1:8118` 是 sing-box 的 HTTP inbound。分流规则只把
`zklighter.elliot.ai` / `lighter.xyz` / `lighter.com` 走日本节点，
其余 `direct`。

**为什么需要**：Lighter 的 API 服务端对受限地区的出口返回
`{"code":20558}`。直连 oracle1（美国圣何塞）会被拦；经日本节点放行。

节点与配置见 `docs/PROXY.md`。

------------------------------------------------------------------------
## 6. 排障

| 现象 | 原因 | 处置 |
|---|---|---|
| `PermissionError: '/opt/lighter-ma-bot'` | 目录 `750 root:root`，bot 进不去 | `chmod 755` 目录 + `chown 10001:10001` data/logs |
| 403 + `Server: CloudFront` | 路径重复 `/api/v1/api/v1/...` | 已修（`_site_root`）；新代码无需处理 |
| 403 + `Server: CloudFront` | SDK 没带 User-Agent | 已修（`_configure_sdk_user_agent`） |
| 403 + `Server: CloudFront` | 鉴权端点没带 Authorization | 已修（`auth_headers()` 显式传 `_headers`） |
| `{"code":20558}` | 出口在受限地区 | 查 sing-box 是否在跑、域名分流是否命中 |
| `invalid auth: invalid deadline` | auth token 的 deadline 传了绝对值 | 传**相对秒数** |
| 面板「没有权益点」 | tick 从未跑过 | `PANEL_SCHEDULER=1` 或手动触发一轮 |

------------------------------------------------------------------------
## 7. 验收（已实测）

```
health        200  {"ok":true,"service":"lighter-ma-bot","version":"0.2.0"}
/status       200
/positions    200
/stops        200
/pnl          200
/symbols      200
/events       200
/             200  (HTML 7.0KB，6 个区块，零外链)

无凭证        401
错误凭证      401
正确凭证      200

旧站点 ma.222677.xyz  未受影响
```

账户读取（经代理）：`equity=101.0`，持仓 0，挂单 0，
`orders_error` 为空（鉴权成功）。

------------------------------------------------------------------------
## 8. 未做（阶段二）

* 调度器上线（`PANEL_SCHEDULER=1`）
* 真实下单演练（dry-run → 小额 → 正式）
* `stop_max` / `stop_min` 等旧系统参数的迁移确认
* 面板 token 与 1Panel 站点的自动化联动

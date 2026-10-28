# 部署（oracle1）

部署目标：`oracle1`（64.181.225.174，Ubuntu，美国圣何塞）。
面板入口：<https://lt.222677.xyz>（**免密可看只读面板**，2026-10-28 移除 Basic Auth；
admin 写操作走应用层 `?k=<ADMIN_TOKEN>` 链接）。

------------------------------------------------------------------------
## 0. 一句话现状

镜像 `ghcr.io/larkspur8506/lighter-ma-bot:latest` 由 CI 构建推送，
oracle1 上以容器运行；nginx（1Panel openresty）反代到 `127.0.0.1:8110`（无 Basic
Auth）；容器走宿主上的 sing-box 代理访问 Lighter。

**重部署一律用 `/opt/lighter-ma-bot/deploy.py`**（从运行中的容器重建 docker run，
env/挂载全继承、旧容器留作回滚点、health 轮询失败自动回滚）——
**不要用本仓库的 `scripts/deploy_oracle1.sh`**：它硬写 `PANEL_SCHEDULER=0` +
`LIGHTER_LIVE_TRADING=0`，照它跑会把真钱模式**静默降级成纯面板**（该脚本已加硬警告，
执行即退出）。

------------------------------------------------------------------------
## 1. 端口与路径

| 层 | 值 | 说明 |
|---|---|---|
| 公网 | `https://lt.222677.xyz` (443) | 证书 CN=lt.222677.xyz，至 2027-01-25 |
| nginx | → `http://127.0.0.1:8110` | `/opt/1panel/www/sites/lt.222677.xyz/proxy/root.conf` |
| 宿主机 | `127.0.0.1:8110` | 容器 `--network host`，进程直接 listen 8110 |
| 容器内 | `8110` | uvicorn，`--host 127.0.0.1`（不直接暴露公网） |
| 代理 | `127.0.0.1:8118` | sing-box HTTP inbound，**只在宿主上** listen |

**为什么是 8110 不是 8100**：8100 已被旧的 `crypto-ma-alert` 容器占用
（`127.0.0.1:8100->8000`）。镜像默认 CMD 是 8100，重建容器**必须带 CMD 覆盖**
`--port 8110 --no-access-log`（deploy.py 会继承运行中容器的 CMD，不用手写）。

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
| `/opt/lighter-ma-bot/backups/` | `700 root:root` | state.db 每日快照（保留 30 天，Hermes 侧 cron 拉回归档） |

**目录权限的坑**：容器以 `uid 10001(bot)` 运行。
`/opt/lighter-ma-bot` 若是 `750 root:root`，bot **进不去**（不是 root 也不在
root 组）⇒ 报 `PermissionError: '/opt/lighter-ma-bot'`。
所以目录本身 `755`，`data`/`logs` `750` 且属主是 bot，而 `.env` 保持
`600 root` ⇒ bot 能进目录、能写 DB，但**读不到私钥**。

**`DB_PATH` 必须是容器内路径**（`/srv/data/state.db`）：宿主路径
`/opt/lighter-ma-bot/...` 在容器里不存在 ⇒ 又一个 `PermissionError`。

------------------------------------------------------------------------
## 3. 部署步骤（2026-10-28 起，deploy.py 是唯一权威路径）

```bash
# 在 oracle1 上：
sudo python3 /opt/lighter-ma-bot/deploy.py --expect-rev <目标 sha 前 7 位>
```

deploy.py 的动作：pull → 校验镜像 revision label → 从**运行中容器** docker inspect
重建 `docker run`（env/mounts/network/user/workdir/CMD/healthcheck 全继承，私钥不过
文件）→ 停旧容器并改名 `lighter-ma-bot-prev` 留作回滚 → 启动 → 轮询 `127.0.0.1:8110/health`
→ 失败自动回滚。

```bash
# 回滚一行：
sudo docker rm -f lighter-ma-bot && sudo docker rename lighter-ma-bot-prev lighter-ma-bot && sudo docker start lighter-ma-bot
```

**`--env-file` 只在 `docker run` 时读一次** —— 改了 `.env` 必须重建容器，
`docker restart` 不生效（会读到旧值）。

**`--no-access-log` 不是省事，是必须**：`ADMIN_TOKEN` 从 `?k=` 进来，而 uvicorn 的
access log 会把**整条带 query 的 request line** 写进 `docker logs`
（2026-10-28 实测，面板按钮点一次就落一条明文 token）。关掉 access log 后，
排障改用应用自己的 `events` 表与容器 stdout（启动/异常仍会打印）。

------------------------------------------------------------------------
## 3b. 写路径冒烟：最小真实往返（**真钱，单位数 USDC**）

读路径与 nonce/auth 通了，不代表「签名→sendTx→挂止损→撤单→平仓」通。第一次真实信号
之前，用这条把整条链路打穿（失败点会自己收尾：撤单 + 市价平掉）：

```bash
sudo docker exec -i -u bot lighter-ma-bot python /srv/scripts/smoke_live_order.py \
  --confirm-live 764377            # account_index 必须与 .env 一致

# 只签名不发请求（先看订单长什么样）
sudo docker exec -i -u bot lighter-ma-bot python /srv/scripts/smoke_live_order.py \
  --confirm-live 764377 --dry-run
```

安全边界写死在脚本里：单笔、只做多、名义 ≤ 25 USDC（默认 12，等于 SOL 的最小合法量），
开始前必须**空仓无挂单**，结束时必须**空仓零挂单**，否则非零码退出要求人工核对。

------------------------------------------------------------------------
## 4. 当前的安全开关

| 开关 | 值 | 含义 |
|---|---|---|
| `PANEL_SCHEDULER` | `1` | tick 每 30 分钟一轮（UTC :03/:33），真钱调度在跑 |
| `LIGHTER_LIVE_TRADING` | `1` | 真实下单三道门的第一道**已开**（实盘） |
| Basic Auth | **已移除**（2026-10-28） | 只读面板免密；admin 写操作靠 `ADMIN_TOKEN` |
| `ADMIN_TOKEN` | 设 | 应用层，保护**写**端点（`/admin/*`），`?k=` 注入 localStorage |
| state.db 备份 | 每日 07:00 UTC | 容器内 backup API 快照 → Hermes 侧拉回本机归档，各留 30 天 |

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

* `stop_max` / `stop_min` 等旧系统参数的迁移确认
* 面板 token 与 1Panel 站点的自动化联动

（调度器与真实下单已于 2026-10-28 上线：`PANEL_SCHEDULER=1`、`LIGHTER_LIVE_TRADING=1`，
smoke 真钱往返多轮 10/10。）

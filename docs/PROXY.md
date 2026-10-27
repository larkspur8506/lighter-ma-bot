# 代理（`LIGHTER_PROXY_URL`）

> 一句话：**读端点直连就够，交易端点必须换出口。** 本仓库只把 Lighter 域名送进一个
> 本地 HTTP 代理，其余流量（探针、第三方行情）保持直连。
>
> 客户端实现：`app/proxy.py`（判定 + 注入）、`app/config.py`（读配置 + 脱敏）、
> `app/exchange.py`（SDK 注入）、`app/market.py`（urllib 注入）。
> 测试：`tests/test_proxy.py`。

---

## 1. 为什么需要代理（实测，不是推断）

Lighter 的**交易**端点有服务端 IP 地域拦截。从美国 IP 直连：

```console
$ curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
    https://mainnet.zklighter.elliot.ai/api/v1/sendTx
{"code":20558,"message":"You are accessing Lighter from a restricted jurisdiction..."}
```

而同一台机器上的**读**端点完全正常（`/orderBooks`、`/candles`、`/orderBookDetails`
都返回 200）。所以这不是「网络不通」：**同一个 host、同一套 TLS、同一个进程，只是
`sendTx` 被拒**。加重试、换 DNS、调 MTU 都没有意义。

实测环境与解法（2026-10-27，oracle1，美国圣何塞 `64.181.225.174`）：

| 路径 | 结果 |
|---|---|
| 直连 `POST /api/v1/sendTx` | `{"code":20558,...}` 地域拦截 |
| `curl -x http://127.0.0.1:8118 -X POST /api/v1/sendTx` | `{"code":20001,"message":"invalid param "}` ← **已过地域检查**（参数错是因为请求体是空 `{}`） |
| 直连 `GET /api/v1/orderBooks` | 200，正常 |

`127.0.0.1:8118` 是本机 sing-box 的 HTTP 入站；出站是日本节点（VLESS），并且**只对
Lighter 域名分流**，其它流量直连。sing-box 日志可以逐条看到分流结果：

```
INFO [outbound/vless[jp-vless]] ... mainnet.zklighter.elliot.ai:443
INFO [outbound/direct[direct]]  ... ip-api.com:80
```

结论：`20001` 而不是 `20558` 就说明出口 IP 已经不在受限地区，**代理链路是通的**。
换节点后请用第 6 节的命令重新验证这两条，不要凭「进程起来了」判断。

> **验证必须在生产机上做。** 上面的 `20558` 是 oracle1（美国圣何塞）测到的。在非受限
> 地区（例如开发机）直连 `sendTx` 也会返回 `20001` —— 那种机器上「直连 vs 代理」没有
> 差别，用它验证会得到一个假的「不用代理也行」。

---

## 2. 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `LIGHTER_PROXY_URL` | 空（**直连**） | 本地/远程 HTTP 代理，形如 `http://127.0.0.1:8118` |

规则（`app/proxy.py::parse_proxy_url`）：

* **空 / 未设置 = 直连**。这是最重要的默认值：没配代理时必须真直连，而不是偷偷读
  `HTTPS_PROXY` / `ALL_PROXY`。
* **非法值 = 启动失败**（`ConfigError`，消息里只有变量名与原因）。**绝不静默忽略** ——
  「以为走代理其实直连」的失败只会在第一次 `sendTx` 时以 `20558` 出现，那时已经很难
  把它和「节点挂了」「网络抖动」区分开。宁可在启动时拒绝。
* 只接受 `http://` / `https://`。`socks5://` 会被明确拒绝：urllib 那条路需要
  `PySocks`、SDK 那条路需要 `aiohttp-socks`，而本仓库**不引入新依赖**
  （`requirements.txt` 冻结）。要 SOCKS 就先改依赖决策，不要指望现在能用。
* 允许带凭据：`http://user:pass@host:port`。凭据会原样用于认证，但**打印时一律脱敏**
  （`app/proxy.py::redact_proxy_url` → `http://user:***@host:port`）。
* 不允许带路径 / query / fragment（对 HTTP 代理没有意义，且容易让人以为在配分流规则）。

取值示例：

```bash
# 直连（默认，也是不要把这一行留空的唯一含义）
LIGHTER_PROXY_URL=

# 本机 sing-box 的 HTTP 入站
LIGHTER_PROXY_URL=http://127.0.0.1:8118

# 带认证的代理（.env 里这样写；日志里显示为 http://bot:***@10.0.0.5:3128）
LIGHTER_PROXY_URL=http://bot:REPLACE_ME@10.0.0.5:3128
```

---

## 3. 「只对 Lighter 走代理」的判定与防误伤

`app/proxy.py::should_proxy(url, proxy_url)` 是**纯函数**：

```python
should_proxy(url, proxy_url):    # 没配代理 -> False；host 属于 Lighter -> True；否则 False
```

host 集合（`LIGHTER_HOST_SUFFIXES`）：

| 后缀 | 覆盖的端点 |
|---|---|
| `zklighter.elliot.ai` | 主网 / 测试网 REST + WS（`mainnet.` / `testnet.` 前缀） |
| `lighter.xyz` | RH 独立交易所（`api.rh.lighter.xyz`、`api.rh-testnet.lighter.xyz`） |
| `lighter.com` | 官网 / 前端（同一组织，未来可能接 API） |

匹配是**精确相等或以 `.` + 后缀结尾**（点边界），先经 `urllib.parse` 取 host：

| URL | 命中 | 原因 |
|---|---|---|
| `https://mainnet.zklighter.elliot.ai/api/v1/orderBooks` | ✅ | 以 `.zklighter.elliot.ai` 结尾 |
| `https://api.rh.lighter.xyz/api/v1` | ✅ | 以 `.lighter.xyz` 结尾 |
| `https://apiv2.lighter.com/...` | ✅ | 以 `.lighter.com` 结尾 |
| `https://example.com` | ❌ | 无关域 |
| `https://evil-zklighter.elliot.ai.com` | ❌ | 后缀不成立（**防误伤**） |
| `https://zklighter.elliot.ai.evil.com` | ❌ | 后缀不成立 |
| `https://notzklighter.elliot.ai` | ❌ | 点边界不成立（少一个 `.`） |
| `https://zklighter.elliot.ai@evil.com/x` | ❌ | 取的是 userinfo 之后的 host（`evil.com`） |

不用 `host.endswith("lighter.xyz")` 这种写法：它会把
`notlighter.xyz`、`evil-lighter.xyz` 一起带进去。也不手写字符串切分去 `@` 和 `:` ——
`urlsplit().hostname` 已经把 userinfo、端口、IPv6 方括号都处理好了。

**为什么判定仍然逐 URL 做**：本项目当前只访问 Lighter，所以实际上「全走代理」；
但 `app/market.py` 里将来可能混进探针/第三方地址（比如查出口 IP 的 `ip-api.com`），
那时候不能把探测流量也甩进日本节点 —— 那会把「查我自己的出口 IP」变成「查节点的出口 IP」。

---

## 4. 注入点（两条独立路径，都不改全局环境变量）

### 4.1 urllib（`app/market.py`）

`urllib.request.urlopen` **默认不读** `HTTPS_PROXY`。所以由 `app/proxy.py` 显式建两个
opener：

* 直连：`build_opener(ProxyHandler({}))` —— 空字典 = 连环境变量里的代理也**不认**；
* 代理：`build_opener(ProxyHandler({"http": P, "https": P}))`；
* 每次请求按 `should_proxy(url, P)` 选一个。

用法：

```python
MarketClient("mainnet", proxy_url=cfg.proxy_url)      # 配置了代理就用带代理的 getter
MarketClient("mainnet")                               # 不传 = 原来的纯 urllib 直连
MarketClient("mainnet", http_get=fake)                # 显式注入永远优先（测试用）
```

`_urllib_get_json` 本身没改；代理版 getter 与它同签名（`(url, timeout=...)` → JSON），
可以互换。

### 4.2 官方 SDK（`app/exchange.py`）

SDK 原生支持，但**不是**构造参数：`lighter.Configuration.__init__` 没有 `proxy=` 关键字
（实测 `lighter-sdk==1.1.8` 会抛 `TypeError: Configuration.__init__() got an unexpected
keyword argument 'proxy'`），`self.proxy: Optional[str] = None` 是构造完才赋的普通属性
（`configuration.py:190`）。链路是：

```
Configuration.proxy  ──抄──>  rest_client.proxy (rest.py:76)
                     ──塞──>  aiohttp 请求的 proxy= (rest.py:159-162)
```

关键在**时机**：`RestClientObject.__init__` 在 `ApiClient` 构造时就把 `configuration.proxy`
读走了，所以必须在 `ApiClient` 被建**之前**把属性赋上。SDK 内部的
`Configuration(host=url)` 是写死的、没有入参，因此 `LighterSdk` 用
`exchange._inject_configuration_proxy(proxy_url)` 在**构造期间**把
`lighter.signer_client` 模块里的 `Configuration` 名字临时换成一个包装（先按原样构造，
再赋 `proxy`），构造完在 `finally` 里还原 —— 不动 `lighter.configuration.Configuration`
本体，也不动环境变量。

构造之后还有一次**自证**：读 `signer.api_client.rest_client.proxy`，若与配置值不一致
就抛 `NotConfigured` 拒绝运行。理由同上 —— 悄悄直连的代价是第一次 `sendTx` 才炸。

**为什么不用 `HTTPS_PROXY` 环境变量**（虽然 `rest.py:80-83` 的
`aiohttp.ClientSession(..., trust_env=True)` 会读它）：那是**进程级**开关。一旦设了，
同进程里所有 aiohttp 客户端都会跟着变，包括将来接的面板/告警 webhook/第三方行情。
`Configuration.proxy` 只作用于这一个客户端，范围精确、可测（`tests/test_proxy.py` 里
有断言：构造带代理的 SDK 前后，`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 等一个都没变）。

判定同样先过 `should_proxy`：`base_url` 的 host 不属于 Lighter 时，即使配了代理也**不**注入。

---

## 5. WebSocket：**本版不支持**（明确记录，不假装）

* 本项目的执行器（T8）走 **REST 轮询**（`GET /tx?by=hash`），当前**不用** WS。
* SDK 的 WS 客户端用 `websockets` 库（`lighter/ws_client.py` 顶部
  `from websockets.sync.client import connect`），**该库默认不读代理环境变量**。
  即使设了 `HTTPS_PROXY`，WS 也照样直连 —— 也就是说：**现在配了代理，`/stream` 仍然从
  本机 IP 出去**。
* 因此本版**没有** WS 代理代码，也没有半成品：`app/proxy.py` 里没有 `websockets` import，
  没有 `ws_proxy()` 之类的占位函数（`tests/test_proxy.py` 钉住这一点）。

将来要启用 WS（例如把 `wait_for_tx` 的轮询换成 `account_tx` 推送），必须补：

1. 核对 `websockets` 版本是否支持 `proxy=` 参数（本仓库环境是 17.3；老版本只支持
   `extra_headers` + 自己建连接，或需要 `aiohttp` 的 WS 客户端）；
2. 在**建连接处显式**传 `proxy=cfg.proxy_url`（`websockets.sync.client.connect(...,
   proxy=...)` / `websockets.asyncio.client.connect(..., proxy=...)`），仍然只对
   `wss://mainnet.zklighter.elliot.ai/stream` 这类 Lighter host 生效；
3. 若走本地 HTTP 代理，确认 sing-box 的 HTTP 入站支持 **WS over CONNECT**（HTTP 代理语义
   上就是 `CONNECT host:443`，sing-box 的 http inbound 支持；实机验证一次再上线）；
4. 加一条与 `tests/test_proxy.py` 同风格的离线测试（假 transport 断言 `proxy=` 真被传入），
   以及一条实机检查（`/stream` 能收到 heartbeat，且 `20558` 不再出现）。

在此之前：**不要**在文档或告警里把 WS 写成「已支持代理」。

---

## 6. 运维侧：sing-box 最小配置 + systemd

> 这一节是**运维**内容，不属于代码。仓库里只放占位符 —— 节点地址、UUID、SNI 全在
> `/etc/sing-box/config.json`（`root:singbox 0640`），不进仓库、不进 `.env.example`。

### 6.1 最小配置

```json
{
  "log": { "level": "info", "timestamp": true },

  "inbounds": [
    { "type": "http", "tag": "http-in", "listen": "127.0.0.1", "listen_port": 8118 }
  ],

  "outbounds": [
    {
      "type": "vless",
      "tag": "jp-vless",
      "server": "<JP_NODE_HOST>",
      "server_port": 443,
      "uuid": "<UUID>",
      "flow": "xtls-rprx-vision",
      "tls": { "enabled": true, "server_name": "<SNI>" },
      "transport": { "type": "ws", "path": "/<WS_PATH>", "headers": { "Host": "<SNI>" } }
    },
    { "type": "direct", "tag": "direct" }
  ],

  "route": {
    "rules": [
      {
        "domain_suffix": ["zklighter.elliot.ai", "lighter.xyz", "lighter.com"],
        "outbound": "jp-vless"
      }
    ],
    "final": "direct",
    "auto_detect_interface": true
  }
}
```

要点：

* `listen` 必须是 `127.0.0.1`。绑 `0.0.0.0` 等于把「一个能绕过地域限制的出口」开放给
  整个网络 —— 那是免费的开放代理。
* `route.final = "direct"`：只有 Lighter 域名走日本节点。这与客户端
  `should_proxy()` 的判定是**两层独立防线**：客户端只把 Lighter 送去代理，代理只把
  Lighter 送出国。任何一层配错，另一层还能兜住。
* `domain_suffix` 用 sing-box 自己的后缀语义（点边界），和本文第 3 节一致。
* 版本字段以 **sing-box 1.14.4** 的 schema 为准；升级前先 `sing-box check -c config.json`。

### 6.2 systemd

```ini
# /etc/systemd/system/sing-box.service
[Unit]
Description=sing-box (Lighter trading egress)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=always
RestartSec=5s
# 配置含节点凭据：专用用户 + 收紧权限，别用 root 跑
User=singbox
Group=singbox
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo install -m 0640 -o root -g singbox config.json /etc/sing-box/config.json
sudo systemctl daemon-reload && sudo systemctl enable --now sing-box
sing-box check -c /etc/sing-box/config.json      # 改配置后先 check 再 restart
```

### 6.3 上线验证（两条都要看）

```bash
# 1) 代理链路通不通（读端点，200 就算通）
curl -sS -o /dev/null -w '%{http_code}\n' -x http://127.0.0.1:8118 \
  https://mainnet.zklighter.elliot.ai/api/v1/orderBooks

# 2) 敲交易端点，看 code 是 20001（过了地域检查）还是 20558（还在受限地区）
curl -sS -x http://127.0.0.1:8118 -X POST -H 'Content-Type: application/json' -d '{}' \
  https://mainnet.zklighter.elliot.ai/api/v1/sendTx

# 3) 分流是否生效（应该看到 zklighter -> outbound/vless、ip-api.com -> direct）
journalctl -u sing-box -f | grep -E 'zklighter|ip-api'
```

> 真实下单不要用 `curl` 试 —— `sendTx` 需要合法签名。用
> `scripts/offline_sign.py`（只签名）或 dry-run 的 `ExchangeClient`。

---

## 7. 风险：代理抖动 → 下单超时

代理多了一跳，失败模式也多了一个：**请求可能已经到了 Lighter（下单成功），而响应在
回来的路上丢了**。这种情况`sendTx` 抛的是传输错误，**不能**当成「没下单」重来 —— 重签
一笔会变成双开仓。

`app/exchange.py` 已经按这个前提写的，不要在代理层另做一套重试：

* `ExchangeClient._send_signed`：每次可重试失败、重发之前，若结果可能已经落地（传输
  错误），先 `_resolve_ambiguous(tx_hash)` → `GET /api/v1/tx?by=hash`；
* 已执行 → 直接定论（不重发）；已失败且 nonce 未消耗 → 重发**同一份签名**；查不到 → 也
  重发同一份（`nonce` 不变，sequencer 只接受第一次），所以最多是「重复提交同一个 nonce」，
  不是「重复下单」；
* 重试耗尽仍未知 → `outcome unknown`（T8 的对账/告警接手，不改库）。

给运维的两条操作建议：

1. **别在代理层加重试**（`ProxyHandler` / sing-box 的 `retry` 都不要开自动重试 POST）。
   传输层重发 POST 会把「已下单、响应丢了」变成真正的重复提交风险。
2. 代理延迟直接吃掉下单超时预算：**只在 `sendTx` 上要求低延迟**，读请求慢一点无所谓。
   节点 RTT 变大时先换节点，不要把 `max_attempts` 调高来「压过去」。

---

## 8. 安全：凭据不要进仓库

* `.gitignore` 已覆盖 `.env`（`!.env.example` 例外）、`*.key`、`*.pem`、`secrets/`。
* **`LIGHTER_PROXY_URL` 里的 `user:pass` 也算 secret**，和 `LIGHTER_API_PRIVATE_KEY`、
  `ADMIN_TOKEN` 一个级别：
  * 只写进服务器上的 `.env`（`chmod 600`）或容器 `--env-file`；
  * 不要写进 `.env.example`（那里只有一行空值 + 注释）；
  * 不要贴进 issue / PR / 聊天 / 告警；
  * 代码与日志里一律用 `app/proxy.py::redact_proxy_url()` 输出（`Config.public()`、
    `Config.__repr__`、`ExchangeClient.__repr__`、`NotConfigured` 的异常消息都走它）。
* 提交前自查：

  ```bash
  git grep -nE 'LIGHTER_PROXY_URL=\S+' -- . ':!.env.example'   # 应该只剩空值/占位符
  ```

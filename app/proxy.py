"""代理支持层：决定「哪些请求走代理、怎么走」，并把它注入两个 HTTP 客户端。

为什么需要这一层（实测 2026-10-27，oracle1 / 美国圣何塞 `64.181.225.174`）：

* Lighter 的**交易**端点（`POST /api/v1/sendTx`、`/sendTxBatch`、WS `/stream`）有服务端
  IP 地域拦截。从美国 IP 直连会拿到
  `{"code":20558,"message":"You are accessing Lighter from a restricted jurisdiction..."}`。
  同一台机器上的**读**端点（`/orderBooks` / `/candles` / `/orderBookDetails`）完全正常 ——
  所以这不是「网络不通」，而是「来源地址被拒」。换 DNS、加重试都不会好。
* 实测解：本机跑 sing-box，提供本地 HTTP 代理 `127.0.0.1:8118`，只把 Lighter 域名分流到
  日本节点，其它流量直连。`curl -x http://127.0.0.1:8118 -X POST .../sendTx` 返回
  `{"code":20001,"message":"invalid param "}`（参数错 —— 说明**已经过了地域检查**），
  而同一台机器直连该端点返回 `20558`；sing-box 日志确认
  `mainnet.zklighter.elliot.ai` → `outbound/vless[jp-vless]`、`ip-api.com` → `direct`。

本模块只负责**客户端这一侧**：读配置、判定、注入。sing-box 的部署与分流是运维的事，
见 `docs/PROXY.md`。

三条被钉住的性质：

1. **默认直连**。`LIGHTER_PROXY_URL` 空/未设置 = 不走代理；没有任何隐式默认代理地址，
   也不会去读 `HTTPS_PROXY` / `ALL_PROXY`。
2. **fail-closed**。URL 非法直接抛 `ProxyConfigError`（空串除外）。静默忽略等于
   「以为走了代理、其实在直连」—— 那种失败会以 20558 的形式出现，比启动就报错难查得多。
3. **凭据不外泄**。代理 URL 可能带 `user:pass@`；`redact_proxy_url()` 是唯一的打印方式，
   本模块所有异常消息都不回显原始值。

注入方式（两条独立路径，都**不**改全局环境变量）：

* `urllib`（`app/market.py`）：`urllib.request.urlopen` 默认**不读** `HTTPS_PROXY`，
  所以这里显式 `build_opener(ProxyHandler({...}))`，按目标 URL 选 opener；直连那条
  opener 用 `ProxyHandler({})`，连环境变量里的代理都显式关掉。
* 官方 SDK（`app/exchange.py`）：`lighter.Configuration.proxy` 是原生字段
  （`configuration.py:190`），`rest.py:159-162` 把它塞进每次请求的 `proxy=` 参数。
  用 `Configuration.proxy` 而**不是** `HTTPS_PROXY`，是因为 `rest.py:80-83` 的
  `aiohttp.ClientSession(connector=..., trust_env=True)` 会读环境变量 —— 一旦设了
  `HTTPS_PROXY`，同进程里所有 aiohttp 客户端都会跟着变。SDK 里 `Configuration(host=url)`
  是写死的（官方 SDK 的 `SignerClient.__init__`，见 `docs/PROXY.md`），所以由
  `app/exchange.py` 在**构造期间**临时替换那次调用用的 `Configuration`，构造完立刻还原。
  （`lighter` 的 import 只允许出现在 `app/exchange.py` —— 这是
  `tests/test_exchange.py::ModuleBoundaryTests` 钉住的边界，本模块不碰。）

WebSocket：**本版不支持**（`lighter/ws_client.py:2` 用 `websockets.sync.client.connect`，
该库默认不读代理环境变量；要支持必须显式传 `proxy=`，且需要 `websockets` 17.x 的
`proxy` 参数 —— 本项目当前用 REST 轮询，尚未启用 WS）。这里**不假装**支持：没有 WS
代理代码，也没有 WS 相关的半成品。将来启用时的做法写在 `docs/PROXY.md`。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_PROXY_SCHEMES", "LIGHTER_HOST_SUFFIXES", "PROXY_ENV_KEY", "REDACTED",
    "ProxyConfigError", "configuration_proxy_for", "http_get_with_proxy",
    "is_lighter_host", "parse_proxy_url", "proxy_url_from_env", "redact_proxy_url",
    "should_proxy", "url_host",
]

PROXY_ENV_KEY = "LIGHTER_PROXY_URL"
REDACTED = "***"

# 只有 HTTP(S) 代理被支持：SOCKS 要 urllib 那条路额外装 PySocks / aiohttp 那条路额外装
# aiohttp-socks，而本仓库**不引入新第三方依赖**（requirements.txt 冻结）。写清支持范围，
# 好过接受 socks:// 却在运行时悄悄直连。
ALLOWED_PROXY_SCHEMES = ("http", "https")

# 「Lighter 自己的域名」集合。判定用**后缀 + 点边界**（见 `is_lighter_host`）：
#   mainnet.zklighter.elliot.ai  ✓（以 `.zklighter.elliot.ai` 结尾）
#   api.rh.lighter.xyz           ✓（RH 独立交易所）
#   zklighter.elliot.ai.evil.com ✗
#   notzklighter.elliot.ai       ✗（`t` 前面没有点，后缀不成立）
LIGHTER_HOST_SUFFIXES = (
    "zklighter.elliot.ai",      # 主网 / 测试网 REST + WS
    "lighter.xyz",              # RH 独立交易所（api.rh.lighter.xyz / api.rh-testnet.lighter.xyz）
    "lighter.com",              # 官网 / 前端（同一组织，未来可能接 API）
)


class ProxyConfigError(ValueError):
    """代理配置非法或 SDK 没有真正用上代理。

    消息里**只有变量名和原因**，不含 URL 原文（可能带 `user:pass`）。
    """


# ------------------------------------------------------------------ 脱敏


def redact_proxy_url(value: Optional[str]) -> str:
    """把 `http://user:pass@host:port` 打成 `http://user:***@host:port`。

    没有凭据的 URL 原样返回（没什么可藏的）；解析不了的返回 `***`。
    空值返回空串 —— 「未配置」由调用方自己表达（`Config.public()` 用 `<unset>`）。
    """
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return REDACTED
    netloc = parts.netloc
    if not netloc:
        # 连 netloc 都没有 = 不是一个 URL。原文可能就是一个被误填的密码，所以吞掉。
        return REDACTED
    if "@" not in netloc:
        return text
    userinfo, _, host = netloc.rpartition("@")
    user, sep, _password = userinfo.partition(":")
    masked = f"{user}{sep}{REDACTED}@{host}" if sep else f"{REDACTED}@{host}"
    return f"{parts.scheme}://{masked}" if parts.scheme else f"//{masked}"


# ------------------------------------------------------------------ 校验


def parse_proxy_url(raw: Optional[str], *, key: str = PROXY_ENV_KEY) -> Optional[str]:
    """校验并规范化代理 URL。返回 `None` 表示**直连**（未配置）。

    空串 / 全空白 / `None` → `None`（默认直连，这是最重要的默认值）。
    其它任何不合法的输入 → `ProxyConfigError`（fail-closed，绝不静默降级成直连）。
    错误消息只带变量名与原因，不回显原始值。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if any(ch.isspace() for ch in text):
        raise ProxyConfigError(f"{key} must not contain whitespace")

    try:
        parts = urlsplit(text)
    except ValueError:
        raise ProxyConfigError(
            f"{key} is not a valid URL (expected http://host:port)"
        ) from None

    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise ProxyConfigError(
            f"{key} must be an absolute URL including the scheme, "
            f"e.g. http://127.0.0.1:8118 (a bare host:port is rejected on purpose)"
        )
    if scheme not in ALLOWED_PROXY_SCHEMES:
        raise ProxyConfigError(
            f"{key} uses scheme {scheme!r}; this build supports only "
            f"{list(ALLOWED_PROXY_SCHEMES)} (a SOCKS proxy would need an extra "
            f"dependency, which this repo does not add)"
        )
    if not parts.hostname:
        raise ProxyConfigError(f"{key} has no host")
    try:
        port = parts.port
    except ValueError:
        raise ProxyConfigError(f"{key} has an invalid port") from None
    if port is not None and not (0 < port < 65536):
        raise ProxyConfigError(f"{key} has an out-of-range port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProxyConfigError(
            f"{key} must point at the proxy root: a path/query/fragment is not "
            f"meaningful for an HTTP proxy"
        )
    # 只保留 scheme://netloc（含 userinfo），去掉末尾斜杠。netloc 原样保留 ——
    # 凭据可能含百分号编码，重新拼装容易改坏。
    return f"{scheme}://{parts.netloc}"


def proxy_url_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """从环境变量读 `LIGHTER_PROXY_URL`（空 = 直连）。校验规则同 `parse_proxy_url`。"""
    source: Mapping[str, str] = env if env is not None else os.environ
    return parse_proxy_url(source.get(PROXY_ENV_KEY))


# ------------------------------------------------------------------ 判定


def url_host(url: str) -> str:
    """取 URL 的 host（小写、去尾部点、去端口、去 userinfo）。取不到返回空串。

    `urlsplit().hostname` 已经把 `user:pass@` 与 `:port` 剥掉、IPv6 的方括号也去掉了，
    这正是我们要比对的东西 —— 手写字符串切分会把 `https://zklighter.elliot.ai@evil.com/`
    这种 userinfo 伎俩判错。
    """
    try:
        parts = urlsplit(str(url or "").strip())
        host = parts.hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".").lower()


def is_lighter_host(host: str) -> bool:
    """host 是否属于 Lighter（精确相等或以 `.`+后缀 结尾 —— 点边界，防后缀误伤）。"""
    name = str(host or "").strip().rstrip(".").lower()
    if not name:
        return False
    for suffix in LIGHTER_HOST_SUFFIXES:
        if name == suffix or name.endswith("." + suffix):
            return True
    return False


def should_proxy(url: str, proxy_url: Optional[str]) -> bool:
    """纯函数：这个目标 URL 该不该走代理。

    * 没配置代理 → 永远 `False`（默认直连）。
    * 目标 host 属于 Lighter → `True`。
    * 其它（探针 / 第三方 URL，比如 `ip-api.com`）→ `False`，即使配置了代理。

    本项目只访问 Lighter，所以实际上「全走代理」；但判定仍然逐 URL 做，
    因为 `app/market.py` 将来可能混入探针/第三方地址，那时不能把它们也甩进日本节点。
    """
    if not proxy_url or not str(proxy_url).strip():
        return False
    return is_lighter_host(url_host(url))


def configuration_proxy_for(url: str, proxy_url: Optional[str]) -> Optional[str]:
    """给 SDK 用：目标 host 该走代理时返回代理 URL，否则 `None`（=`Configuration.proxy=None`）。"""
    return proxy_url if should_proxy(url, proxy_url) else None


# ------------------------------------------------------------------ urllib 注入


def http_get_with_proxy(proxy_url: str, *,
                        headers: Optional[Mapping[str, str]] = None,
                        opener_factory: Optional[Callable[..., Any]] = None,
                        default_timeout: float = 30.0) -> Callable[..., Any]:
    """返回一个 `get(url, timeout=...)`：**只对 Lighter 的 URL** 走代理，其余直连。

    与 `app.market._urllib_get_json` 同签名同语义（GET + 解析 JSON，异常原样抛给调用方
    分类），因此可以直接注入 `MarketClient(http_get=...)`。

    * `opener_factory` 仅供测试注入（默认 `urllib.request.build_opener`）。
    * 直连 opener 用 `ProxyHandler({})`：显式不代理，连 `HTTP(S)_PROXY` 环境变量也不认 ——
      「直连」就该是真直连。
    """
    normalized = parse_proxy_url(proxy_url)
    if not normalized:
        raise ProxyConfigError(
            f"{PROXY_ENV_KEY} is empty, so there is nothing to proxy; use the plain "
            f"direct http_get instead of http_get_with_proxy()"
        )
    build = opener_factory or urllib.request.build_opener
    direct_opener = build(urllib.request.ProxyHandler({}))
    proxied_opener = build(urllib.request.ProxyHandler(
        {"http": normalized, "https": normalized}))
    request_headers: Dict[str, str] = {"Accept": "application/json"}
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    def get(url: str, timeout: float = default_timeout) -> Any:
        opener = proxied_opener if should_proxy(url, normalized) else direct_opener
        request = urllib.request.Request(url, headers=request_headers)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw)

    # 供日志/探针/测试观察「这个 getter 到底配了哪个代理」——只留脱敏后的标签。
    get.proxied = True                                        # type: ignore[attr-defined]
    get.proxy_label = redact_proxy_url(normalized)            # type: ignore[attr-defined]
    return get

"""配置层：只从环境变量读，secret 不进 repr / 日志 / 异常消息。

约定（与 `.env.example` 的键名一一对应，那里是命名权威）：

* **必填**：`LIGHTER_ACCOUNT_INDEX` / `LIGHTER_API_KEY_INDEX` / `LIGHTER_API_PRIVATE_KEY` /
  `LIGHTER_L1_ADDRESS` / `ADMIN_TOKEN`。缺任何一项就抛 `ConfigError` 并**列清缺哪几项** ——
  fail-closed，不用「看起来合理」的默认值顶上（默认值最容易在实盘里静默变成
  「用错账户下单」）。
* **可选（有文档化默认值）**：`LIGHTER_ENVIRONMENT=mainnet`、`DB_PATH=./data/state.db`、
  `DB_URL`（空则从 `DB_PATH` 推导）、`RISK_PCT=0.01`、`HOST=0.0.0.0`、`PORT=8100`、
  `LIGHTER_PROXY_URL`（空 = 直连；判定与注入见 `app.proxy` / `docs/PROXY.md`）。
* **secret**：`LIGHTER_API_PRIVATE_KEY`、`ADMIN_TOKEN`（以及可能带凭据的 `DB_URL`）在
  `repr()` / `str()` / `public()` 里全部脱敏。异常消息只报**变量名**，绝不带回值 ——
  否则一个 PK 格式错误就会把私钥抄进日志和通知。
* **不做**：不读 `.env` 文件（由容器 `--env-file` 或运行环境注入），不写任何密钥到磁盘，
  不做 `os.environ` 的隐式修改。

`LIGHTER_ENVIRONMENT` 决定 API base URL（`mainnet` / `testnet`），映射表在
`app.market.BASE_URLS`，这里只做查表，不复制一份常量。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from app.market import BASE_URLS, DEFAULT_ENVIRONMENT
from app.proxy import PROXY_ENV_KEY, ProxyConfigError, parse_proxy_url, redact_proxy_url

__all__ = [
    "ADMIN_TOKEN_KEY", "DEFAULT_ENVIRONMENT", "IDENTITY_KEYS", "OPTIONAL_KEYS",
    "PROXY_ENV_KEY", "REQUIRED_KEYS", "REDACTED", "SECRET_KEYS", "Config", "ConfigError",
]

REDACTED = "***"
UNSET = "<unset>"

ACCOUNT_INDEX_KEY = "LIGHTER_ACCOUNT_INDEX"
API_KEY_INDEX_KEY = "LIGHTER_API_KEY_INDEX"
API_PRIVATE_KEY_KEY = "LIGHTER_API_PRIVATE_KEY"
L1_ADDRESS_KEY = "LIGHTER_L1_ADDRESS"
ENVIRONMENT_KEY = "LIGHTER_ENVIRONMENT"
ADMIN_TOKEN_KEY = "ADMIN_TOKEN"
DB_PATH_KEY = "DB_PATH"
DB_URL_KEY = "DB_URL"
RISK_PCT_KEY = "RISK_PCT"
HOST_KEY = "HOST"
PORT_KEY = "PORT"

IDENTITY_KEYS = (ACCOUNT_INDEX_KEY, API_KEY_INDEX_KEY, API_PRIVATE_KEY_KEY, L1_ADDRESS_KEY)
REQUIRED_KEYS = IDENTITY_KEYS + (ADMIN_TOKEN_KEY,)
SECRET_KEYS = (API_PRIVATE_KEY_KEY, ADMIN_TOKEN_KEY)
OPTIONAL_KEYS = (ENVIRONMENT_KEY, DB_PATH_KEY, DB_URL_KEY, RISK_PCT_KEY, HOST_KEY,
                 PORT_KEY, PROXY_ENV_KEY)

DEFAULTS: Dict[str, str] = {
    ENVIRONMENT_KEY: DEFAULT_ENVIRONMENT,
    DB_PATH_KEY: "./data/state.db",
    RISK_PCT_KEY: "0.01",
    HOST_KEY: "0.0.0.0",
    PORT_KEY: "8100",
}

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Lighter L2 API 私钥实测是 40 字节（0x + 80 hex = 82 字符，`lighter.create_api_key()`
# 返回的 pk 长度就是这个）；L1 钱包私钥是 32 字节（0x + 64 hex = 66 字符）。两种都收，
# 免得把「格式对但长度按另一套标准」的合法密钥挡在门外。
_HEX_KEY_LENS = frozenset({66, 82})
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")


class ConfigError(RuntimeError):
    """环境配置不完整或格式不对。消息里只有**变量名**，没有值。"""


def _mask(value: Optional[str]) -> str:
    return REDACTED if value else UNSET


def _mask_url(value: Optional[str]) -> str:
    """URL 里的密码脱敏（`postgres://user:pass@host` -> `postgres://user:***@host`）。"""
    if not value:
        return UNSET
    try:
        parts = urlsplit(value)
    except ValueError:
        return REDACTED
    if not parts.netloc or "@" not in parts.netloc:
        return value
    userinfo, _, host = parts.netloc.rpartition("@")
    user, sep, _password = userinfo.partition(":")
    netloc = f"{user}{sep}{REDACTED}@{host}" if sep else f"{REDACTED}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _read(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    if raw is None:
        return ""
    return str(raw).strip()


def _parse_int(env: Mapping[str, str], key: str, *, minimum: int = 0,
               default: Optional[int] = None) -> int:
    raw = _read(env, key)
    if not raw:
        if default is not None:
            return int(default)
        raise ConfigError(f"{key} is empty but is required to be a base-10 integer")
    try:
        value = int(raw, 10)
    except ValueError:
        raise ConfigError(f"{key} must be a base-10 integer") from None
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def _parse_float(env: Mapping[str, str], key: str, *, default: float,
                 low: float, high: float) -> float:
    raw = _read(env, key) or str(default)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not (low < value <= high):
        raise ConfigError(f"{key} must be in ({low}, {high}]")
    return value


@dataclass(frozen=True)
class Config:
    """运行时配置。构造走 `Config.from_env()`；字段顺序与 `.env.example` 一致。

    secret 字段用 `repr=False` 屏蔽掉 dataclass 自动生成的 repr，再由自定义 `__repr__`
    打码 —— 双重保险，避免有人把 `Config` 整个丢进日志时泄露私钥。
    """

    account_index: int
    api_key_index: int
    api_private_key: str = field(repr=False)
    l1_address: str
    environment: str
    admin_token: str = field(repr=False)
    db_path: str
    db_url: str
    risk_pct: float
    host: str
    port: int
    proxy_url: str = ""

    # ---------------------------------------------------------------- 派生

    @property
    def base_url(self) -> str:
        """`LIGHTER_ENVIRONMENT` -> API base URL（查 `app.market.BASE_URLS`）。"""
        return BASE_URLS[self.environment]

    @property
    def is_testnet(self) -> bool:
        return self.environment == "testnet"

    @property
    def proxy_enabled(self) -> bool:
        """`LIGHTER_PROXY_URL` 是否配了代理（空串 = 直连，默认）。"""
        return bool(self.proxy_url)

    @property
    def sqlite_path(self) -> Optional[str]:
        """若 `db_url` 是 sqlite 形式，返回文件路径；否则 None。"""
        prefix = "sqlite:///"
        return self.db_url[len(prefix):] if self.db_url.startswith(prefix) else None

    # ---------------------------------------------------------------- 脱敏

    def public(self) -> Dict[str, Any]:
        """可安全落日志/进面板的视图（secret 全部打码）。"""
        return {
            "account_index": self.account_index,
            "api_key_index": self.api_key_index,
            "api_private_key": _mask(self.api_private_key),
            "l1_address": self.l1_address,
            "environment": self.environment,
            "admin_token": _mask(self.admin_token),
            "db_path": self.db_path,
            "db_url": _mask_url(self.db_url),
            "risk_pct": self.risk_pct,
            "host": self.host,
            "port": self.port,
            # 代理 URL 可能带 user:pass@ —— 与 db_url 同一套脱敏规则。
            "proxy_url": redact_proxy_url(self.proxy_url) or UNSET,
        }

    def __repr__(self) -> str:
        pub = self.public()
        inner = ", ".join(f"{key}={value!r}" for key, value in pub.items())
        return f"Config({inner})"

    __str__ = __repr__

    # ------------------------------------------------------------- 加载

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        """从环境变量构造。缺项/格式错一律 `ConfigError`（消息里只有变量名）。"""
        source: Mapping[str, str] = env if env is not None else os.environ

        missing = [key for key in REQUIRED_KEYS if not _read(source, key)]
        if missing:
            raise ConfigError(
                "missing required environment variable(s): " + ", ".join(missing)
                + " — see .env.example (no defaults are assumed: a bot that trades on a "
                  "guessed account index is worse than a bot that refuses to start)"
            )

        environment = (_read(source, ENVIRONMENT_KEY) or DEFAULTS[ENVIRONMENT_KEY]).lower()
        if environment not in BASE_URLS:
            raise ConfigError(
                f"{ENVIRONMENT_KEY} must be one of {sorted(BASE_URLS)}, got an unknown value"
            )

        api_private_key = _read(source, API_PRIVATE_KEY_KEY)
        if not _HEX_RE.match(api_private_key) or len(api_private_key) not in _HEX_KEY_LENS:
            raise ConfigError(
                f"{API_PRIVATE_KEY_KEY} must be 0x-prefixed hex, "
                f"{sorted(_HEX_KEY_LENS)} chars (32- or 40-byte key)"
            )

        l1_address = _read(source, L1_ADDRESS_KEY)
        if not _ADDRESS_RE.match(l1_address):
            raise ConfigError(f"{L1_ADDRESS_KEY} must be a 0x-prefixed 40-hex-char address")

        db_path = _read(source, DB_PATH_KEY) or DEFAULTS[DB_PATH_KEY]
        db_url = _read(source, DB_URL_KEY) or f"sqlite:///{db_path}"

        # 代理：空 = 直连（默认）；非法 = 直接失败。静默忽略会让「以为走代理其实直连」
        # 变成运行时才暴露的 20558，所以这里 fail-closed。
        try:
            proxy_url = parse_proxy_url(_read(source, PROXY_ENV_KEY))
        except ProxyConfigError as exc:
            raise ConfigError(str(exc)) from None

        return cls(
            account_index=_parse_int(source, ACCOUNT_INDEX_KEY),
            api_key_index=_parse_int(source, API_KEY_INDEX_KEY),
            api_private_key=api_private_key,
            l1_address=l1_address,
            environment=environment,
            admin_token=_read(source, ADMIN_TOKEN_KEY),
            db_path=db_path,
            db_url=db_url,
            risk_pct=_parse_float(source, RISK_PCT_KEY,
                                  default=float(DEFAULTS[RISK_PCT_KEY]), low=0.0, high=1.0),
            host=_read(source, HOST_KEY) or DEFAULTS[HOST_KEY],
            port=_parse_int(source, PORT_KEY, minimum=1,
                            default=int(DEFAULTS[PORT_KEY])),
            proxy_url=proxy_url or "",
        )

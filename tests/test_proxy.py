"""`app/proxy.py` 的契约测试 —— 全部离线（除一组「真 SDK 只构造、不联网」的断言）。

钉住的六条性质：

1. **默认直连**：`LIGHTER_PROXY_URL` 未设置 / 空串 → 不走代理，且 `urllib` 那条路连
   `HTTP(S)_PROXY` 环境变量都不认。
2. **fail-closed**：非法代理 URL → `ProxyConfigError`（`Config` 那层包成 `ConfigError`），
   绝不静默降级成直连。
3. **只对 Lighter 走代理**：后缀 + 点边界匹配，`evil-zklighter.elliot.ai.com` 这类不命中；
   `https://zklighter.elliot.ai@evil.com/` 这种 userinfo 伎俩也不命中。
4. **凭据不外泄**：代理 URL 带 `user:pass@` 时，`repr` / `public()` / 异常消息里只有 `***`。
5. **urllib 确实用了代理**：用假 transport（假 opener）断言 ProxyHandler 的 proxies 与
   实际被调用的 opener，而不是「相信它应该会走代理」。
6. **SDK 走 `Configuration.proxy`**：真 SDK 构造后 `rest_client.proxy` 就是配置值
   （`rest.py:76` 抄自 `Configuration.proxy`，`rest.py:159-162` 塞进每次请求的 `proxy=`），
   并且过程中**没有**写任何 `*_PROXY` 环境变量（不污染同进程其它 aiohttp 用例）。

另外把「WebSocket 尚不支持」这件事也钉成文档契约：`docs/PROXY.md` 必须写明，且
`app/proxy.py` 里不许出现 `websockets` —— 不假装支持。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import unittest
from contextlib import contextmanager
from typing import Any
from unittest import mock

from app import exchange, market, proxy
from app.config import Config, ConfigError

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROXY = "http://127.0.0.1:8118"
PROXY_WITH_CREDS = "http://alice:s3cret@127.0.0.1:8118"
LIGHTER_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"

MAINNET_KEY = "0x" + "ab" * 40         # 82 chars（40 字节 L2 key）
L1_ADDRESS = "0x" + "cd" * 20


def env(**overrides: object) -> dict:
    """最小可用的环境变量集合（与 `tests/test_config.py` 同形）。"""
    base = {
        "LIGHTER_ACCOUNT_INDEX": "123456",
        "LIGHTER_API_KEY_INDEX": "4",
        "LIGHTER_API_PRIVATE_KEY": MAINNET_KEY,
        "LIGHTER_L1_ADDRESS": L1_ADDRESS,
        "ADMIN_TOKEN": "admin-token",
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = str(value)
    return base


# --------------------------------------------------------------------------- 1. 默认直连


class DefaultIsDirectTest(unittest.TestCase):
    def test_unset_is_none_not_a_default_proxy(self):
        self.assertIsNone(proxy.parse_proxy_url(None))
        self.assertIsNone(proxy.parse_proxy_url(""))
        self.assertIsNone(proxy.parse_proxy_url("   "))
        self.assertIsNone(proxy.proxy_url_from_env({}))

    def test_should_proxy_is_false_without_a_proxy_url(self):
        self.assertFalse(proxy.should_proxy(LIGHTER_URL, None))
        self.assertFalse(proxy.should_proxy(LIGHTER_URL, ""))
        self.assertFalse(proxy.should_proxy(LIGHTER_URL, "   "))

    def test_config_defaults_to_direct(self):
        cfg = Config.from_env(env())
        self.assertEqual(cfg.proxy_url, "")
        self.assertFalse(cfg.proxy_enabled)
        self.assertEqual(cfg.public()["proxy_url"], "<unset>")
        # 空串显式设置 = 与未设置等价（不是「空就是配了但没用」的模糊态）
        self.assertEqual(Config.from_env(env(LIGHTER_PROXY_URL="")).proxy_url, "")

    def test_market_client_defaults_to_plain_urllib(self):
        client = market.MarketClient("mainnet")
        self.assertIs(client._http_get, market._urllib_get_json)

    def test_config_reads_the_proxy_key(self):
        cfg = Config.from_env(env(LIGHTER_PROXY_URL=PROXY))
        self.assertEqual(cfg.proxy_url, PROXY)
        self.assertTrue(cfg.proxy_enabled)
        self.assertEqual(cfg.public()["proxy_url"], PROXY)


# --------------------------------------------------------------------------- 2. fail-closed


class InvalidProxyUrlTest(unittest.TestCase):
    """非法 URL 必须报错。静默忽略 = 「以为走代理其实直连」，那会撞 20558 而查不出原因。"""

    INVALID = (
        "127.0.0.1:8118",                    # 缺 scheme
        "localhost:8118",
        "127.0.0.1",                         # 连端口都没有
        "http://",                           # 没 host
        "http://127.0.0.1:99999",            # 端口越界
        "http://127.0.0.1:8118/path",        # 代理 URL 不该带路径
        "http://127.0.0.1:8118?x=1",         # 也不该带 query
        "socks5://127.0.0.1:1080",           # 不支持（会需要新依赖）
        "ftp://127.0.0.1:21",
        "http://user:pa ss@127.0.0.1:8118",  # 空白
    )

    def test_every_invalid_url_raises(self):
        for bad in self.INVALID:
            with self.subTest(value=bad):
                with self.assertRaises(proxy.ProxyConfigError) as ctx:
                    proxy.parse_proxy_url(bad)
                message = str(ctx.exception)
                self.assertIn(proxy.PROXY_ENV_KEY, message)
                # 消息里不回显「可能带凭据」的原文（下面有专门的凭据用例）
                self.assertNotIn("s3cret", message)

    def test_config_turns_it_into_a_config_error(self):
        for bad in self.INVALID:
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError) as ctx:
                    Config.from_env(env(LIGHTER_PROXY_URL=bad))
                self.assertIn("LIGHTER_PROXY_URL", str(ctx.exception))
                self.assertNotIn("s3cret", str(ctx.exception))

    def test_http_get_with_proxy_refuses_an_empty_url(self):
        with self.assertRaises(proxy.ProxyConfigError):
            proxy.http_get_with_proxy("")
        with self.assertRaises(proxy.ProxyConfigError):
            proxy.http_get_with_proxy("not-a-url")


# --------------------------------------------------------------------------- 3. 域名判定


class HostMatchingTest(unittest.TestCase):
    POSITIVE = (
        "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks",
        "https://testnet.zklighter.elliot.ai/api/v1/candles",
        "https://zklighter.elliot.ai/api/v1",
        "https://api.rh.lighter.xyz/api/v1",
        "https://api.rh-testnet.lighter.xyz/api/v1",
        "https://lighter.xyz",
        "https://app.lighter.com/trade",
        # hostname 大小写 / 尾部点都要归一
        "https://MAINNET.ZKLIGHTER.ELLIOT.AI./api/v1",
    )
    NEGATIVE = (
        "https://example.com",
        "https://evil-zklighter.elliot.ai.com",       # 后缀误伤（关键用例）
        "https://zklighter.elliot.ai.evil.com",
        "https://notzklighter.elliot.ai",             # 点边界不成立
        "https://zlighter.elliot.ai",                 # 少一个 k
        "https://elliot.ai",
        "https://lighter.xyz.evil.com",
        "https://evil.com/?next=lighter.xyz",
        "https://zklighter.elliot.ai@evil.com/x",     # userinfo 伎俩
        "",
        "not a url",
    )

    def test_positive_hosts_are_proxied(self):
        for url in self.POSITIVE:
            with self.subTest(url=url):
                self.assertTrue(proxy.should_proxy(url, PROXY))
                self.assertTrue(proxy.is_lighter_host(proxy.url_host(url)))

    def test_negative_hosts_stay_direct(self):
        for url in self.NEGATIVE:
            with self.subTest(url=url):
                self.assertFalse(proxy.should_proxy(url, PROXY))

    def test_url_host_strips_userinfo_port_and_case(self):
        self.assertEqual(proxy.url_host("https://User:pw@HOST.example:8443/x?y=1"), "host.example")
        self.assertEqual(proxy.url_host("https://mainnet.zklighter.elliot.ai./x"),
                         "mainnet.zklighter.elliot.ai")
        self.assertEqual(proxy.url_host(""), "")
        self.assertEqual(proxy.url_host("not a url"), "")

    def test_is_lighter_host_is_exact_or_dot_bounded(self):
        self.assertTrue(proxy.is_lighter_host("zklighter.elliot.ai"))
        self.assertTrue(proxy.is_lighter_host("a.b.zklighter.elliot.ai"))
        self.assertTrue(proxy.is_lighter_host("ZKLIGHTER.ELLIOT.AI"))
        self.assertFalse(proxy.is_lighter_host("zklighter.elliot.ai.evil.com"))
        self.assertFalse(proxy.is_lighter_host("notzklighter.elliot.ai"))
        self.assertFalse(proxy.is_lighter_host(""))

    def test_sdk_gets_proxy_only_for_lighter_hosts(self):
        self.assertEqual(proxy.configuration_proxy_for(LIGHTER_URL, PROXY), PROXY)
        self.assertIsNone(proxy.configuration_proxy_for("https://example.com", PROXY))
        self.assertIsNone(proxy.configuration_proxy_for(LIGHTER_URL, None))


# --------------------------------------------------------------------------- 4. 凭据脱敏


class RedactionTest(unittest.TestCase):
    def test_redact_replaces_the_password(self):
        self.assertEqual(proxy.redact_proxy_url(PROXY_WITH_CREDS),
                         "http://alice:***@127.0.0.1:8118")
        self.assertEqual(proxy.redact_proxy_url("http://127.0.0.1:8118"), PROXY)
        self.assertEqual(proxy.redact_proxy_url(""), "")
        self.assertEqual(proxy.redact_proxy_url(None), "")
        # 解析不了的东西（可能就是一个被误填的密码）整个吞掉
        self.assertEqual(proxy.redact_proxy_url("s3cret-password"), "***")

    def test_parsing_keeps_the_credentials_for_authentication(self):
        # 脱敏只发生在「打印」时；真正用的 URL 必须保留凭据，否则带认证的代理连不上。
        self.assertEqual(proxy.parse_proxy_url(PROXY_WITH_CREDS), PROXY_WITH_CREDS)

    def test_config_repr_and_public_hide_the_password(self):
        cfg = Config.from_env(env(LIGHTER_PROXY_URL=PROXY_WITH_CREDS))
        for text in (repr(cfg), str(cfg), str(cfg.public())):
            self.assertNotIn("s3cret", text)
            self.assertIn("***", text)
        self.assertEqual(cfg.public()["proxy_url"], "http://alice:***@127.0.0.1:8118")

    def test_invalid_url_error_does_not_leak_the_password(self):
        with self.assertRaises(proxy.ProxyConfigError) as ctx:
            proxy.parse_proxy_url("http://alice:s3cret@127.0.0.1:8118/some/path")
        message = str(ctx.exception)
        self.assertNotIn("s3cret", message)
        self.assertNotIn("alice", message)

    def test_config_error_does_not_leak_the_password(self):
        for bad in ("http://alice:s3cret@127.0.0.1:8118/path",
                    "http://alice:s3cret@127.0.0.1:99999",
                    "socks5://alice:s3cret@127.0.0.1:1080"):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError) as ctx:
                    Config.from_env(env(LIGHTER_PROXY_URL=bad))
                message = str(ctx.exception)
                self.assertNotIn("s3cret", message)
                self.assertNotIn("alice", message)

    def test_exchange_repr_hides_the_password(self):
        cfg = Config.from_env(env(LIGHTER_PROXY_URL=PROXY_WITH_CREDS))
        client = exchange.ExchangeClient(cfg)
        text = repr(client)
        self.assertNotIn("s3cret", text)
        self.assertIn("proxy_url='http://alice:***@127.0.0.1:8118'", text)
        self.assertIn("api_private_key='***'", text)

    def test_direct_exchange_repr_says_direct(self):
        client = exchange.ExchangeClient(Config.from_env(env()))
        self.assertIn("proxy_url='<direct>'", repr(client))
        self.assertEqual(client.proxy_url, "")


# --------------------------------------------------------------------------- 5. urllib 注入


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeOpener:
    """假 transport：记录 ProxyHandler 的 proxies 与每次 open() 的参数。"""

    def __init__(self, *handlers: object) -> None:
        self.handlers = handlers
        self.proxies = getattr(handlers[0], "proxies", None) if handlers else None
        self.calls: list = []

    def open(self, request: object, timeout: float = None) -> FakeResponse:   # type: ignore[assignment]
        self.calls.append((request.full_url, timeout,
                           {k.lower(): v for k, v in request.headers.items()}))
        return FakeResponse(b'{"code": 200, "order_book_details": []}')


class UrllibProxyInjectionTest(unittest.TestCase):
    def _getter(self, proxy_url: str = PROXY):
        built: list = []

        def factory(*handlers: object) -> FakeOpener:
            opener = FakeOpener(*handlers)
            built.append(opener)
            return opener

        getter = proxy.http_get_with_proxy(
            proxy_url, headers={"User-Agent": "lighter-ma-bot/market"},
            opener_factory=factory)
        return getter, built

    def test_openers_carry_the_expected_proxy_handlers(self):
        _getter, built = self._getter()
        self.assertEqual(len(built), 2)
        direct, proxied = built
        self.assertEqual(direct.proxies, {})                      # 直连：连环境变量代理都不认
        self.assertEqual(proxied.proxies, {"http": PROXY, "https": PROXY})

    def test_lighter_urls_use_the_proxied_opener(self):
        getter, built = self._getter()
        direct, proxied = built
        payload = getter(LIGHTER_URL, timeout=7.5)
        self.assertEqual(payload, {"code": 200, "order_book_details": []})
        self.assertEqual([call[0] for call in proxied.calls], [LIGHTER_URL])
        self.assertEqual(proxied.calls[0][1], 7.5)
        self.assertEqual(proxied.calls[0][2]["user-agent"], "lighter-ma-bot/market")
        self.assertEqual(direct.calls, [])

    def test_other_hosts_use_the_direct_opener(self):
        getter, built = self._getter()
        direct, proxied = built
        getter("https://example.com/probe")
        self.assertEqual([call[0] for call in direct.calls], ["https://example.com/probe"])
        self.assertEqual(direct.calls[0][1], 30.0)                # 默认超时
        self.assertEqual(proxied.calls, [])

    def test_getter_exposes_only_a_redacted_label(self):
        getter, _built = self._getter(PROXY_WITH_CREDS)
        self.assertTrue(getter.proxied)
        self.assertEqual(getter.proxy_label, "http://alice:***@127.0.0.1:8118")
        self.assertNotIn("s3cret", repr(getter.proxy_label))

    def test_market_client_wires_the_proxy_getter(self):
        client = market.MarketClient("mainnet", proxy_url=PROXY)
        self.assertEqual(getattr(client._http_get, "proxy_label", None), PROXY)
        self.assertTrue(getattr(client._http_get, "proxied", False))
        self.assertIsNot(client._http_get, market._urllib_get_json)

    def test_explicit_http_get_wins_over_the_proxy_setting(self):
        fake = lambda url, timeout=30.0: {}                       # noqa: E731
        client = market.MarketClient("mainnet", http_get=fake, proxy_url=PROXY)
        self.assertIs(client._http_get, fake)

    def test_invalid_proxy_is_not_silently_ignored_by_the_market_client(self):
        with self.assertRaises(proxy.ProxyConfigError):
            market.MarketClient("mainnet", proxy_url="127.0.0.1:8118")


# --------------------------------------------------------------------------- 6. SDK 注入


class ConfigurationProxyInjectionTest(unittest.TestCase):
    def test_the_scope_sets_configuration_proxy_then_restores_it(self):
        import lighter.signer_client as signer_client

        original = signer_client.Configuration
        with exchange._inject_configuration_proxy(PROXY):
            self.assertIsNot(signer_client.Configuration, original)
            configuration = signer_client.Configuration(
                host="https://mainnet.zklighter.elliot.ai")
            self.assertEqual(configuration.proxy, PROXY)
        self.assertIs(signer_client.Configuration, original)
        self.assertIsNone(signer_client.Configuration(
            host="https://mainnet.zklighter.elliot.ai").proxy)

    def test_empty_proxy_is_a_no_op_scope(self):
        import lighter.signer_client as signer_client

        original = signer_client.Configuration
        with exchange._inject_configuration_proxy(None) as value:
            self.assertIsNone(value)
            self.assertIs(signer_client.Configuration, original)

    def test_scope_restores_even_when_the_body_raises(self):
        import lighter.signer_client as signer_client

        original = signer_client.Configuration
        with self.assertRaises(RuntimeError):
            with exchange._inject_configuration_proxy(PROXY):
                raise RuntimeError("boom")
        self.assertIs(signer_client.Configuration, original)

    def test_sdk_rest_proxy_reads_the_effective_transport_proxy(self):
        class FakeRest:
            proxy = PROXY

        class FakeClient:
            rest_client = FakeRest()

        class FakeSigner:
            api_client = FakeClient()

        self.assertEqual(exchange._sdk_rest_proxy(FakeSigner()), PROXY)
        self.assertIsNone(exchange._sdk_rest_proxy(object()))


class RealSdkProxyTest(unittest.TestCase):
    """真 SDK、真 `Configuration`，但**不发任何网络请求**（构造函数不联网）。"""

    @classmethod
    def setUpClass(cls) -> None:
        import lighter

        private_key, _public_key, error = lighter.create_api_key()
        assert error is None, error
        cls.api_private_key = private_key

    def _build(self, proxy_url=None):
        return exchange.LighterSdk(
            base_url="https://mainnet.zklighter.elliot.ai/api/v1",
            account_index=12345, api_key_index=4,
            api_private_key=self.api_private_key, environment="mainnet",
            proxy_url=proxy_url)

    def test_rest_client_proxy_is_the_configured_proxy(self):
        async def run():
            sdk = self._build(PROXY)
            try:
                self.assertEqual(sdk.proxy_url, PROXY)
                self.assertEqual(sdk._signer.api_client.rest_client.proxy, PROXY)
            finally:
                await sdk.close()
        asyncio.run(run())

    def test_no_proxy_means_none(self):
        async def run():
            sdk = self._build()
            try:
                self.assertIsNone(sdk.proxy_url)
                self.assertIsNone(sdk._signer.api_client.rest_client.proxy)
            finally:
                await sdk.close()
        asyncio.run(run())

    def test_no_proxy_environment_variable_is_written(self):
        watched = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "all_proxy", "no_proxy")
        before = {name: os.environ.get(name) for name in watched}

        async def run():
            sdk = self._build(PROXY)
            try:
                self.assertEqual(sdk._signer.api_client.rest_client.proxy, PROXY)
            finally:
                await sdk.close()
        asyncio.run(run())

        self.assertEqual({name: os.environ.get(name) for name in watched}, before)

    def test_sdk_refuses_to_run_when_the_proxy_did_not_land(self):
        from app.exchange import NotConfigured

        @contextmanager
        def noop(_proxy_url):
            yield None

        class FakeSigner:
            """只回答 `chain_id` 与「transport 上的 proxy」—— 这条测试只验守门逻辑。

            用假签名器而不是真 SDK，是为了不留下一个没人关闭的 aiohttp session
            （构造成功之后才失败的那条路径本来就没有收尾）。真 SDK 的代理生效
            由本类其它测试覆盖。
            """

            chain_id = 304

            class api_client:
                class rest_client:
                    proxy = None

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        with mock.patch.object(exchange, "_inject_configuration_proxy", noop), \
                mock.patch("lighter.signer_client.SignerClient", FakeSigner):
            with self.assertRaises(NotConfigured) as ctx:
                exchange.LighterSdk(
                    base_url="https://mainnet.zklighter.elliot.ai/api/v1",
                    account_index=12345, api_key_index=4,
                    api_private_key=self.api_private_key, environment="mainnet",
                    proxy_url=PROXY_WITH_CREDS)
        message = str(ctx.exception)
        self.assertIn("did not apply the configured proxy", message)
        self.assertIn("http://alice:***@127.0.0.1:8118", message)
        self.assertNotIn("s3cret", message)


# --------------------------------------------------------------------------- 7. WebSocket 现状


class WebSocketIsNotSupportedTest(unittest.TestCase):
    """不假装支持：代码里没有 WS 代理实现，文档里必须写明「未支持」。"""

    def test_proxy_module_has_no_websockets_import(self):
        text = (ROOT / "app" / "proxy.py").read_text(encoding="utf-8")
        self.assertNotIn("import websockets", text)
        self.assertNotIn("from websockets", text)

    def test_proxy_module_does_not_claim_ws_support(self):
        self.assertFalse(hasattr(proxy, "ws_proxy"))
        self.assertFalse(hasattr(proxy, "connect_ws"))

    def test_docs_state_ws_is_unsupported(self):
        doc = (ROOT / "docs" / "PROXY.md").read_text(encoding="utf-8")
        self.assertIn("WebSocket", doc)
        self.assertIn("不支持", doc)
        # 需要时怎么做：显式 proxy= 参数
        self.assertIn("proxy=", doc)


if __name__ == "__main__":
    unittest.main()

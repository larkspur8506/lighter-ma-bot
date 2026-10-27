"""`app/config.py` 的契约测试 —— 全部离线。

三条被钉住的性质：

* **fail-closed**：必填项缺失 -> `ConfigError`，且消息**逐个列出**缺了哪些键；
* **secret 脱敏**：`repr` / `str` / `public()` / 异常消息里都不出现私钥与 admin token；
* **`LIGHTER_ENVIRONMENT` 决定 base URL**（mainnet / testnet），未知值直接报错。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.config import (
    ADMIN_TOKEN_KEY, API_PRIVATE_KEY_KEY, API_KEY_INDEX_KEY, ACCOUNT_INDEX_KEY,
    DB_PATH_KEY, DB_URL_KEY, ENVIRONMENT_KEY, L1_ADDRESS_KEY, PORT_KEY, REDACTED,
    REQUIRED_KEYS, RISK_PCT_KEY, Config, ConfigError,
)
from app.market import MAINNET_API, TESTNET_API

API_KEY = "0x" + "ab" * 40          # 82 chars: Lighter L2 key (40 bytes)
L1_ADDRESS = "0x" + "cd" * 20       # 42 chars: Ethereum address
ADMIN_TOKEN = "admin-token-4f8e1d2c9b7a"


def env(**overrides: object) -> dict:
    base = {
        ACCOUNT_INDEX_KEY: "123456",
        API_KEY_INDEX_KEY: "3",
        API_PRIVATE_KEY_KEY: API_KEY,
        L1_ADDRESS_KEY: L1_ADDRESS,
        ADMIN_TOKEN_KEY: ADMIN_TOKEN,
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = str(value)
    return base


class HappyPathTest(unittest.TestCase):
    def test_full_environment_parses(self):
        config = Config.from_env(env())
        self.assertEqual(config.account_index, 123456)
        self.assertEqual(config.api_key_index, 3)
        self.assertEqual(config.api_private_key, API_KEY)
        self.assertEqual(config.l1_address, L1_ADDRESS)
        self.assertEqual(config.admin_token, ADMIN_TOKEN)
        self.assertEqual(config.environment, "mainnet")
        self.assertEqual(config.db_path, "./data/state.db")
        self.assertEqual(config.db_url, "sqlite:///./data/state.db")
        self.assertAlmostEqual(config.risk_pct, 0.01)
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 8100)

    def test_optional_keys_are_honoured(self):
        config = Config.from_env(env(**{
            DB_PATH_KEY: "/srv/data/state.db",
            RISK_PCT_KEY: "0.02",
            PORT_KEY: "9999",
            ENVIRONMENT_KEY: "testnet",
        }))
        self.assertEqual(config.db_path, "/srv/data/state.db")
        self.assertEqual(config.db_url, "sqlite:////srv/data/state.db")
        self.assertAlmostEqual(config.risk_pct, 0.02)
        self.assertEqual(config.port, 9999)
        self.assertEqual(config.environment, "testnet")
        self.assertTrue(config.is_testnet)

    def test_explicit_db_url_wins_over_db_path(self):
        config = Config.from_env(env(**{DB_URL_KEY: "postgresql://bot@db:5432/state"}))
        self.assertEqual(config.db_url, "postgresql://bot@db:5432/state")
        self.assertIsNone(config.sqlite_path)

    def test_values_are_stripped(self):
        config = Config.from_env(env(**{ACCOUNT_INDEX_KEY: "  42  "}))
        self.assertEqual(config.account_index, 42)

    def test_config_is_immutable(self):
        config = Config.from_env(env())
        with self.assertRaises(Exception):
            config.account_index = 1        # type: ignore[misc]

    def test_reads_process_environment_when_no_mapping_is_given(self):
        with mock.patch.dict("os.environ", env(), clear=True):
            config = Config.from_env()
        self.assertEqual(config.account_index, 123456)


class FailClosedTest(unittest.TestCase):
    def test_all_missing_keys_are_listed(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env({})
        message = str(caught.exception)
        for key in REQUIRED_KEYS:
            self.assertIn(key, message)
        self.assertIn("missing required environment variable", message)

    def test_only_the_missing_key_is_named(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{ADMIN_TOKEN_KEY: None}))
        message = str(caught.exception)
        self.assertIn(ADMIN_TOKEN_KEY, message)
        self.assertNotIn(L1_ADDRESS_KEY, message)

    def test_whitespace_only_counts_as_missing(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{L1_ADDRESS_KEY: "   "}))
        self.assertIn(L1_ADDRESS_KEY, str(caught.exception))

    def test_no_defaults_are_silently_assumed(self):
        """缺 `LIGHTER_ACCOUNT_INDEX` 时绝不能默默用 0 —— 那会下到错的账户上。"""
        with self.assertRaises(ConfigError):
            Config.from_env(env(**{ACCOUNT_INDEX_KEY: None}))

    def test_bad_integer_is_named(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{ACCOUNT_INDEX_KEY: "12.5"}))
        self.assertIn(ACCOUNT_INDEX_KEY, str(caught.exception))

    def test_negative_index_is_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_env(env(**{API_KEY_INDEX_KEY: "-1"}))

    def test_bad_port_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{PORT_KEY: "0"}))
        self.assertIn(PORT_KEY, str(caught.exception))

    def test_bad_risk_pct_is_rejected(self):
        for value in ("0", "1.5", "-0.1", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    Config.from_env(env(**{RISK_PCT_KEY: value}))
                self.assertIn(RISK_PCT_KEY, str(caught.exception))

    def test_api_private_key_format_is_checked(self):
        for bad in ("0x1234", "ab" * 40, "0x" + "zz" * 40, "0x" + "ab" * 33):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError) as caught:
                    Config.from_env(env(**{API_PRIVATE_KEY_KEY: bad}))
                self.assertIn(API_PRIVATE_KEY_KEY, str(caught.exception))

    def test_l1_address_format_is_checked(self):
        for bad in ("0x1234", "cd" * 20, "0x" + "zz" * 20):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError) as caught:
                    Config.from_env(env(**{L1_ADDRESS_KEY: bad}))
                self.assertIn(L1_ADDRESS_KEY, str(caught.exception))

    def test_error_messages_never_echo_a_secret(self):
        """格式错的那一项是私钥时，错误消息里不能出现私钥片段。"""
        bad_key = "0x" + "9f" * 40          # 82 chars? no: 2+80=82 -> valid length
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{API_PRIVATE_KEY_KEY: bad_key + "00"}))
        self.assertNotIn("9f9f9f", str(caught.exception))

    def test_errors_for_other_keys_do_not_mention_the_secrets(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{RISK_PCT_KEY: "nope"}))
        message = str(caught.exception)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn(ADMIN_TOKEN, message)


class RedactionTest(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_env(env())

    def test_repr_hides_secrets(self):
        text = repr(self.config)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(ADMIN_TOKEN, text)
        self.assertIn(REDACTED, text)
        self.assertIn("account_index=123456", text)

    def test_str_hides_secrets(self):
        text = str(self.config)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(ADMIN_TOKEN, text)

    def test_fstring_formatting_hides_secrets(self):
        text = f"{self.config}"
        self.assertNotIn(API_KEY, text)

    def test_public_view_is_safe(self):
        public = self.config.public()
        self.assertEqual(public["api_private_key"], REDACTED)
        self.assertEqual(public["admin_token"], REDACTED)
        self.assertNotIn(API_KEY, repr(public))
        self.assertNotIn(ADMIN_TOKEN, repr(public))
        self.assertEqual(public["environment"], "mainnet")
        self.assertEqual(public["account_index"], 123456)

    def test_db_url_password_is_masked(self):
        config = Config.from_env(env(**{DB_URL_KEY: "postgresql://bot:hunter2@db:5432/state"}))
        text = repr(config)
        self.assertNotIn("hunter2", text)
        self.assertIn("bot", text)
        self.assertIn(REDACTED, text)
        # 真实值仍然可用（脱敏只作用于展示）
        self.assertIn("hunter2", config.db_url)

    def test_masked_secret_is_distinguishable_from_a_real_value(self):
        """打码后的展示值必须明显不是可用密钥（`***`，且长度与真值无关）。"""
        public = self.config.public()
        self.assertEqual(public["admin_token"], REDACTED)
        self.assertNotEqual(public["admin_token"], ADMIN_TOKEN)
        self.assertLess(len(public["admin_token"]), len(ADMIN_TOKEN))


class EnvironmentTest(unittest.TestCase):
    def test_mainnet_is_the_default(self):
        config = Config.from_env(env())
        self.assertEqual(config.environment, "mainnet")
        self.assertEqual(config.base_url, MAINNET_API)
        self.assertFalse(config.is_testnet)

    def test_testnet_switches_the_base_url(self):
        config = Config.from_env(env(**{ENVIRONMENT_KEY: "testnet"}))
        self.assertEqual(config.base_url, TESTNET_API)
        self.assertNotEqual(config.base_url, MAINNET_API)

    def test_environment_is_case_insensitive(self):
        self.assertEqual(Config.from_env(env(**{ENVIRONMENT_KEY: "TESTNET"})).base_url,
                         TESTNET_API)

    def test_unknown_environment_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env(**{ENVIRONMENT_KEY: "devnet"}))
        self.assertIn(ENVIRONMENT_KEY, str(caught.exception))

    def test_base_url_comes_from_the_market_module_table(self):
        """不复制常量：配置层的 URL 必须与 `app.market` 用的是同一份。"""
        from app.market import BASE_URLS

        for name, url in BASE_URLS.items():
            with self.subTest(environment=name):
                config = Config.from_env(env(**{ENVIRONMENT_KEY: name}))
                self.assertEqual(config.base_url, url)


if __name__ == "__main__":
    unittest.main()

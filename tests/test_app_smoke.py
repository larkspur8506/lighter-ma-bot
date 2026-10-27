"""Smoke tests for the Lighter SDK dependency.

Deliberate design rule: every assertion here runs unconditionally. No
try/except ImportError → skipTest wrapper, because a missing SDK must be a
red build, not a green build with zero coverage. The module-level import
below fails the whole test module if the wheel is not installed.
"""

import os
import platform
import unittest

import lighter  # noqa: F401  -- must fail loudly if the SDK is absent


class LighterSdkSmokeTest(unittest.TestCase):
    def test_module_imports(self):
        path = lighter.__file__
        self.assertIsNotNone(path)
        assert path is not None  # narrow for type checkers
        self.assertTrue(path.endswith("__init__.py"))
        self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_signer_client_exists(self):
        self.assertTrue(hasattr(lighter, "SignerClient"), "lighter.SignerClient missing")
        self.assertTrue(callable(lighter.SignerClient))

    def test_api_client_exists(self):
        self.assertTrue(hasattr(lighter, "ApiClient"), "lighter.ApiClient missing")
        self.assertTrue(callable(lighter.ApiClient))

    def test_configuration_exists(self):
        self.assertTrue(hasattr(lighter, "Configuration"), "lighter.Configuration missing")

    def test_network_constants(self):
        for name in ("MAINNET", "TESTNET"):
            self.assertTrue(hasattr(lighter, name), f"lighter.{name} missing")
            self.assertIsNotNone(getattr(lighter, name))

    def test_create_api_key_callable(self):
        self.assertTrue(hasattr(lighter, "create_api_key"), "lighter.create_api_key missing")
        self.assertTrue(callable(lighter.create_api_key))

    def test_read_api_classes_exposed(self):
        # Read-only surface the bot depends on from T3 onwards.
        for name in ("AccountApi", "OrderApi", "InfoApi", "CandlestickApi", "TransactionApi"):
            self.assertTrue(hasattr(lighter, name), f"lighter.{name} missing")

    def test_wasm_signer_binary_present(self):
        """The WASM signer binary is shipped inside the wheel; on linux/amd64
        (our only supported runtime) the .so must be there — otherwise the
        image would build fine and fail at the first order."""
        path = lighter.__file__
        assert path is not None
        signers = os.path.join(os.path.dirname(path), "signers")
        self.assertTrue(os.path.isdir(signers), f"signers dir missing: {signers}")
        entries = os.listdir(signers)
        self.assertTrue(entries, "signers dir is empty")
        if platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64"):
            so = os.path.join(signers, "lighter-signer-linux-amd64.so")
            self.assertTrue(os.path.isfile(so), f"missing {so}; found {sorted(entries)}")
            self.assertGreater(os.path.getsize(so), 1_000_000)


class AppPackageSmokeTest(unittest.TestCase):
    def test_app_package_importable(self):
        import app

        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()

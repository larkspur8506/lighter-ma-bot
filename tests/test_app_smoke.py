"""Smoke tests for the Lighter SDK dependency and the HTTP app skeleton.

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

    def test_wasm_signer_binary_loads(self):
        """The signer must be *loadable*, not merely present on disk.

        A file-exists check is the exact failure mode it pretends to catch: a
        musl/glibc mismatch, a wrong-arch binary or a truncated file all pass
        `isfile()` and then explode on the first order. So the primary
        assertion is a real `create_api_key()` call, which dlopen()s the
        signer and generates a keypair locally (no network). The on-disk
        checks below are kept only as diagnostics for the amd64 image.
        """
        path = lighter.__file__
        assert path is not None
        signers = os.path.join(os.path.dirname(path), "signers")
        self.assertTrue(os.path.isdir(signers), f"signers dir missing: {signers}")
        self.assertTrue(os.listdir(signers), "signers dir is empty")

        if platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64"):
            so = os.path.join(signers, "lighter-signer-linux-amd64.so")
            self.assertTrue(os.path.isfile(so), f"missing {so}; found {sorted(os.listdir(signers))}")
            self.assertGreater(os.path.getsize(so), 1_000_000)

        # Primary assertion — platform-independent, must run everywhere.
        private_key, public_key, err = lighter.create_api_key()
        self.assertIsNone(err, f"create_api_key() failed: {err!r}")
        self.assertEqual(len(private_key), 82, "unexpected private key length")
        self.assertEqual(len(public_key), 82, "unexpected public key length")
        self.assertTrue(private_key.startswith("0x"), "private key is not hex-encoded")
        self.assertTrue(public_key.startswith("0x"), "public key is not hex-encoded")


class AppPackageSmokeTest(unittest.TestCase):
    def test_app_package_importable(self):
        import app

        self.assertIsNotNone(app)

    def test_app_main_importable(self):
        # The image's default CMD is `uvicorn app.main:app`; importing the
        # module here is what keeps a broken CMD from being discovered only
        # after deploy.
        from app.main import app as fastapi_app

        self.assertIsNotNone(fastapi_app)


class HealthRouteTest(unittest.TestCase):
    """Route-level guard for the one endpoint T1 ships."""

    def test_health_returns_ok(self):
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app

        response = TestClient(fastapi_app).get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["service"], "lighter-ma-bot")
        self.assertIn("version", body)


if __name__ == "__main__":
    unittest.main()

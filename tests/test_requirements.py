"""Dependency contract tests: the pins in requirements.txt must match what is
actually installed in the environment running the suite."""

import pathlib
import unittest

REQ = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"


def _pins():
    out = {}
    for raw in REQ.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("==")
        if rest:
            out[name.strip().lower()] = rest.strip()
    return out


class RequirementsTest(unittest.TestCase):
    def test_requirements_file_exists(self):
        self.assertTrue(REQ.is_file(), f"{REQ} missing")

    def test_lighter_sdk_pinned(self):
        self.assertEqual(_pins().get("lighter-sdk"), "1.1.8")

    def test_installed_lighter_matches_pin(self):
        from importlib.metadata import version

        self.assertEqual(version("lighter-sdk"), _pins()["lighter-sdk"])

    def test_core_runtime_deps_importable(self):
        for mod in ("fastapi", "uvicorn", "apscheduler", "httpx", "multipart"):
            with self.subTest(module=mod):
                __import__(mod)

    def test_fastapi_testclient_available(self):
        # httpx is what makes route-level smoke tests possible in T10.
        from fastapi.testclient import TestClient

        self.assertTrue(callable(TestClient))


if __name__ == "__main__":
    unittest.main()

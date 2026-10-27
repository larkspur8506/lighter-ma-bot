"""Dependency contract tests.

Two rules are enforced here:

1. Every line in ``requirements.txt`` is parsed as a real requirement (PEP 508)
   and the *installed* version must satisfy it. A constraint that is silently
   dropped (`_pins()` used to keep only `==` lines) turns this test into
   theatre, so range constraints are first-class.
2. The web stack is pinned exactly, because a floating resolution is how a
   `starlette`/`httpx2` surprise reached CI once already.
"""

import pathlib
import unittest
from importlib.metadata import PackageNotFoundError, version

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REQ = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

# Every dependency the runtime actually needs. Compared as a set so that a
# deleted line is a failure, not a silently narrower test.
EXPECTED = {
    "lighter-sdk",
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "httpx2",
    "python-multipart",
    "apscheduler",
    "packaging",
}

# Modules that must be importable in the environment under test (import name,
# not distribution name — `python-multipart` imports as `python_multipart`).
IMPORT_NAMES = {
    "lighter": "lighter-sdk",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "httpx2": "httpx2",
    "python_multipart": "python-multipart",
    "apscheduler": "apscheduler",
    "packaging": "packaging",
}


def parse_requirements(text):
    """Parse requirement lines into {canonical name: SpecifierSet}.

    Handles comments, blank lines, ``==x``, ``>=x,<y`` and bare names; a line
    that is not a valid requirement raises instead of being skipped.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        out[canonicalize_name(requirement.name)] = requirement.specifier
    return out


def _pins():
    return parse_requirements(REQ.read_text(encoding="utf-8"))


class RequirementsParseTest(unittest.TestCase):
    def test_requirements_file_exists(self):
        self.assertTrue(REQ.is_file(), f"{REQ} missing")

    def test_parse_keeps_range_constraints(self):
        # Regression guard: the old parser kept only `==` lines, so 5 of 6
        # dependencies were never checked.
        parsed = parse_requirements(
            "a==1.2.3\nb>=2.0,<3.0\nc>=1.0  # trailing comment\n\nd\n"
        )
        self.assertEqual(set(parsed), {"a", "b", "c", "d"})
        self.assertEqual(str(parsed["b"]), "<3.0,>=2.0")
        self.assertEqual(str(parsed["c"]), ">=1.0")
        self.assertEqual(str(parsed["a"]), "==1.2.3")
        # A bare name parses to an empty specifier, not to a dropped line.
        self.assertEqual(str(parsed["d"]), "")

    def test_all_expected_deps_declared(self):
        declared = set(_pins())
        self.assertEqual(
            declared, EXPECTED, f"missing: {sorted(EXPECTED - declared)}, extra: {sorted(declared - EXPECTED)}"
        )


class InstalledMatchesRequirementsTest(unittest.TestCase):
    def test_every_declared_dep_installed_and_satisfies_constraint(self):
        for name, specifier in _pins().items():
            with self.subTest(dependency=name):
                try:
                    installed = version(name)
                except PackageNotFoundError:
                    self.fail(f"{name} is declared in requirements.txt but not installed")
                self.assertTrue(
                    specifier.contains(installed, prereleases=True),
                    f"{name} {installed} does not satisfy {specifier!r}",
                )

    def test_lighter_sdk_pinned_exactly(self):
        specifier = _pins()["lighter-sdk"]
        self.assertEqual(str(specifier), "==1.1.8")
        self.assertEqual(version("lighter-sdk"), "1.1.8")

    def test_web_stack_pinned_exactly(self):
        # Exact pins cannot be resolved to a different combination on a later
        # CI run; anything less makes the build non-reproducible.
        pins = _pins()
        for name in ("fastapi", "starlette", "uvicorn"):
            with self.subTest(dependency=name):
                self.assertEqual(str(pins[name]), f"=={version(name)}")

    def test_httpx_capped_below_029(self):
        # starlette 1.10 declares httpx<0.29 in its own [full] extra; going
        # past it is untested against the TestClient fallback path.
        self.assertEqual(str(_pins()["httpx"]), "<0.29,>=0.27")
        self.assertFalse(_pins()["httpx"].contains("0.29.0"))

    def test_httpx2_declared(self):
        # starlette 1.10's TestClient imports httpx2 first and only warns +
        # falls back when it is absent. Declaring it keeps that path explicit.
        self.assertIn("httpx2", _pins())


class ImportableTest(unittest.TestCase):
    def test_core_runtime_deps_importable(self):
        for module, distribution in IMPORT_NAMES.items():
            with self.subTest(module=module, distribution=distribution):
                __import__(module)

    def test_fastapi_testclient_available(self):
        # TestClient is what makes route-level smoke tests possible (T10).
        from fastapi.testclient import TestClient

        self.assertTrue(callable(TestClient))

    def test_testclient_uses_httpx2_without_deprecation_warning(self):
        # Guards the exact CI regression: starlette warns when it has to fall
        # back to `httpx`. With httpx2 installed the import must be clean.
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from fastapi.testclient import TestClient

            self.assertTrue(callable(TestClient))
        starlette_warnings = [str(w.message) for w in caught if "starlette" in str(w.message).lower()]
        self.assertEqual(starlette_warnings, [], f"starlette deprecation warnings: {starlette_warnings}")


if __name__ == "__main__":
    unittest.main()

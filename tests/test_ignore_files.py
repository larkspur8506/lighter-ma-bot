"""Build-context and repository ignore rules, asserted instead of trusted.

From T7 this repository holds an L1 private key and a Lighter API private key,
so "the file happens to be listed" is not a property worth eyeballing: both
ignore files are checked here on every run. `.dockerignore` matters most,
because `COPY . .` in the Dockerfile will happily bake a key into a published
image layer.

Matching follows Docker's documented rules: `**/` matches any number of
directories (including zero), a pattern matching a parent directory excludes
the whole subtree, and a trailing `/` is cleaned away.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERIGNORE = ROOT / ".dockerignore"
GITIGNORE = ROOT / ".gitignore"

# Paths that must never reach a build context / a commit.
SECRETS = [
    "lighter.key",
    "conf/lighter.key",
    "id_ed25519",
    "conf/id_rsa",
    "server.pem",
    "secrets/x.json",
    "conf/secrets/x.json",
    ".env",
    ".env.prod",
    ".envrc",
    "conf/.envrc",
    "state.db",
    "data/state.db",
    "logs/bot.log",
    "bot.log",
    "data/x.sqlite",
    "app/__pycache__/x.pyc",
]

# Paths the image is worthless without.
REQUIRED_IN_IMAGE = ["app/main.py", "app/__init__.py", "requirements.txt", "README.md"]


def _to_regex(pattern):
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def _patterns(path):
    patterns = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.rstrip("/") or "/")
    return patterns


def dockerignore_excludes(rel_path, patterns):
    parts = rel_path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in patterns:
        regex = _to_regex(pattern)
        for candidate in candidates:
            if re.fullmatch(regex, candidate):
                return pattern
    return None


class DockerignoreTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DOCKERIGNORE.is_file(), f"{DOCKERIGNORE} missing")
        self.patterns = _patterns(DOCKERIGNORE)

    def test_secrets_are_excluded_from_the_build_context(self):
        for path in SECRETS:
            with self.subTest(path=path):
                culprit = dockerignore_excludes(path, self.patterns)
                self.assertIsNotNone(culprit, f"{path} would be copied into the image")

    def test_app_sources_are_not_excluded(self):
        for path in REQUIRED_IN_IMAGE:
            with self.subTest(path=path):
                culprit = dockerignore_excludes(path, self.patterns)
                self.assertIsNone(culprit, f"{path} excluded by {culprit!r} — image cannot start")

    def test_dockerfile_and_dockerignore_are_not_copied_in(self):
        for path in ("Dockerfile", ".dockerignore"):
            with self.subTest(path=path):
                self.assertIsNotNone(dockerignore_excludes(path, self.patterns))


class GitignoreTest(unittest.TestCase):
    def test_private_key_patterns_are_declared(self):
        text = GITIGNORE.read_text(encoding="utf-8")
        for pattern in ("*.key", "*.pem", "*.p12", "id_rsa*", "id_ed25519*", "secrets/", ".envrc"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, text.splitlines())

    def test_env_example_is_whitelisted(self):
        # `.gitignore` promises `!.env.example`; the file must actually exist,
        # otherwise the promise is dead weight.
        self.assertTrue((ROOT / ".env.example").is_file(), ".env.example missing")
        self.assertIn("!.env.example", GITIGNORE.read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()

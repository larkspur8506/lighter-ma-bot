# syntax=docker/dockerfile:1
# The lighter-sdk wheel bundles linux-amd64 WASM signer binaries, so this
# image is amd64-only (no arm64 build). The platform comes from the caller
# (`docker build --platform linux/amd64`), not from a constant flag on FROM:
# buildx rejects a const flag (FromPlatformFlagConstDisallowed) because it
# silently overrides whatever the operator asked for.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first so the layer is cached independently of source edits.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt

# Source tree. `app/` may be absent or partially filled in on early tasks,
# so copy the whole context instead of a hard `COPY app/ ./app/` which
# would make the build fail on a missing path. Secrets, tests, docs and
# runtime state are excluded by .dockerignore.
COPY . .

# Fail the build here, not at the first order, if the WASM signer cannot be
# loaded: a musl/glibc mismatch, a wrong-arch binary or a truncated file all
# pass an `isfile()` check and then explode when signing. create_api_key()
# performs the real dlopen and generates a throwaway keypair (no network).
RUN python -c "import lighter; pk, pub, err = lighter.create_api_key(); assert err is None, err; assert len(pk) == 82 and len(pub) == 82, (len(pk), len(pub)); print('signer loadable')"

# From T7 this process holds the L1 private key and the Lighter API private
# key, so it must not run as root. Runtime state belongs in /srv/data and
# /srv/logs, which are the only paths the bot user owns (the code stays
# root-owned so the running process cannot rewrite itself).
RUN useradd --create-home --user-group --uid 10001 --shell /usr/sbin/nologin bot \
 && mkdir -p /srv/data /srv/logs \
 && chown bot:bot /srv/data /srv/logs

USER bot

EXPOSE 8100

# /health is served by app.main (T1). T10 replaces the module behind it with
# the monitoring panel; the path is the stable contract either way.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4).status == 200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]

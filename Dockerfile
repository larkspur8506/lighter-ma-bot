# syntax=docker/dockerfile:1
# The lighter-sdk wheel bundles linux-amd64 WASM signer binaries, so this
# image is amd64-only (no arm64 build).
FROM --platform=linux/amd64 python:3.14-slim

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
# would make the build fail on a missing path.
COPY . .

# Fail the build early (not at first order) if the signer binary is missing.
RUN python -c "import lighter, os; d=os.path.join(os.path.dirname(lighter.__file__), 'signers'); assert os.path.isdir(d), 'signers dir missing'; print('signers:', sorted(os.listdir(d)))"

EXPOSE 8100

# /health is served by app.main once T10 lands. Until then the container
# starts but stays unhealthy — that is expected for a skeleton.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4).status == 200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]

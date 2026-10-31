# agentring daemon (CLI + `agentring serve`, with the voice extra), for the optional
# "daemon" profile in deploy/docker-compose.yml. Dependencies come only from uv.lock.
# The same image runs the remote MCP server: command `mcp-http --host 0.0.0.0` (port
# 8790, state in /config/mcp-http; docs/agents.md#remote-mcp-http).
#
# Base images pinned by tag and digest (python 3.12.14 build of 2026-09-25, uv 0.13.0 of
# 2026-10-09): a re-tagged image can't slip in.
# Bump deliberately: docker buildx imagetools inspect <image>:<tag>
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cached layer), exactly as locked. Then the project as a wheel,
# built with a pinned backend (build-constraints.txt) and installed without deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra voice --no-install-project
COPY build-constraints.txt README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /tmp/dist --build-constraints build-constraints.txt \
 && uv pip install --python /app/.venv/bin/python --no-deps /tmp/dist/*.whl

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
# pip is not needed at runtime: everything is in the venv.
RUN pip uninstall --yes --root-user-action=ignore pip \
 && useradd --uid 1000 --user-group --no-create-home --home-dir /config --shell /usr/sbin/nologin agentring \
 && mkdir -p /config && chown agentring:agentring /config && chmod 0700 /config
# Code owned by root and not writable by the process; only /config belongs to it.
COPY --from=build --chown=root:root /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH AGENTRING_HOME=/config PYTHONUNBUFFERED=1
USER agentring
WORKDIR /config
VOLUME /config
ENTRYPOINT ["agentring"]
CMD ["serve"]

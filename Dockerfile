# syntax=docker/dockerfile:1

# ---- ビルド段: 依存を .venv に入れる ----
FROM python:3.14-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# 依存だけを先に入れ、ソースの変更でこの層が無効にならないようにする。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ---- Node.js: Claude Code の要求する版（>=22）を公式イメージから借りる ----
FROM node:22-bookworm-slim AS node

# ---- 実行段 ----
FROM python:3.14-slim-bookworm

# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.311

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# Claude Code は ~/.claude に書くので、書き込める HOME を持つ非 root ユーザーで動かす。
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir /state \
    && chown app:app /state

COPY --from=build /app/.venv /app/.venv

ENV HOME=/home/app \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/state

USER app
WORKDIR /home/app
VOLUME /state

# トークンは焼き込まない。PAPERLESS_TOKEN と CLAUDE_CODE_OAUTH_TOKEN は実行時に渡す。
ENTRYPOINT ["python", "-m", "paperless_scan_sorter"]

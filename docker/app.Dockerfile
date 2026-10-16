# Resident 应用镜像（console + kernel）。沙箱镜像见同目录 Dockerfile。
# 构建阶段需要联网（拉取 node / docker-cli 基底 + PyPI 依赖）。
# 中国大陆网络：可通过 build args 换镜像源（install 脚本 --cn 会自动设置）：
#   NODE_IMAGE / DOCKERCLI_IMAGE / PYTHON_IMAGE / NPM_REGISTRY / PIP_INDEX_URL / APT_MIRROR

ARG NODE_IMAGE=node:20-slim
ARG DOCKERCLI_IMAGE=docker:27-cli
ARG PYTHON_IMAGE=python:3.12-slim
ARG NPM_REGISTRY=
ARG PIP_INDEX_URL=

# ── 阶段 1：前端构建 ──────────────────────────────────────────
FROM ${NODE_IMAGE} AS frontend
ARG NPM_REGISTRY
WORKDIR /build
COPY frontend/ ./frontend/
# 镜像源优先，失败自动回退官方（NPM_REGISTRY 为空则直接用官方）
RUN cd frontend \
 && { if [ -n "$NPM_REGISTRY" ]; then \
        npm ci --registry "$NPM_REGISTRY" || npm ci; \
      else \
        npm ci; \
      fi; } \
 && npm run build

# ── 阶段 2：docker CLI（kernel 需要 docker exec 驱动沙箱容器）──
FROM ${DOCKERCLI_IMAGE} AS dockercli

# ── 阶段 3：运行镜像 ─────────────────────────────────────────
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL
ARG APT_MIRROR
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
          /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY . /app
# pip 依赖：镜像源优先，失败自动回退官方（PIP_INDEX_URL 为空则直接用官方）
RUN python3 -m venv /app/.venv \
 && { if [ -n "$PIP_INDEX_URL" ]; then \
        /app/.venv/bin/pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /app/requirements.txt \
        || /app/.venv/bin/pip install --no-cache-dir -r /app/requirements.txt; \
      else \
        /app/.venv/bin/pip install --no-cache-dir -r /app/requirements.txt; \
      fi; }
COPY --from=frontend /build/src/kernel/console/frontend/dist \
     /app/src/kernel/console/frontend/dist
EXPOSE 18082
CMD ["/app/.venv/bin/python", "-m", "kernel.console", \
     "--config", "config/console.yaml", "--host", "0.0.0.0", "--port", "18082"]

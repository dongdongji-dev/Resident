# Resident sandbox —— 沙箱镜像（agent 的命令与文件操作都在此容器内执行）。
# BASE_IMAGE 可用 --build-arg 覆盖（默认 python:3.12-slim）。
# 构建时清空一切代理变量并移除 apt 代理配置，保证容器内网络直连可用。
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
# 大陆网络加速：apt 源主机名（安装脚本探测到大陆时传入，如
# mirrors.aliyun.com；留空用官方源）
ARG APT_MIRROR=

# 清空一切代理环境变量（含小写/大写/all_proxy）
ENV http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= \
    all_proxy= ALL_PROXY= no_proxy= NO_PROXY=
# 顺手清掉 apt 代理配置（防止基底镜像里带了代理写进 /etc/apt/apt.conf.d/*proxy*）
RUN rm -f /etc/apt/apt.conf.d/*proxy* 2>/dev/null || true

# 常用工具（ps / curl）：技能与脚本里常用，避免容器内缺命令
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
          /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends procps curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/sandbox

# 长驻容器：等待 docker exec 进入
CMD ["sleep", "infinity"]


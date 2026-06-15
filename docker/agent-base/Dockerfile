# =============================================================================
# agent-base:latest — 多智能体协作框架子智能体基础镜像
# =============================================================================
# 设计原则：
#   - Ubuntu 单一镜像，所有角色通用
#   - 预安装 Claude Code CLI + 基础工具（git/build/node）
#   - 其余工具链由 Claude Code 运行时自行安装
#   - 容器内 root，可 apt-get 安装任意中间件
# =============================================================================

FROM ubuntu:24.04

LABEL maintainer="Multi-Agent Framework"
LABEL description="Agent base image: Ubuntu + Claude Code CLI"

# ── 环境变量 ──────────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV NODE_VERSION=20.17.0
ENV PATH="/usr/local/lib/nodejs/bin:$PATH"
ENV HOME=/root

# ── 基础工具 ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 构建工具
    build-essential \
    ca-certificates \
    curl \
    wget \
    git \
    xz-utils \
    # 网络/调试
    net-tools \
    iputils-ping \
    dnsutils \
    # 常用工具
    vim \
    nano \
    less \
    jq \
    tree \
    unzip \
    # Python 环境（Claude Code 依赖 python3）
    python3 \
    python3-pip \
    python3-venv \
    # Docker 客户端（容器内可执行 docker 命令访问宿主机 daemon）
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 直接安装（不依赖 nvm，避免网络不稳定导致构建失败）─────────────────
RUN NODE_TARBALL="node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}" \
    && mkdir -p /usr/local/lib/nodejs \
    && cd /tmp \
    && curl -fsSLO "${NODE_URL}" \
    && tar -xJf "${NODE_TARBALL}" -C /usr/local/lib/nodejs \
    && mv /usr/local/lib/nodejs/node-v${NODE_VERSION}-linux-x64/* /usr/local/lib/nodejs/ \
    && rmdir /usr/local/lib/nodejs/node-v${NODE_VERSION}-linux-x64 \
    && rm -f "${NODE_TARBALL}" \
    && node --version \
    && npm --version \
    && corepack enable

# ── Claude Code CLI ────────────────────────────────────────────────────────
# 官方安装脚本：https://docs.anthropic.com/claude-code/install
RUN npm install -g @anthropic-ai/claude-code

# ── SSH 密钥目录（可选挂载）────────────────────────────────────────────────
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

# ── 工作目录 ──────────────────────────────────────────────────────────────
RUN mkdir -p /workspace/shared /workspace/output
WORKDIR /workspace

# ── 权限与目录结构 ─────────────────────────────────────────────────────────
RUN mkdir -p /workspace/.agenticx/tasks /workspace/.agenticx/results

# ── entrypoint ────────────────────────────────────────────────────────────
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# 容器保持运行，等待任务下发
CMD ["sleep", "infinity"]

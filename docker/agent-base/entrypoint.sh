#!/bin/bash
# ───────────────────────────────────────────────────────────────────────────
# agent-base 容器 entrypoint
# ───────────────────────────────────────────────────────────────────────────
# 职责：
#   1. 确保 /workspace 目录结构存在
#   2. 如果挂载了 CLAUDE.md 和 settings.json，将其放到 Claude Code 可识别位置
#   3. 保持容器运行等待任务
# ───────────────────────────────────────────────────────────────────────────
set -e

echo "[entrypoint] Agent base container starting..."
echo "[entrypoint] AGENTICX_GROUP_ID=$AGENTICX_GROUP_ID"
echo "[entrypoint] AGENTICX_INSTANCE_ID=$AGENTICX_INSTANCE_ID"
echo "[entrypoint] AGENTICX_DEFINITION_ID=$AGENTICX_DEFINITION_ID"

# 确保工作目录结构
mkdir -p \
    /workspace/source \
    /workspace/shared \
    /workspace/output \
    /workspace/.agenticx/tasks \
    /workspace/.agenticx/results

# 如果卷内有 CLAUDE.md，建立符号链接到 Claude Code 默认读取位置
# Claude Code 默认会在当前目录或父目录查找 CLAUDE.md
if [[ -f /workspace/.agenticx/CLAUDE.md && ! -f /workspace/CLAUDE.md ]]; then
    ln -s /workspace/.agenticx/CLAUDE.md /workspace/CLAUDE.md
    echo "[entrypoint] CLAUDE.md linked"
fi

# 如果使用 Claude Code 的 settings.json 来限制工具权限，可以将其复制到
# Claude Code 配置目录（如有需要后续扩展）
if [[ -f /workspace/.agenticx/settings.json ]]; then
    CLAUDE_SETTINGS_DIR="/root/.claude"
    mkdir -p "$CLAUDE_SETTINGS_DIR"
    # 复制为项目级配置（注意：Claude Code CLI 的配置机制视版本可能不同）
    cp /workspace/.agenticx/settings.json "$CLAUDE_SETTINGS_DIR/settings.json"
    echo "[entrypoint] settings.json copied to $CLAUDE_SETTINGS_DIR"
fi

echo "[entrypoint] Ready. Waiting for tasks..."

# 执行传入的命令（CMD 或 docker run 的命令）
exec "$@"

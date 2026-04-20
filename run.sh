#!/bin/bash
# 便捷运行脚本 — 自动设置 PYTHONPATH 解决中文路径下 .pth 文件不被加载的问题
set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}"
exec "$PROJECT_DIR/.venv/bin/python" -m data_agent_baseline.cli "$@"

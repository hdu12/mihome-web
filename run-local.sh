#!/usr/bin/env bash
# 本地 / 测试环境启动脚本（Linux / macOS / Git Bash）
# Windows 下可直接双击 start.bat，两者最终都调用 run_local.py，
# 保证 .env 解析与数据目录规则只有一份实现。
set -e
cd "$(dirname "$0")"

# Windows 的 venv 在 Scripts/，Linux/macOS 在 bin/
if [ -x ./.venv/Scripts/python.exe ]; then
  PY=./.venv/Scripts/python.exe
elif [ -x ./.venv/bin/python ]; then
  PY=./.venv/bin/python
else
  PY=python
fi

exec "$PY" run_local.py "$@"

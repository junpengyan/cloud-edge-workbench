#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先运行 ./install-macos.sh' >&2
  exit 1
fi
if [ ! -f data/portable-key.bin ]; then
  echo '迁移密钥不存在：data/portable-key.bin' >&2
  exit 1
fi
if [ -f data/server.pid ] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  echo "工作台已运行，PID $(cat data/server.pid)"
  exit 0
fi
export UNIFIED_PORT="${UNIFIED_PORT:-18768}"
nohup .venv/bin/python server.py > data/server.log 2> data/server.err.log < /dev/null &
echo $! > data/server.pid
sleep 2
if ! kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  echo '启动失败，请查看 data/server.err.log' >&2
  exit 1
fi
echo "工作台已启动：http://127.0.0.1:$UNIFIED_PORT/"
open "http://127.0.0.1:$UNIFIED_PORT/"

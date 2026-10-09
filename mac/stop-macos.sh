#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f data/server.pid ]; then
  echo '没有运行中的工作台记录'
  exit 0
fi
pid="$(cat data/server.pid)"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "已请求停止工作台，PID $pid"
fi
rm -f data/server.pid

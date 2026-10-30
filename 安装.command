#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  runtime_python="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  if [ -x "$runtime_python" ]; then
    "$runtime_python" -m venv .venv
  else
    python3 -m venv .venv
  fi
fi
.venv/bin/python -m pip install -r requirements.txt
echo '安装完成，可双击 启动.command。'
read -r 'answer?按回车退出'

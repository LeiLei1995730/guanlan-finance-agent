#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '首次使用请先双击 安装.command，完成后再启动。'
  read -r 'answer?按回车退出'
  exit 1
fi
if .venv/bin/python -c 'import urllib.request,json; r=json.load(urllib.request.urlopen("http://127.0.0.1:18765/api/health",timeout=2)); assert r.get("version")=="1.0.0" and r.get("app")=="guanlan"' 2>/dev/null; then
  open 'http://127.0.0.1:18765'
  exit 0
fi
export COLLECT_INTERVAL_MINUTES="${COLLECT_INTERVAL_MINUTES:-360}"
echo '观澜运行于 http://127.0.0.1:18765 。保持此终端运行，Control-C 停止。'
(
.venv/bin/python - <<'PY'
import json,time,urllib.request,webbrowser
for attempt in range(30):
    try:
        r=json.load(urllib.request.urlopen('http://127.0.0.1:18765/api/health',timeout=1))
        if r.get('app')=='guanlan':
            webbrowser.open('http://127.0.0.1:18765')
            break
    except Exception:
        time.sleep(.5)
PY
) &
exec .venv/bin/python -m finance_agent serve

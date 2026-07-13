#!/bin/bash
cd "$(dirname "$0")" || exit 1

pause_on_error() {
  echo
  read -r -p "按回车键关闭此窗口……" _
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未找到 Python 3。请先从 https://www.python.org/downloads/ 安装 Python 3。"
  pause_on_error
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "首次启动：正在创建 MailPilot 专用 Python 环境……"
  if ! python3 -m venv .venv; then
    echo "❌ 无法创建 .venv。请把本窗口内容发给维护者。"
    pause_on_error
    exit 1
  fi
fi

if [ ! -f ".venv/.mailpilot-ready-v0.2.0" ]; then
  echo "首次启动：正在安装依赖（需要网络，可能等待几分钟）……"
  if ! .venv/bin/python -m pip install --disable-pip-version-check -e ".[dev]"; then
    echo "❌ 依赖安装失败。请检查网络，并把本窗口内容发给维护者。"
    pause_on_error
    exit 1
  fi
  touch .venv/.mailpilot-ready-v0.2.0
fi

if [ "${MAILPILOT_SMOKE:-}" = "1" ]; then
  .venv/bin/python -c "import app; response=app.app.test_client().get('/'); assert response.status_code == 200; print('MailPilot launcher smoke: HTTP', response.status_code)"
  exit $?
fi

.venv/bin/python app.py
status=$?
if [ "$status" -ne 0 ]; then
  echo "❌ MailPilot 异常退出（代码 $status）。请把本窗口内容发给维护者。"
  pause_on_error
fi
exit "$status"

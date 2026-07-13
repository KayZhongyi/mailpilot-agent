#!/bin/bash
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "请先双击 run_mailpilot_app.command 完成首次环境安装。"
  read -r -p "按回车键关闭……" _
  exit 1
fi

if ! .venv/bin/python -c "import pytest, ruff" >/dev/null 2>&1; then
  echo "验收工具未安装。请联网重新双击 run_mailpilot_app.command 完成一次环境更新。"
  read -r -p "按回车键关闭……" _
  exit 1
fi

.venv/bin/python scripts/safety_acceptance.py
status=$?
echo
read -r -p "验收结束。按回车键关闭……" _
exit "$status"

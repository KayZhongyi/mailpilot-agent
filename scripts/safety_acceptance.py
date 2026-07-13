#!/usr/bin/env python3
"""Run MailPilot's no-real-network acceptance gate."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(label, command):
    print(f"\n== {label} ==")
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        print(f"\n❌ {label} 未通过（退出码 {completed.returncode}）。")
        return False
    print(f"✅ {label} 通过")
    return True


def main():
    print("MailPilot 安全验收：全程禁止真实 SMTP / IMAP / 网络连接。")
    checks = [
        ("自动化与故障恢复测试", [sys.executable, "-m", "pytest", "-q"]),
        ("Python 静态检查", [sys.executable, "-m", "ruff", "check", "."]),
        (
            "Flask 首页离线检查",
            [
                sys.executable,
                "-c",
                "import app; r=app.app.test_client().get('/'); "
                "assert r.status_code == 200; print('GET /', r.status_code)",
            ],
        ),
    ]
    if sys.platform != "win32":
        checks.append(("macOS 启动脚本语法", ["bash", "-n", "run_mailpilot_app.command"]))
    if not all(run(label, command) for label, command in checks):
        return 1
    print("\n✅ 全部安全验收通过。没有发送真实邮件，也没有连接真实邮箱。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

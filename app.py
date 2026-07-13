#!/usr/bin/env python3
"""Local web UI for MailPilot Agent.

This is a lightweight Flask app, not a SaaS service. It runs on localhost and
reuses the same preview/send engine as the CLI and Agent Skill.
"""

from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import webbrowser

import pandas as pd
from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for
from werkzeug.serving import make_server

from mailpilot import mailer


ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
RUNS_DIR = ROOT / ".mailpilot_runs"
RUNS_DIR.mkdir(exist_ok=True)
mailer._fsync_parent(RUNS_DIR)
for stale_pattern in ("mailpilot_*/smtp-*.yaml", "mailpilot_*/config.yaml"):
    for stale_config in RUNS_DIR.glob(stale_pattern):
        stale_config.unlink(missing_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
CAPTURE_LOCK = threading.Lock()

CSS = """
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --border: #e5e7eb;
  --text: #15171a;
  --muted: #697386;
  --accent: #4568dc;
  --accent-dark: #2447b8;
  --danger: #b42318;
  --success: #027a48;
  --warn: #8a5a00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  color: var(--text);
  background: linear-gradient(180deg, #fbfcfd 0%, var(--bg) 260px), var(--bg);
  letter-spacing: 0;
}
.shell {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  min-height: 100vh;
}
aside {
  border-right: 1px solid var(--border);
  background: rgba(255,255,255,0.72);
  padding: 28px 22px;
}
main {
  padding: 32px;
  max-width: 1180px;
  width: 100%;
}
.brand {
  font-size: 15px;
  font-weight: 760;
  margin-bottom: 4px;
}
.muted, .help, label span {
  color: var(--muted);
}
.help {
  font-size: 13px;
  line-height: 1.55;
}
.runbook {
  margin-top: 28px;
  padding-top: 24px;
  border-top: 1px solid var(--border);
}
.runbook div {
  color: #333946;
  font-size: 13px;
  padding: 7px 0;
}
.hero {
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.88);
  border-radius: 9px;
  padding: 26px 28px;
  box-shadow: 0 18px 44px rgba(16, 24, 40, 0.06);
}
.eyebrow {
  color: var(--accent);
  font-size: 12px;
  font-weight: 760;
  text-transform: uppercase;
  margin-bottom: 8px;
}
h1 {
  font-size: 34px;
  line-height: 1.12;
  margin: 0;
}
h2 {
  font-size: 20px;
  margin: 0 0 16px;
}
h3 {
  font-size: 15px;
  margin: 0 0 12px;
}
.subtitle {
  max-width: 760px;
  color: var(--muted);
  font-size: 15px;
  line-height: 1.65;
  margin: 10px 0 0;
}
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}
.pill {
  border: 1px solid var(--border);
  border-radius: 999px;
  background: #fafafa;
  color: #343946;
  font-size: 12px;
  padding: 6px 10px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  margin-top: 22px;
}
.card {
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: 9px;
  padding: 20px;
  box-shadow: 0 8px 24px rgba(16, 24, 40, 0.04);
}
.full { grid-column: 1 / -1; }
form { margin: 0; }
label {
  display: block;
  font-size: 13px;
  font-weight: 650;
  margin: 12px 0 6px;
}
input, select, textarea {
  width: 100%;
  border: 1px solid #cfd5df;
  border-radius: 7px;
  background: #fff;
  color: var(--text);
  font: inherit;
  padding: 9px 10px;
}
textarea {
  min-height: 170px;
  resize: vertical;
}
input[type="checkbox"] {
  width: auto;
  margin-right: 8px;
}
.row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-top: 16px;
}
button, .button {
  appearance: none;
  border: 1px solid #cfd5df;
  border-radius: 7px;
  background: #fff;
  color: var(--text);
  cursor: pointer;
  font-weight: 700;
  padding: 10px 13px;
  text-decoration: none;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
}
button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
button.primary:hover { background: var(--accent-dark); }
button.danger {
  background: #fff5f5;
  border-color: #f7b5b0;
  color: var(--danger);
}
.metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.metric {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  background: #fff;
}
.metric .label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
.metric .value {
  font-size: 26px;
  font-weight: 760;
  margin-top: 4px;
}
.notice {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  background: #fbfbfc;
  color: #313846;
  font-size: 13px;
  line-height: 1.55;
}
.notice.success { border-color: #abefc6; background: #f6fef9; color: var(--success); }
.notice.error { border-color: #fecdca; background: #fff7f7; color: var(--danger); }
.notice.warn { border-color: #fedf89; background: #fffbeb; color: var(--warn); }
pre {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #0d1117;
  color: #e6edf3;
  overflow: auto;
  padding: 14px;
  font-size: 12px;
  line-height: 1.5;
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
th, td {
  border-bottom: 1px solid var(--border);
  padding: 9px 10px;
  text-align: left;
  vertical-align: top;
}
th {
  color: #343946;
  background: #fafafa;
  font-size: 12px;
}
.table-wrap {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: auto;
  max-height: 420px;
}
.tabs {
  display: flex;
  gap: 8px;
  margin: 20px 0 0;
}
.tabs a {
  border: 1px solid var(--border);
  border-radius: 999px;
  color: #343946;
  font-size: 13px;
  font-weight: 700;
  padding: 8px 12px;
  text-decoration: none;
  background: #fff;
}
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  aside { border-right: 0; border-bottom: 1px solid var(--border); }
  main { padding: 20px; }
  .grid, .row, .metrics { grid-template-columns: 1fr; }
}
"""

PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MailPilot Agent</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">MailPilot Agent</div>
      <div class="help">本地邮件合并工作台，不需要 AI 软件。</div>
      <div class="runbook">
        <div>1. 上传并预览名单</div>
        <div>2. 处理数据阻断</div>
        <div>3. 每个模板发送测试</div>
        <div>4. 小批发送与安全恢复</div>
        <div>5. 关闭外发并扫描退信</div>
        <div>6. 完成对账后归档</div>
      </div>
      <div class="runbook help">
        仅在本机运行。预览不会发送邮件，密码不会写入磁盘。
      </div>
    </aside>
    <main>
      <section class="hero">
        <div class="eyebrow">本地邮件合并工作台</div>
        <h1>先预览、再测试、最后分批发送。</h1>
        <p class="subtitle">
          上传 CSV/XLSX 名单，人工确认业务分类与模板；不安全的行会被阻断，发送进度可在中断后恢复。
        </p>
        <div class="pills">
          <span class="pill">Preview-first</span>
          <span class="pill">Crash-safe ledger</span>
          <span class="pill">Grouped templates</span>
          <span class="pill">Local-only</span>
        </div>
      </section>

      <div class="tabs">
        <a href="#preview">预览</a>
        <a href="#send">发送</a>
        <a href="#templates">模板</a>
      </div>

      {% if message %}
        <div class="notice {{ message_kind }}">{{ message }}</div>
      {% endif %}

      <div class="grid">
        {% if runs %}
          <section class="card full">
            <h2>恢复或查看已有批次</h2>
            <p class="help">中断后必须打开原批次；不要靠重新上传名单恢复。</p>
            <div class="table-wrap">
              <table>
                <thead><tr><th>活动</th><th>批次</th><th>阶段</th><th>更新时间</th><th>待发送</th><th>已接受</th><th>不确定</th><th>失败</th><th>退信</th><th></th></tr></thead>
                <tbody>
                  {% for run in runs %}
                    <tr>
                      <td><strong>{{ run.activity_name }}</strong></td>
                      <td><code>{{ run.name }}</code></td>
                      <td>{{ "已归档" if run.archived else run.phase_label }}</td>
                      <td>{{ run.updated }}</td>
                      <td>{{ run.status_counts.ready }}</td>
                      <td>{{ run.status_counts.accepted }}</td>
                      <td>{{ run.status_counts.unknown }}</td>
                      <td>{{ run.status_counts.error }}</td>
                      <td>{{ run.status_counts.bounced }}</td>
                      <td><a class="button" href="{{ run.url }}">安全打开</a></td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </section>
        {% endif %}

        <section class="card" id="preview">
          <h2>上传并预览</h2>
          <p class="help">此步骤绝不发送邮件。MailPilot 只渲染模板并标记不安全行。</p>
          <form action="/preview" method="post" enctype="multipart/form-data">
            <input type="hidden" name="activity_token" value="{{ activity_token }}">
            <input type="hidden" name="activity_from" value="{{ activity_from }}">
            {% if activity_token %}<div class="notice warn">正在创建新活动：<strong>{{ activity_name }}</strong>。授权仅可使用一次，并将在 30 分钟后失效。</div>{% endif %}
            <label>活动名称</label>
            <input name="activity_name" value="{{ activity_name }}" placeholder="例如：2026 夏季申请结果通知" required>
            <label>收件人 CSV/XLSX</label>
            <input id="recipients-file" type="file" name="recipients" accept=".csv,.xlsx" required>
            <div id="column-detection" class="help">选择文件后，MailPilot 会在本机读取表头并自动填入唯一匹配的列；请在预览前核对。</div>
            <datalist id="uploaded-columns"></datalist>

            <div class="row">
              <div>
                <label>模式</label>
                <select name="mode">
                  <option value="grouped">分组模式（必须选择业务已确认的分类列）</option>
                  <option value="simple">单模板模式</option>
                </select>
              </div>
              <div>
                <label>单模板模式使用的模板</label>
                <select name="template">
                  {% for tpl in templates %}
                    <option value="{{ tpl }}">{{ tpl }}</option>
                  {% endfor %}
                </select>
              </div>
            </div>

            <div class="row">
              <div>
                <label>邮箱列 <span>可选，建议明确填写</span></label>
                <input name="email_col" list="uploaded-columns" placeholder="email">
              </div>
              <div>
                <label>称呼/姓名列 <span>可选</span></label>
                <input name="name_col" list="uploaded-columns" placeholder="name">
              </div>
            </div>
            <label>业务结果/模板列 <span>分组模式必填；MailPilot 不会猜</span></label>
            <input name="group_col" list="uploaded-columns" placeholder="例如：邮件类型（MailPilot 不会猜测）">
            <label>可信的历史已发送列 <span>如果有人手工发过，必须填写</span></label>
            <input name="sent_col" list="uploaded-columns" placeholder="例如：已发送">
            <label><input type="checkbox" name="confirm_no_history"> 我确认这个活动此前没有任何人手工发送过名单中的邮件。</label>
            <div class="actions">
              <button class="primary" type="submit">预览名单（不会发送）</button>
            </div>
          </form>
        </section>

        <section class="card" id="send">
          <h2>测试或发送</h2>
          {% if not sendable_path %}
            <div class="notice warn">请先完成预览。</div>
          {% elif is_archived %}
            <div class="notice success">该活动已经归档，不能再从这个批次发送。</div>
            <p><a class="button" href="/runs/{{ run_name }}/audit.json">下载审计报告</a></p>
            <h3>明确开始一个全新活动</h3>
            <p class="help">只有完成归档后才能复用相同收件人。新活动会生成独立 Activity ID 和 Message-ID 命名空间。</p>
            <form action="/new-activity" method="post">
              <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
              <input type="hidden" name="run_dir" value="{{ run_dir }}">
              <input type="hidden" name="action_token" value="{{ action_token }}">
              <label>新活动名称</label><input name="activity_name" required>
              <label>输入 <code>新活动 活动名称</code></label><input name="confirm_phrase" required>
              <button type="submit">授权一次新活动预览</button>
            </form>
          {% elif lifecycle_phase == "OUTBOUND_CLOSED" %}
            <div class="notice warn">外发已经永久关闭。下一步是扫描退信并归档。</div>
          {% else %}
            <p class="help">当前安全批次文件：<code>{{ sendable_path }}</code></p>
            <form action="/send" method="post">
              <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
              <input type="hidden" name="run_dir" value="{{ run_dir }}">
              <input type="hidden" name="action_token" value="{{ action_token }}">

              <div class="row">
                <div>
                  <label>SMTP 服务器地址</label>
                  <input name="host" value="smtp.gmail.com" required>
                </div>
                <div>
                  <label>SMTP 端口</label>
                  <input name="port" value="465" required>
                </div>
              </div>
              <label><input type="checkbox" name="use_ssl" checked> 使用 SSL 加密连接</label>
              <label>SMTP 邮箱账号</label>
              <input name="user" required>
              <label>邮箱应用专用密码（不会保存）</label>
              <input name="password" type="password" required>
              <div class="row">
                <div>
                  <label>发件地址</label>
                  <input name="from_addr">
                </div>
                <div>
                  <label>发件人显示名称</label>
                  <input name="from_name">
                </div>
              </div>
              <label>退订邮箱或网址</label>
              <input name="unsubscribe">

              <div class="row">
                <div>
                  <label>把所有模板重定向到测试邮箱</label>
                  <input name="redirect_to" placeholder="必须等于 SMTP 账号或发件地址">
                  <p class="help">只能填写你自己的发件测试邮箱，而且该地址不能出现在客户名单中。</p>
                </div>
                <div>
                  <label>只处理指定分组/模板（可选）</label>
                  <input name="only" placeholder="confirmed">
                </div>
              </div>
              <div class="row">
                <div>
                  <label>本次最多尝试封数（1–100）</label>
                  <input name="limit" type="number" min="1" max="100" value="3" required>
                </div>
                <div>
                  <label>每封邮件之间等待秒数</label>
                  <input name="sleep" type="number" min="1" step="0.5" value="2" required>
                </div>
              </div>
              <label><input type="checkbox" name="confirm_real_send"> 我已核对预览，明白正式发送会联系真实收件人。</label>
              <label><input type="checkbox" name="confirm_test_received"> 我已收到测试邮件，并检查了本活动的每个模板。</label>
              <label>正式发送时，请按上面的封数输入 <code>发送 N 封</code></label>
              <input name="confirm_phrase" placeholder="例如：发送 3 封">
              <div class="actions">
                <button type="submit" name="action" value="dry" formnovalidate>仅演练（绝不连接邮箱）</button>
                <button type="submit" name="action" value="test">只发送到测试邮箱</button>
                <button class="danger" type="submit" name="action" value="send">确认正式发送</button>
              </div>
            </form>
          {% endif %}
        </section>

        {% if preview_output %}
          <section class="card full">
            <h2>预览与当前状态</h2>
            <div class="metrics">
              <div class="metric"><div class="label">待发送</div><div class="value">{{ status_counts.ready }}</div></div>
              <div class="metric"><div class="label">SMTP 已接受</div><div class="value">{{ status_counts.accepted }}</div></div>
              <div class="metric"><div class="label">结果不确定</div><div class="value">{{ status_counts.unknown }}</div></div>
              <div class="metric"><div class="label">明确失败</div><div class="value">{{ status_counts.error }}</div></div>
              <div class="metric"><div class="label">已退信</div><div class="value">{{ status_counts.bounced }}</div></div>
              <div class="metric"><div class="label">历史已发</div><div class="value">{{ status_counts.historical_sent }}</div></div>
              <div class="metric"><div class="label">主动停止</div><div class="value">{{ status_counts.suppressed }}</div></div>
              <div class="metric"><div class="label">数据阻断</div><div class="value">{{ status_counts.blocked }}</div></div>
            </div>
            {% if status_counts.unknown %}
              <div class="notice error">存在结果不确定邮件：整个批次已暂停，必须先根据服务商记录逐条对账，绝不会自动重试。</div>
            {% endif %}
            <pre>{{ preview_output }}</pre>
            <p class="help">页面只展示前 200 行。请下载审计报告查看完整状态。</p>
            {% if run_name %}<p><a class="button" href="/runs/{{ run_name }}/status.csv">下载完整状态 CSV</a> <a class="button" href="/runs/{{ run_name }}/audit.json">下载完整审计 JSON</a></p>{% endif %}
            {{ table_html|safe }}
          </section>
        {% endif %}

        {% if sendable_path and production_started and not is_archived %}
          <section class="card full" id="lifecycle">
            <h2>活动收尾与对账</h2>
            <p class="help">当前阶段：<strong>{{ phase_label }}</strong>（<code>{{ lifecycle_phase }}</code>）。所有操作都会写入本地审计台账。</p>
            {% if lifecycle_phase == "OUTBOUND_OPEN" %}
              {% if status_counts.unknown or status_counts.error %}
                <h3>逐条处理异常状态</h3>
                <div class="table-wrap"><table><thead><tr><th>邮箱</th><th>状态</th><th>错误</th><th>可用的精确确认短语</th></tr></thead><tbody>{% for item in attention_rows %}<tr><td>{{ item.email }}</td><td>{{ '结果不确定' if item.status == 'unknown' else '明确失败' }}</td><td>{{ item.error }}</td><td>{% for phrase in item.phrases %}<code>{{ phrase }}</code>{% if not loop.last %}<br>{% endif %}{% endfor %}</td></tr>{% endfor %}</tbody></table></div>
                <form action="/lifecycle" method="post">
                  <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
                  <input type="hidden" name="run_dir" value="{{ run_dir }}">
                  <input type="hidden" name="action_token" value="{{ action_token }}">
                  <label>完整收件邮箱</label><input name="resolve_email" required>
                  <label>处理方式</label>
                  <select name="lifecycle_action">
                    <option value="unknown_sent">不确定 → 已从服务商确认接受</option>
                    <option value="unknown_suppress">不确定 → 无法确认，永久不重发</option>
                    <option value="error_retry">明确失败 → 授权重试</option>
                    <option value="error_suppress">明确失败 → 关闭失败，不再发送</option>
                  </select>
                  <label>核对依据/负责人备注（至少 8 个字）</label><input name="evidence" required>
                  <label>页面提示的精确确认短语</label><input name="confirm_phrase" required>
                  <button type="submit">记录人工处置</button>
                </form>
              {% endif %}
              {% if status_counts.ready %}
                <h3>停止剩余待发邮件</h3>
                <form action="/lifecycle" method="post">
                  <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
                  <input type="hidden" name="run_dir" value="{{ run_dir }}">
                  <input type="hidden" name="action_token" value="{{ action_token }}">
                  <input type="hidden" name="lifecycle_action" value="suppress_ready">
                  <label>停止原因（至少 8 个字）</label><input name="evidence" required>
                  <label>输入 <code>停止剩余 {{ status_counts.ready }} 封</code></label><input name="confirm_phrase" required>
                  <button type="submit">永久停止剩余邮件</button>
                </form>
              {% endif %}
              <h3>关闭外发</h3>
              <form action="/lifecycle" method="post">
                <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
                <input type="hidden" name="run_dir" value="{{ run_dir }}">
                <input type="hidden" name="action_token" value="{{ action_token }}">
                <input type="hidden" name="lifecycle_action" value="close">
                <label><input type="checkbox" name="confirm_close"> 我确认关闭后，这个批次永远不能继续发送。</label>
                <label>输入 <code>关闭外发 {{ batch_short_id }}</code></label>
                <input name="confirm_phrase" required>
                <button type="submit">关闭外发</button>
              </form>
            {% elif lifecycle_phase == "OUTBOUND_CLOSED" %}
              <h3>扫描退信（只读取，不写入）</h3>
              <form action="/bounces" method="post">
                <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
                <input type="hidden" name="run_dir" value="{{ run_dir }}">
                <input type="hidden" name="action_token" value="{{ action_token }}">
                <input type="hidden" name="bounce_action" value="scan">
                <div class="row"><div><label>IMAP 主机</label><input name="imap_host" placeholder="imap.example.com" required></div><div><label>端口</label><input name="imap_port" value="993" required></div></div>
                <label>邮箱账号</label><input name="imap_user" required>
                <label>应用专用密码（仅保存在内存）</label><input name="imap_password" type="password" required>
                <label>发件地址（必须与发送时一致）</label><input name="from_addr" required>
                <label>本活动开始后最多扫描邮件数</label><input name="lookback" type="number" min="1" max="50000" value="5000" required>
                <button type="submit">扫描退信，不修改状态</button>
              </form>
              {% if bounce_reconciliation %}
                <div class="notice {{ 'success' if bounce_reconciliation.coverage.complete else 'error' }}">扫描 {{ bounce_reconciliation.checked_at }}：覆盖 {{ bounce_reconciliation.coverage.scanned_messages }}/{{ bounce_reconciliation.coverage.matching_messages }} 封活动开始后的邮箱邮件；可验证 {{ bounce_reconciliation.matched }} 条；仅地址相同但无法绑定 {{ bounce_reconciliation.unverified }} 条；已应用 {{ bounce_reconciliation.applied }} 条。</div>
                {% if not bounce_reconciliation.coverage.complete %}<div class="notice error">扫描范围不完整，必须提高扫描上限并重新扫描，当前结果不能归档。</div>{% endif %}
                <p><a class="button" href="/runs/{{ run_name }}/bounces.json">下载完整退信核对报告</a></p>
                {% if bounce_reconciliation.candidates %}
                  <h4>可自动验证的退信</h4>
                  <div class="table-wrap"><table><thead><tr><th>邮箱</th><th>原因</th><th>本活动 Message-ID</th></tr></thead><tbody>{% for item in bounce_reconciliation.candidates %}<tr><td>{{ item.email }}</td><td>{{ item.reason }}</td><td><code>{{ item.message_id }}</code></td></tr>{% endfor %}</tbody></table></div>
                {% endif %}
                {% if bounce_reconciliation.unverified_candidates %}
                  <h4>必须人工核对，绝不自动修改</h4>
                  <div class="table-wrap"><table><thead><tr><th>邮箱</th><th>退信原因</th><th>预期 Message-ID</th><th>退信中看到的 Message-ID</th></tr></thead><tbody>{% for item in bounce_reconciliation.unverified_candidates %}<tr><td>{{ item.email }}</td><td>{{ item.reason }}</td><td><code>{{ item.expected_message_id }}</code></td><td><code>{{ item.observed_message_ids|join(', ') or '无' }}</code></td></tr>{% endfor %}</tbody></table></div>
                {% endif %}
                {% if bounce_reconciliation.unapplied or (bounce_reconciliation.unverified and not bounce_reconciliation.unverified_acknowledged) %}
                <form action="/bounces" method="post">
                  <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
                  <input type="hidden" name="run_dir" value="{{ run_dir }}">
                  <input type="hidden" name="action_token" value="{{ action_token }}">
                  <input type="hidden" name="bounce_action" value="{{ 'apply' if bounce_reconciliation.unapplied else 'acknowledge' }}">
                  {% if bounce_reconciliation.unapplied %}<label>输入 <code>应用 {{ bounce_reconciliation.matched }} 条退信</code></label><input name="confirm_phrase" required>{% endif %}
                  {% if bounce_reconciliation.unverified %}<label><input type="checkbox" name="ack_unverified"> 我已人工核对无法自动关联的候选</label><label>人工核对备注</label><input name="unverified_note" required><label>输入 <code>确认人工核对 {{ bounce_reconciliation.unverified }} 条</code></label><input name="ack_phrase" required>{% endif %}
                  <button type="submit">{{ '应用已验证退信' if bounce_reconciliation.unapplied else '记录人工核对' }}</button>
                </form>
                {% endif %}
                <h3>最终归档</h3>
                <div class="notice warn">不要在最后一封发出后立即归档。请按邮箱服务商规则等待退信窗口（通常至少 24–72 小时），然后重新扫描；v0.2 归档后不能追加晚到退信。</div>
                <form action="/archive" method="post">
                  <input type="hidden" name="sendable_path" value="{{ sendable_path }}">
                  <input type="hidden" name="run_dir" value="{{ run_dir }}">
                  <input type="hidden" name="action_token" value="{{ action_token }}">
                  <label><input type="checkbox" name="confirm_waited_bounce_window"> 我已等待适用的退信窗口，并在归档前重新扫描。</label>
                  <label><input type="checkbox" name="confirm_delivery_limits"> 我理解 SMTP 接受不等于最终送达，晚到退信仍可能出现。</label>
                  <label>输入 <code>归档 {{ batch_short_id }}</code></label><input name="confirm_phrase" required>
                  <button type="submit">完成并归档活动</button>
                </form>
              {% endif %}
            {% endif %}
          </section>
        {% endif %}

        {% if send_output %}
          <section class="card full">
            <h2>操作结果</h2>
            <pre>{{ send_output }}</pre>
            {{ table_html|safe }}
          </section>
        {% endif %}

        <section class="card full" id="templates">
          <h2>模板</h2>
          <p class="help">分组值会直接映射到同名模板文件；缺失模板会在预览阶段阻断。</p>
          <div class="grid">
            {% for tpl in template_files %}
              <div class="card">
                <h3>{{ tpl.name }}</h3>
                <pre>{{ tpl.text }}</pre>
              </div>
            {% endfor %}
          </div>
        </section>
      </div>
    </main>
  </div>
  <script>
    const fileInput = document.getElementById('recipients-file');
    const columnStatus = document.getElementById('column-detection');
    if (fileInput) fileInput.addEventListener('change', async function () {
      if (!fileInput.files.length) return;
      columnStatus.textContent = '正在本机读取表头……';
      const data = new FormData();
      data.append('recipients', fileInput.files[0]);
      try {
        const response = await fetch('/columns', {method: 'POST', body: data});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || '无法读取表头');
        const list = document.getElementById('uploaded-columns');
        list.replaceChildren(...result.columns.map(function (name) {
          const option = document.createElement('option'); option.value = name; return option;
        }));
        const mapping = {email_col: 'email', name_col: 'name', group_col: 'group', sent_col: 'sent'};
        Object.entries(mapping).forEach(function (entry) {
          const input = document.querySelector('[name="' + entry[0] + '"]');
          const candidates = result.suggestions[entry[1]] || [];
          if (input && !input.value && candidates.length === 1) input.value = candidates[0];
        });
        columnStatus.textContent = '已读取列：' + result.columns.join('、') + '。请确认自动填入的列符合业务含义。';
      } catch (error) {
        columnStatus.textContent = '表头读取失败：' + error.message + '。仍可打开表格并手工复制列名。';
      }
    });
  </script>
</body>
</html>
"""


def _capture_with_result(func, args):
    buf = io.StringIO()
    try:
        # redirect_stdout mutates process-global state, so captured commands must not overlap.
        with CAPTURE_LOCK:
            with redirect_stdout(buf):
                result = func(args)
        return True, buf.getvalue(), result
    except SystemExit as e:
        return False, f"{buf.getvalue()}\n{e}".strip(), None
    except Exception as e:
        return False, f"{buf.getvalue()}\n{type(e).__name__}: {e}".strip(), None


def _capture(func, args):
    ok, output, _result = _capture_with_result(func, args)
    return ok, output


def _read_sendable(path):
    if not path or not Path(path).exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=object).fillna("")


def _metric_counts(df):
    counts = _status_counts(df)
    return {
        "ready": counts["ready"],
        "skipped": (
            counts["accepted"]
            + counts["historical_sent"]
            + counts["error"]
            + counts["unknown"]
            + counts["bounced"]
            + counts["suppressed"]
        ),
        "blocked": counts["blocked"],
    }


def _status_counts(df):
    if df.empty:
        return {
            "total": 0,
            "ready": 0,
            "accepted": 0,
            "historical_sent": 0,
            "error": 0,
            "unknown": 0,
            "bounced": 0,
            "suppressed": 0,
            "blocked": 0,
        }
    sendable = df["sendable"].astype(str).str.lower().isin(["yes", "true", "1"])
    status = df["status"].astype(str).str.lower() if "status" in df else pd.Series("", index=df.index)
    initial_status = (
        df["initial_status"].astype(str).str.lower()
        if "initial_status" in df
        else pd.Series("", index=df.index)
    )
    suppressed = status.isin(mailer.SUPPRESSED_STATUSES) | initial_status.isin(
        mailer.SUPPRESSED_STATUSES
    )
    unknown = status.eq("unknown") | initial_status.eq("unknown")
    bounced = (status.eq("bounced") | initial_status.eq("bounced")) & ~unknown
    error = (status.eq("error") | initial_status.eq("error")) & ~(unknown | bounced)
    historical_sent = initial_status.eq("sent") & ~(unknown | bounced | error)
    accepted = status.eq("sent") & ~(historical_sent | unknown | bounced | error)
    other_suppressed = suppressed & ~(
        historical_sent | unknown | bounced | error | accepted
    )
    return {
        "total": int(len(df)),
        "ready": int((sendable & ~suppressed).sum()),
        "accepted": int((sendable & accepted).sum()),
        "historical_sent": int((sendable & historical_sent).sum()),
        "error": int((sendable & error).sum()),
        "unknown": int((sendable & unknown).sum()),
        "bounced": int((sendable & bounced).sum()),
        "suppressed": int((sendable & other_suppressed).sum()),
        "blocked": int((~sendable).sum()),
    }


def _pending_templates(df):
    sendable = df["sendable"].astype(str).str.lower().isin(["yes", "true", "1"])
    status = df["status"].astype(str).str.lower()
    initial_status = df["initial_status"].astype(str).str.lower()
    pending = df[
        sendable
        & ~status.isin(mailer.SUPPRESSED_STATUSES)
        & ~initial_status.isin(mailer.SUPPRESSED_STATUSES)
    ]
    return sorted(set(pending["template"].astype(str)))


def _table_html(df):
    if df.empty:
        return ""
    columns = [c for c in ["name", "email", "template", "subject", "status", "sendable", "reason", "send_time", "send_error"] if c in df.columns]
    safe = df[columns].head(200).copy()
    return f'<div class="table-wrap">{safe.to_html(index=False, escape=True)}</div>'


def _attention_rows(df):
    rows = []
    if df.empty:
        return rows
    for _, row in df.iterrows():
        status = str(row.get("status", "")).strip().lower()
        email_address = str(row.get("email", "")).strip().lower()
        if status == "unknown":
            actions = [
                f"确认已接受 {email_address}",
                f"永久不重发 {email_address}",
            ]
        elif status == "error":
            actions = [
                f"重试明确失败 {email_address}",
                f"关闭失败 {email_address}",
            ]
        else:
            continue
        rows.append(
            {
                "email": email_address,
                "status": status,
                "error": str(row.get("send_error", "")),
                "phrases": actions,
            }
        )
    return rows


def _phase_label(phase):
    return {
        "PREVIEW": "预览中",
        "OUTBOUND_OPEN": "外发进行中",
        "OUTBOUND_CLOSED": "外发已关闭",
        "ARCHIVED": "已归档",
        "REVIEW_REQUIRED": "需要维护者检查",
    }.get(str(phase), str(phase))


def _content_hash(df):
    columns = [
        c
        for c in (
            "record_id",
            "email",
            "template",
            "subject",
            "body",
            "initial_status",
            "activity_id",
            "sendable",
            "reason",
        )
        if c in df.columns
    ]
    payload = df[columns].fillna("").astype(str).to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _batch_fingerprint(df):
    fields = (
        "email",
        "template",
        "subject",
        "body",
        "status",
        "initial_status",
        "sendable",
        "reason",
    )
    rows = []
    for _, row in df.iterrows():
        normalized = []
        for field in fields:
            value = str(row.get(field, "")).replace("\r\n", "\n").replace("\r", "\n").strip()
            normalized.append(value.lower() if field == "email" else value)
        rows.append(normalized)
    payload = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recipient_set_hash(df):
    recipients = sorted(str(value).strip().lower() for value in df["email"] if str(value).strip())
    payload = json.dumps(recipients, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recipient_hashes(df):
    return sorted(
        {
            hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()
            for value in df["email"]
            if str(value).strip()
        }
    )


def _assign_activity(df, activity_id):
    df = df.copy()
    df["activity_id"] = str(activity_id)
    df["record_id"] = [
        mailer._record_id(
            idx,
            str(row.get("email", "")).strip(),
            str(row.get("template", "")).strip(),
            str(row.get("subject", "")),
            str(row.get("body", "")),
            str(row.get("sendable", "")).strip().lower(),
            str(row.get("reason", "")),
            str(row.get("initial_status", "")).strip().lower(),
            str(activity_id),
        )
        for idx, row in df.iterrows()
    ]
    df["batch_id"] = mailer._batch_id_for_record_ids(df["record_id"].astype(str))
    return df


def _apply_archived_suppression(df, archived_runs):
    if isinstance(archived_runs, (str, Path)):
        archived_runs = [Path(archived_runs)]
    suppressed_addresses = set()
    for archived_run in archived_runs:
        archived_run = Path(archived_run)
        if not _archive_receipt(archived_run):
            continue
        previous = _read_sendable(archived_run / "sendable.csv")
        mailer._apply_ledger(
            previous,
            mailer._default_ledger_path(str(archived_run / "sendable.csv")),
        )
        for _, row in previous.iterrows():
            status = str(row.get("status", "")).strip().lower()
            initial = str(row.get("initial_status", "")).strip().lower()
            if status in {"bounced", "unsubscribed"} or initial in {
                "bounced",
                "unsubscribed",
            }:
                suppressed_addresses.add(str(row.get("email", "")).strip().lower())
    if not suppressed_addresses:
        return df, 0
    df = df.copy()
    blocked = 0
    for idx, row in df.iterrows():
        if str(row.get("email", "")).strip().lower() in suppressed_addresses:
            df.at[idx, "sendable"] = "no"
            df.at[idx, "reason"] = (
                "recipient was bounced or unsubscribed in an archived MailPilot activity"
            )
            blocked += 1
    return df, blocked


def _template_names():
    names = sorted(p.stem for p in TEMPLATES_DIR.glob("*.txt"))
    return names or ["default"]


def _template_files():
    return [
        {"name": p.name, "text": p.read_text(encoding="utf-8")}
        for p in sorted(TEMPLATES_DIR.glob("*.txt"))
    ]


def _config_from_form(form):
    user = form.get("user", "").strip()
    from_addr = form.get("from_addr", "").strip() or user
    config = {
        "smtp": {
            "host": form.get("host", "").strip(),
            "port": int(form.get("port", "465") or 465),
            "use_ssl": bool(form.get("use_ssl")),
            "user": user,
            "password": form.get("password", ""),
        },
        "from_addr": from_addr,
        "from_name": form.get("from_name", "").strip(),
        "unsubscribe": form.get("unsubscribe", "").strip() or from_addr,
    }
    mailer.normalize_config(config)
    return config


def _imap_config_from_form(form):
    user = form.get("imap_user", "").strip()
    imap_host = form.get("imap_host", "").strip()
    config = {
        "smtp": {
            "host": form.get("smtp_host", "").strip() or "smtp.local.invalid",
            "port": 465,
            "use_ssl": True,
            "user": user,
            "password": form.get("imap_password", ""),
        },
        "imap": {
            "host": imap_host,
            "port": int(form.get("imap_port", "993") or 993),
        },
        "from_addr": form.get("from_addr", "").strip() or user,
    }
    normalized = mailer.normalize_config(config)
    if not imap_host:
        raise ValueError("必须填写 IMAP 服务器地址。")
    return config, normalized


def _smtp_identity(form):
    user = form.get("user", "").strip().lower()
    return {
        "host": form.get("host", "").strip().lower(),
        "port": int(form.get("port", "465") or 465),
        "use_ssl": bool(form.get("use_ssl")),
        "user": user,
        "from_addr": (form.get("from_addr", "").strip() or user).lower(),
        "from_name": form.get("from_name", "").strip(),
        "unsubscribe": form.get("unsubscribe", "").strip(),
    }


def _write_test_marker(run_dir, df, templates, form):
    payload = {
        "content_hash": _content_hash(df),
        "templates": sorted(templates),
        "smtp_identity": _smtp_identity(form),
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (Path(run_dir) / "redirect-test-passed.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_test_marker(run_dir):
    path = Path(run_dir) / "redirect-test-passed.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_paths(sendable_path, run_dir):
    try:
        run = Path(run_dir).resolve(strict=True)
        sendable = Path(sendable_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, None
    runs_root = RUNS_DIR.resolve()
    if run.parent != runs_root or not run.name.startswith("mailpilot_"):
        return None, None
    if sendable.parent != run or sendable.name != "sendable.csv":
        return None, None
    return sendable, run


def _new_action_token(run_dir):
    token = secrets.token_urlsafe(32)
    (Path(run_dir) / "action.token").write_text(token, encoding="utf-8")
    return token


def _consume_action_token(run_dir, submitted):
    token_path = Path(run_dir) / "action.token"
    try:
        with mailer.BatchLock(str(Path(run_dir) / "action.lock")):
            expected = token_path.read_text(encoding="utf-8").strip()
            if not submitted or not secrets.compare_digest(expected, submitted):
                return None
            return _new_action_token(run_dir)
    except (OSError, mailer.BatchAlreadyLocked):
        return None


def _new_activity_authorization(run_dir, activity_name):
    token = secrets.token_urlsafe(24)
    receipt_path = Path(run_dir) / "archive-receipt.json"
    payload = {
        "token": token,
        "archived_run": Path(run_dir).name,
        "archive_receipt_hash": _file_hash(receipt_path),
        "activity_name": activity_name,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "expires_at": datetime.fromtimestamp(
            datetime.now().timestamp() + 30 * 60
        ).astimezone().isoformat(timespec="seconds"),
    }
    _write_once_json(RUNS_DIR / f".new-activity-{token}.json", payload)
    return token


def _consume_activity_authorization(token, archived_run, activity_name):
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
        return None
    path = RUNS_DIR / f".new-activity-{token}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        valid = (
            secrets.compare_digest(str(payload.get("token", "")), token)
            and payload.get("archived_run") == Path(archived_run).name
            and payload.get("activity_name") == activity_name
            and datetime.now().astimezone() <= expires_at
            and secrets.compare_digest(
                str(payload.get("archive_receipt_hash", "")),
                _file_hash(Path(archived_run) / "archive-receipt.json"),
            )
            and bool(_archive_receipt(archived_run))
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None
    if not valid:
        return None
    path.unlink()
    mailer._fsync_parent(path)
    return payload


def _sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(run_dir, **values):
    path = Path(run_dir) / "manifest.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(values)
    tmp = path.with_suffix(f".{secrets.token_hex(6)}.tmp")
    with tmp.open("x", encoding="utf-8") as f:
        f.write(json.dumps(current, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    mailer._fsync_parent(path)


def _write_once_json(path, payload):
    path = Path(path)
    with path.open("x", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    mailer._fsync_parent(path)


def _atomic_write_json(path, payload):
    path = Path(path)
    tmp = path.with_suffix(f".{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        mailer._fsync_parent(path)
    finally:
        tmp.unlink(missing_ok=True)


def _file_hash(path):
    path = Path(path)
    return _sha256_path(path) if path.exists() else ""


def _bounce_scan_path(run_dir):
    return Path(run_dir) / "bounce-reconciliation.json"


def _valid_bounce_scan(run_dir, df):
    path = _bounce_scan_path(run_dir)
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        sendable = Path(run_dir) / "sendable.csv"
        checks = (
            secrets.compare_digest(
                str(report.get("batch_id", "")), str(df["batch_id"].iloc[0])
            ),
            secrets.compare_digest(
                str(report.get("content_hash", "")), _content_hash(df)
            ),
            secrets.compare_digest(
                str(report.get("production_ledger_hash", "")),
                _file_hash(mailer._default_ledger_path(str(sendable))),
            ),
            secrets.compare_digest(
                str(report.get("lifecycle_ledger_hash", "")),
                _file_hash(mailer._default_lifecycle_path(str(sendable))),
            ),
        )
        if not all(checks):
            return None
    except (OSError, KeyError, IndexError, json.JSONDecodeError):
        return None
    return report


def _write_hash_sidecar(run_dir, name, value):
    path = Path(run_dir) / name
    with path.open("x", encoding="ascii") as f:
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    mailer._fsync_parent(path)


def _write_recipient_index(run_dir, df):
    path = Path(run_dir) / "recipient-index.json"
    with path.open("x", encoding="utf-8") as f:
        json.dump(_recipient_hashes(df), f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    mailer._fsync_parent(path)


def _ensure_production_marker(run_dir, df, smtp_identity=None):
    path = Path(run_dir) / "production.started"
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise mailer.LedgerIntegrityError("正式发送启动标记已损坏。") from e
        if not secrets.compare_digest(
            str(stored.get("content_hash", "")), _content_hash(df)
        ):
            raise mailer.LedgerIntegrityError("正式发送启动标记与当前批次不一致。")
        if smtp_identity is not None and stored.get("smtp_identity") != smtp_identity:
            raise mailer.LedgerIntegrityError("正式发送身份与首次启动记录不一致。")
        return stored
    payload = {
        "content_hash": _content_hash(df),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "smtp_identity": smtp_identity,
    }
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        return _ensure_production_marker(run_dir, df, smtp_identity)
    mailer._fsync_parent(path)
    return payload


def _remove_run(run_dir):
    run_dir = Path(run_dir)
    shutil.rmtree(run_dir)
    mailer._fsync_parent(run_dir)


def _find_source_batches(source_hash, exclude=None):
    """Find every run made from the same bytes, even if its manifest is damaged."""
    RUNS_DIR.mkdir(exist_ok=True)
    matches = []
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        candidate_hashes = []
        sidecar = run / "source.sha256"
        if sidecar.exists():
            try:
                candidate_hashes.append(sidecar.read_text(encoding="ascii").strip())
            except OSError:
                pass
        for source in (run / "recipients.csv", run / "recipients.xlsx"):
            if source.exists():
                try:
                    candidate_hashes.append(_sha256_path(source))
                except OSError:
                    pass
        manifest = run / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                candidate_hashes.append(str(data.get("source_hash", "")))
            except (OSError, json.JSONDecodeError):
                pass
        if any(value and secrets.compare_digest(value, source_hash) for value in candidate_hashes):
            matches.append(run)
    return matches


def _find_recipient_batches(recipient_set_hash, exclude=None):
    RUNS_DIR.mkdir(exist_ok=True)
    matches = []
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        candidate = ""
        sidecar = run / "recipients.sha256"
        if sidecar.exists():
            try:
                candidate = sidecar.read_text(encoding="ascii").strip()
            except OSError:
                candidate = ""
        if not candidate:
            try:
                manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
                candidate = str(manifest.get("recipient_set_hash", ""))
            except (OSError, json.JSONDecodeError):
                candidate = ""
        if not candidate and (run / "sendable.csv").exists():
            try:
                candidate = _recipient_set_hash(_read_sendable(run / "sendable.csv"))
            except (OSError, KeyError):
                candidate = ""
        if candidate and secrets.compare_digest(candidate, recipient_set_hash):
            matches.append(run)
    return matches


def _run_recipient_hashes(run):
    run = Path(run)
    index = run / "recipient-index.json"
    if index.exists():
        try:
            values = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise mailer.LedgerIntegrityError(
                f"批次 {run.name} 的收件人身份索引已损坏。"
            ) from e
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise mailer.LedgerIntegrityError(
                f"批次 {run.name} 的收件人身份索引无效。"
            )
        return set(values)
    sendable = run / "sendable.csv"
    if sendable.exists():
        try:
            return set(_recipient_hashes(_read_sendable(sendable)))
        except (OSError, KeyError) as e:
            raise mailer.LedgerIntegrityError(
                f"无法读取批次 {run.name} 的收件人身份。"
            ) from e
    raise mailer.LedgerIntegrityError(
        f"已启动批次 {run.name} 缺少可恢复的收件人身份索引。"
    )


def _find_overlapping_started_batch(df, exclude=None):
    current = set(_recipient_hashes(df))
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        if _production_started(run) and current.intersection(_run_recipient_hashes(run)):
            return run
    return None


def _find_overlapping_archived_batches(df, exclude=None):
    current = set(_recipient_hashes(df))
    matches = []
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        if _archive_receipt(run) and current.intersection(_run_recipient_hashes(run)):
            matches.append(run)
    return sorted(matches, key=lambda path: path.name)


def _find_overlapping_archived_batch(df, exclude=None):
    matches = _find_overlapping_archived_batches(df, exclude=exclude)
    return matches[0] if matches else None


def _has_production_evidence(run):
    sendable = Path(run) / "sendable.csv"
    ledger = Path(mailer._default_ledger_path(str(sendable)))
    anchor = Path(mailer._ledger_anchor_path(str(ledger)))
    if ledger.exists() or anchor.exists() or (Path(run) / "production.started").exists():
        return True
    try:
        manifest = json.loads((Path(run) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return manifest.get("production_started") is True


def _archive_receipt(run):
    run = Path(run)
    path = run / "archive-receipt.json"
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        sendable = run / "sendable.csv"
        df = _read_sendable(sendable)
        mailer._validate_reviewed_batch(df)
        mailer._apply_ledger(df, mailer._default_ledger_path(str(sendable)))
        lifecycle_events = mailer._read_lifecycle(str(sendable))
        archived_event = lifecycle_events[-1] if lifecycle_events else {}
        checks = (
            secrets.compare_digest(
                str(receipt.get("batch_id", "")), str(df["batch_id"].iloc[0])
            ),
            secrets.compare_digest(
                str(receipt.get("content_hash", "")), _content_hash(df)
            ),
            secrets.compare_digest(
                str(receipt.get("production_ledger_hash", "")),
                _file_hash(mailer._default_ledger_path(str(sendable))),
            ),
            secrets.compare_digest(
                str(receipt.get("manifest_hash", "")),
                _file_hash(run / "manifest.json"),
            ),
            secrets.compare_digest(
                str(receipt.get("bounce_report_hash", "")),
                _file_hash(_bounce_scan_path(run)),
            ),
            archived_event.get("event") == "archived",
            secrets.compare_digest(
                str(archived_event.get("receipt_hash", "")), _file_hash(path)
            ),
        )
        if not all(checks) or mailer._lifecycle_phase(str(sendable)) != "ARCHIVED":
            return None
    except (
        OSError,
        KeyError,
        IndexError,
        json.JSONDecodeError,
        mailer.LedgerIntegrityError,
    ):
        return None
    return receipt


def _production_started(run):
    if _archive_receipt(run):
        return False
    return _has_production_evidence(run)


def _find_existing_preview(batch_fingerprint, exclude=None):
    RUNS_DIR.mkdir(exist_ok=True)
    for run in RUNS_DIR.glob("mailpilot_*"):
        if exclude and run == exclude:
            continue
        manifest = run / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            candidate = str(data.get("batch_fingerprint", ""))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate and secrets.compare_digest(candidate, batch_fingerprint):
            return run
    return None


def _run_from_name(name):
    if not name.startswith("mailpilot_") or Path(name).name != name:
        return None
    try:
        run = (RUNS_DIR / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return run if run.parent == RUNS_DIR.resolve() else None


def _run_summaries():
    RUNS_DIR.mkdir(exist_ok=True)
    summaries = []
    for run in sorted(RUNS_DIR.glob("mailpilot_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        sendable = run / "sendable.csv"
        if not sendable.exists():
            continue
        try:
            df = _read_sendable(sendable)
            mailer._apply_ledger(df, mailer._default_ledger_path(str(sendable)))
            metrics = _metric_counts(df)
            status_counts = _status_counts(df)
            archived = bool(_archive_receipt(run))
            phase = mailer._lifecycle_phase(str(sendable))
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            activity_name = str(manifest.get("activity_name", "")).strip()
        except Exception:
            metrics = {"ready": 0, "skipped": 0, "blocked": "review required"}
            status_counts = _status_counts(pd.DataFrame())
            archived = False
            phase = "REVIEW_REQUIRED"
            activity_name = ""
        summaries.append(
            {
                "name": run.name,
                "updated": datetime.fromtimestamp(run.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "metrics": metrics,
                "status_counts": status_counts,
                "archived": archived,
                "phase": phase,
                "phase_label": _phase_label(phase),
                "activity_name": activity_name or "未命名活动",
                "url": f"/runs/{run.name}",
            }
        )
    return summaries


def _render(**kwargs):
    defaults = {
        "css": CSS,
        "templates": _template_names(),
        "template_files": _template_files(),
        "message": "",
        "message_kind": "",
        "preview_output": "",
        "send_output": "",
        "sendable_path": "",
        "run_dir": "",
        "action_token": "",
        "runs": _run_summaries(),
        "metrics": {"ready": 0, "skipped": 0, "blocked": 0},
        "status_counts": _status_counts(pd.DataFrame()),
        "lifecycle_phase": "PREVIEW",
        "is_archived": False,
        "production_started": False,
        "bounce_reconciliation": None,
        "batch_short_id": "",
        "activity_token": "",
        "activity_from": "",
        "activity_name": "",
        "run_name": "",
        "table_html": "",
        "attention_rows": [],
        "phase_label": _phase_label("PREVIEW"),
    }
    defaults.update(kwargs)
    run_dir = defaults.get("run_dir")
    sendable_path = defaults.get("sendable_path")
    if run_dir and sendable_path and Path(sendable_path).exists():
        try:
            defaults["run_name"] = Path(run_dir).name
            state_df = _read_sendable(sendable_path)
            mailer._apply_ledger(
                state_df, mailer._default_ledger_path(str(sendable_path))
            )
            defaults["status_counts"] = _status_counts(state_df)
            defaults["attention_rows"] = _attention_rows(state_df)
            defaults["batch_short_id"] = str(state_df["batch_id"].iloc[0])[:8]
            defaults["lifecycle_phase"] = mailer._lifecycle_phase(
                str(sendable_path)
            )
            defaults["phase_label"] = _phase_label(defaults["lifecycle_phase"])
            manifest = json.loads(
                (Path(run_dir) / "manifest.json").read_text(encoding="utf-8")
            )
            defaults["activity_name"] = str(
                manifest.get("activity_name", defaults.get("activity_name", ""))
            )
            defaults["is_archived"] = bool(_archive_receipt(run_dir))
            defaults["production_started"] = _has_production_evidence(run_dir)
            defaults["bounce_reconciliation"] = _valid_bounce_scan(
                run_dir, state_df
            )
        except (OSError, json.JSONDecodeError, mailer.LedgerIntegrityError):
            pass
    return render_template_string(PAGE, **defaults)


@app.get("/")
def index():
    return _render()


@app.get("/runs/<run_name>")
def resume_run(run_name):
    run_dir = _run_from_name(run_name)
    if not run_dir:
        return redirect(url_for("index"))
    sendable_path = run_dir / "sendable.csv"
    if not sendable_path.exists():
        return _render(
            message=(
                "恢复已阻断：批次清单存在，但 sendable.csv 缺失。"
                "在原发送历史完成对账前，不要创建替代批次。"
            ),
            message_kind="error",
        )
    try:
        df = _read_sendable(sendable_path)
        mailer._apply_ledger(df, mailer._default_ledger_path(str(sendable_path)))
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise mailer.LedgerIntegrityError("批次清单缺失。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not secrets.compare_digest(
            str(manifest.get("content_hash", "")), _content_hash(df)
        ):
            raise mailer.LedgerIntegrityError(
                "已审核的收件人/内容清单与当前批次不再一致。"
            )
    except (OSError, json.JSONDecodeError, mailer.LedgerIntegrityError) as e:
        return _render(
            message=f"为防止重复发送，恢复已被阻断：{e}",
            message_kind="error",
            preview_output=(run_dir / "preview.txt").read_text(encoding="utf-8")
            if (run_dir / "preview.txt").exists()
            else "",
            metrics={"ready": 0, "skipped": 0, "blocked": len(df) if "df" in locals() else 0},
            table_html=_table_html(df) if "df" in locals() else "",
        )

    return _render(
        message="已安全打开原批次及其检查点台账；已接受或已停止的行不会再次发送。",
        message_kind="success",
        preview_output=(run_dir / "preview.txt").read_text(encoding="utf-8")
        if (run_dir / "preview.txt").exists()
        else "",
        sendable_path=str(sendable_path),
        run_dir=str(run_dir),
        action_token=_new_action_token(run_dir),
        metrics=_metric_counts(df),
        table_html=_table_html(df),
    )


@app.get("/runs/<run_name>/audit.json")
def download_audit(run_name):
    run_dir = _run_from_name(run_name)
    if not run_dir:
        return redirect(url_for("index"))
    sendable = run_dir / "sendable.csv"
    try:
        df, manifest = _load_locked_batch(sendable, run_dir)
        ledger = mailer._default_ledger_path(str(sendable))
        audit = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run": run_name,
            "batch_id": str(df["batch_id"].iloc[0]),
            "phase": mailer._lifecycle_phase(str(sendable)),
            "archived": bool(_archive_receipt(run_dir)),
            "status_counts": _status_counts(df),
            "manifest": {
                key: value
                for key, value in manifest.items()
                if key not in {"password", "smtp_password"}
            },
            "production_ledger_hash": _file_hash(ledger),
            "lifecycle_ledger_hash": _file_hash(
                mailer._default_lifecycle_path(str(sendable))
            ),
            "lifecycle_events": mailer._read_lifecycle(str(sendable)),
            "production_events": list(mailer._iter_ledger(ledger) or []),
            "bounce_reconciliation": json.loads(
                _bounce_scan_path(run_dir).read_text(encoding="utf-8")
            )
            if _bounce_scan_path(run_dir).exists()
            else None,
            "archive_receipt": _archive_receipt(run_dir),
            "rows": df.fillna("").to_dict(orient="records"),
        }
    except (
        OSError,
        KeyError,
        IndexError,
        json.JSONDecodeError,
        mailer.LedgerIntegrityError,
    ) as e:
        return _render(message=f"审计报告生成失败：{e}", message_kind="error")
    return Response(
        json.dumps(audit, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run_name}-audit.json"'},
    )


@app.get("/runs/<run_name>/status.csv")
def download_status(run_name):
    run_dir = _run_from_name(run_name)
    if not run_dir:
        return redirect(url_for("index"))
    sendable = run_dir / "sendable.csv"
    try:
        df, _manifest = _load_locked_batch(sendable, run_dir)
    except (
        OSError,
        json.JSONDecodeError,
        mailer.LedgerIntegrityError,
    ) as e:
        return _render(message=f"状态报告生成失败：{e}", message_kind="error")
    return Response(
        "\ufeff" + df.to_csv(index=False, lineterminator="\n"),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_name}-status.csv"'},
    )


@app.get("/runs/<run_name>/bounces.json")
def download_bounces(run_name):
    run_dir = _run_from_name(run_name)
    if not run_dir:
        return redirect(url_for("index"))
    sendable = run_dir / "sendable.csv"
    report_path = _bounce_scan_path(run_dir)
    try:
        df, _manifest = _load_locked_batch(sendable, run_dir)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not secrets.compare_digest(
            str(report.get("batch_id", "")), str(df["batch_id"].iloc[0])
        ):
            raise mailer.LedgerIntegrityError("退信报告属于另一个批次。")
    except (
        OSError,
        KeyError,
        IndexError,
        json.JSONDecodeError,
        mailer.LedgerIntegrityError,
    ) as e:
        return _render(message=f"退信报告生成失败：{e}", message_kind="error")
    return Response(
        json.dumps(report, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{run_name}-bounces.json"'
        },
    )


@app.post("/columns")
def detect_columns():
    uploaded = request.files.get("recipients")
    if not uploaded or not uploaded.filename:
        return jsonify(error="请选择 CSV 或 XLSX 名单。"), 400
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        return jsonify(error="只支持 CSV 和 XLSX 文件。"), 400
    try:
        if suffix == ".csv":
            frame = pd.read_csv(uploaded.stream, dtype=object, nrows=0)
        else:
            frame = pd.read_excel(uploaded.stream, dtype=object, nrows=0)
    except Exception as e:
        return jsonify(error=f"无法读取表头：{e}"), 400
    columns = [str(column) for column in frame.columns]
    return jsonify(
        columns=columns,
        suggestions={
            "email": mailer._auto_col_candidates(frame, mailer.EMAIL_ALIASES),
            "name": mailer._auto_col_candidates(frame, mailer.NAME_ALIASES),
            "group": mailer._auto_col_candidates(frame, mailer.GROUP_ALIASES),
            "sent": mailer._find_exact_cols(frame, mailer.SENT_ALIASES),
        },
    )


@app.post("/preview")
def preview():
    uploaded = request.files.get("recipients")
    if not uploaded or not uploaded.filename:
        return _render(message="请上传 CSV 或 XLSX 名单。", message_kind="error")

    suffix = Path(uploaded.filename).suffix.lower() or ".csv"
    if suffix not in (".csv", ".xlsx"):
        return _render(message="只支持 CSV 和 XLSX 文件。", message_kind="error")
    activity_name = request.form.get("activity_name", "").strip()
    if (
        not 2 <= len(activity_name) <= 60
        or any(char in activity_name for char in "\r\n\t/\\")
    ):
        return _render(
            message="请填写 2–60 个字符的活动名称（不能包含路径或换行符）。",
            message_kind="error",
        )
    activity_token = request.form.get("activity_token", "").strip()
    activity_from_name = request.form.get("activity_from", "").strip()
    mode = request.form.get("mode")
    group_col = request.form.get("group_col", "").strip() or None
    if mode == "simple":
        group_col = "__none__"
    elif not group_col:
        return _render(
            message="分组模式必须明确填写业务已确认的分类列；MailPilot 不会擅自猜测。",
            message_kind="error",
        )

    sent_col = request.form.get("sent_col", "").strip()
    if not sent_col and not request.form.get("confirm_no_history"):
        return _render(
            message="请选择可信的历史已发送列，或者明确确认本活动此前无人手工发送。",
            message_kind="error",
        )

    RUNS_DIR.mkdir(exist_ok=True)
    mailer._fsync_parent(RUNS_DIR)
    run_dir = Path(tempfile.mkdtemp(prefix="mailpilot_", dir=RUNS_DIR))
    mailer._fsync_parent(run_dir)
    source_path = run_dir / f"recipients{suffix}"
    uploaded.save(source_path)
    source_hash = _sha256_path(source_path)
    _write_hash_sidecar(run_dir, "source.sha256", source_hash)
    out_path = run_dir / "sendable.csv"

    args = Namespace(
        file=str(source_path),
        template=request.form.get("template") or "default",
        out=str(out_path),
        email_col=request.form.get("email_col") or None,
        name_col=request.form.get("name_col") or None,
        group_col=group_col,
        sent_col=sent_col or "__none__",
    )
    ok, output = _capture(mailer.cmd_preview, args)
    df = _read_sendable(out_path)
    if not ok:
        _remove_run(run_dir)
        return _render(
            message="预览失败；刚上传的名单副本已自动删除。",
            message_kind="error",
            preview_output=output,
        )

    batch_fingerprint = _batch_fingerprint(df)
    recipient_set_hash = _recipient_set_hash(df)
    try:
        with mailer.BatchLock(str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True):
            existing = _find_existing_preview(batch_fingerprint, exclude=run_dir)
            same_sources = _find_source_batches(source_hash, exclude=run_dir)
            equivalent_recipients = _find_recipient_batches(
                recipient_set_hash, exclude=run_dir
            )
            overlapping_started = _find_overlapping_started_batch(df, exclude=run_dir)
            if overlapping_started:
                _remove_run(run_dir)
                return redirect(
                    url_for("resume_run", run_name=overlapping_started.name)
                )
            overlapping_archives = _find_overlapping_archived_batches(
                df, exclude=run_dir
            )
            overlapping_archived = overlapping_archives[0] if overlapping_archives else None
            authorized_archive = None
            if overlapping_archived:
                requested_archive = _run_from_name(activity_from_name)
                authorization = (
                    _consume_activity_authorization(
                        activity_token, requested_archive, activity_name
                    )
                    if requested_archive in overlapping_archives
                    else None
                )
                if not authorization:
                    _remove_run(run_dir)
                    return redirect(
                        url_for("resume_run", run_name=overlapping_archived.name)
                    )
                authorized_archive = overlapping_archived
            elif activity_token:
                _remove_run(run_dir)
                return _render(
                    message="新活动授权与上传名单不匹配，未创建批次。",
                    message_kind="error",
                )
            if existing and existing != authorized_archive:
                _remove_run(run_dir)
                return redirect(url_for("resume_run", run_name=existing.name))
            for prior_run in set(same_sources + equivalent_recipients):
                if prior_run == authorized_archive:
                    continue
                if _production_started(prior_run):
                    _remove_run(run_dir)
                    return redirect(url_for("resume_run", run_name=prior_run.name))
            for same_source in same_sources:
                if same_source == authorized_archive:
                    continue
                if _archive_receipt(same_source):
                    _remove_run(run_dir)
                    return redirect(url_for("resume_run", run_name=same_source.name))
                try:
                    old_manifest = json.loads(
                        (same_source / "manifest.json").read_text(encoding="utf-8")
                    )
                    old_fingerprint = str(old_manifest.get("batch_fingerprint", ""))
                except (OSError, json.JSONDecodeError):
                    old_fingerprint = ""
                if not old_fingerprint or not (same_source / "sendable.csv").exists():
                    _remove_run(run_dir)
                    return redirect(url_for("resume_run", run_name=same_source.name))
            for same_source in same_sources:
                if same_source != authorized_archive:
                    _remove_run(same_source)
            activity_id = secrets.token_hex(12)
            if authorized_archive:
                df, archived_suppressed = _apply_archived_suppression(
                    df, overlapping_archives
                )
                if archived_suppressed:
                    output += (
                        f"\n\nBlocked by archived bounce/unsubscribe history: "
                        f"{archived_suppressed} row(s)."
                    )
            df = _assign_activity(df, activity_id)
            mailer._atomic_write_csv(df, str(out_path))
            _write_hash_sidecar(run_dir, "recipients.sha256", recipient_set_hash)
            _write_recipient_index(run_dir, df)
            (run_dir / "preview.txt").write_text(output, encoding="utf-8")
            _write_manifest(
                run_dir,
                original_filename=uploaded.filename,
                source_hash=source_hash,
                content_hash=_content_hash(df),
                batch_fingerprint=batch_fingerprint,
                recipient_set_hash=recipient_set_hash,
                activity_id=activity_id,
                activity_name=activity_name,
                predecessor_run=authorized_archive.name if authorized_archive else "",
                created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
    except mailer.BatchAlreadyLocked:
        _remove_run(run_dir)
        return _render(
            message="另一个预览正在完成安全校验，请稍后重试。",
            message_kind="error",
        )
    except (OSError, mailer.LedgerIntegrityError) as e:
        _remove_run(run_dir)
        return _render(
            message=f"预览在最终安全校验时被阻断：{e}",
            message_kind="error",
        )
    action_token = _new_action_token(run_dir)
    return _render(
        message="预览完成；没有发送任何邮件。",
        message_kind="success",
        preview_output=output,
        sendable_path=str(out_path),
        run_dir=str(run_dir),
        action_token=action_token,
        metrics=_metric_counts(df),
        table_html=_table_html(df),
    )


@app.post("/send")
def send():
    sendable_path, run_dir = _safe_paths(
        request.form.get("sendable_path", ""), request.form.get("run_dir", "")
    )
    if not sendable_path or not run_dir:
        return redirect(url_for("index"))

    next_action_token = _consume_action_token(run_dir, request.form.get("action_token", ""))
    if not next_action_token:
        return _render(
            message="该操作已使用或批次正忙，请重新打开批次后再试。",
            message_kind="error",
        )

    action = request.form.get("action")
    df = _read_sendable(sendable_path)
    metrics = _metric_counts(df)

    def render_error(message):
        return _render(
            message=message,
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_action_token,
            metrics=metrics,
            table_html=_table_html(df),
        )

    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return render_error("批次清单缺失或损坏，测试与正式发送均已阻断。")
    if not secrets.compare_digest(str(manifest.get("content_hash", "")), _content_hash(df)):
        return render_error("已审核的收件人或邮件内容发生变化，请重新预览。")

    if action not in ("dry", "test", "send"):
        return render_error("未知操作；没有发送任何邮件。")
    try:
        limit = int(request.form.get("limit", ""))
    except (TypeError, ValueError):
        return render_error("本次封数必须是 1 到 100 的整数。")
    if not 1 <= limit <= 100:
        return render_error("本次封数必须在 1 到 100 之间；0 绝不表示不限量。")

    redirect_to = request.form.get("redirect_to", "").strip() or None
    if action == "test" and not redirect_to:
        return render_error("测试模式必须填写重定向邮箱，而且绝不能使用客户地址。")
    test_templates = []
    if action == "test":
        normalized_redirect = (mailer._valid_email(redirect_to) or "").lower()
        allowed_test_inboxes = {
            str(request.form.get("user", "")).strip().lower(),
            str(request.form.get("from_addr", "")).strip().lower(),
        }
        customer_inboxes = set(df["email"].astype(str).str.strip().str.lower())
        if not normalized_redirect or normalized_redirect not in allowed_test_inboxes:
            return render_error(
                "为防误发，测试邮箱必须与 SMTP 账号或发件地址完全一致。"
            )
        if normalized_redirect in customer_inboxes:
            return render_error(
                "测试邮箱也出现在客户名单中；请从名单移除它，或改用独立的发件测试邮箱。"
            )
        test_templates = _pending_templates(df)
        if not test_templates:
            return render_error("当前没有待测试的模板。")
        if len(test_templates) > 20:
            return render_error("待审核模板超过 20 个，请先拆分或简化批次。")
    if action == "send":
        if metrics["blocked"]:
            return render_error(
                f"仍有 {metrics['blocked']} 行数据不安全，正式发送已阻断。请修正源名单并重新预览。"
            )
        if not request.form.get("confirm_real_send"):
            return render_error("正式发送前必须勾选明确确认。")
        if not request.form.get("confirm_test_received"):
            return render_error("请先确认已经收到并核对重定向测试邮件。")
        expected_phrase = f"发送 {limit} 封"
        if request.form.get("confirm_phrase", "").strip() != expected_phrase:
            return render_error(f"请输入准确短语“{expected_phrase}”来授权本次正式发送。")
        marker = _read_test_marker(run_dir)
        pending_templates = set(_pending_templates(df))
        try:
            marker_matches = (
                marker
                and secrets.compare_digest(
                    str(marker.get("content_hash", "")), _content_hash(df)
                )
                and pending_templates.issubset(set(marker.get("templates", [])))
                and marker.get("smtp_identity") == _smtp_identity(request.form)
            )
        except (TypeError, ValueError):
            marker_matches = False
        if not marker_matches:
            return render_error("正式发送前，必须先为当前预览成功完成重定向测试。")

    try:
        sleep_seconds = float(request.form.get("sleep", "2") or 2)
    except ValueError:
        return render_error("邮件间隔秒数必须是数字。")
    if action == "send" and sleep_seconds < 1:
        return render_error("正式发送时，每封邮件之间至少等待 1 秒。")

    config_data = None
    if action != "dry":
        try:
            config_data = _config_from_form(request.form)
        except (OSError, TypeError, ValueError, SystemExit) as e:
            return render_error(f"SMTP 设置无效：{e}")

    if action == "send":
        try:
            with mailer.BatchLock(
                str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True
            ):
                with mailer.BatchLock(f"{sendable_path}.lock"):
                    if not run_dir.exists() or not sendable_path.exists():
                        return _render(
                            message=(
                                "正式发送开始前，该预览已被替换；没有发送任何邮件。"
                                "请打开当前批次重新核对。"
                            ),
                            message_kind="error",
                        )
                    locked_df = _read_sendable(sendable_path)
                    mailer._validate_reviewed_batch(locked_df)
                    locked_manifest = json.loads(
                        (run_dir / "manifest.json").read_text(encoding="utf-8")
                    )
                    if not secrets.compare_digest(
                        str(locked_manifest.get("content_hash", "")),
                        _content_hash(locked_df),
                    ):
                        raise mailer.LedgerIntegrityError(
                            "正式发送开始前，已审核批次发生变化。"
                        )
                    overlapping = _find_overlapping_started_batch(
                        locked_df, exclude=run_dir
                    )
                    if overlapping:
                        return render_error(
                            "收件人与已启动批次重叠，正式发送已阻断："
                            f"{overlapping.name}。请先打开并对账该批次。"
                        )
                    batch_id = str(locked_df["batch_id"].iloc[0])
                    phase = mailer._lifecycle_phase(str(sendable_path))
                    if phase == "PREVIEW" and _has_production_evidence(run_dir):
                        raise mailer.LedgerIntegrityError(
                            "本地生产标记证明该批次曾开始发送，但两套审计台账均缺失。"
                            "在人工对账前绝不能重新发送。"
                        )
                    mailer._ensure_lifecycle_open(str(sendable_path), batch_id)
                    _ensure_production_marker(
                        run_dir, locked_df, _smtp_identity(request.form)
                    )
                    _write_manifest(
                        run_dir,
                        production_started=True,
                        production_started_at=datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                    )
                    df = locked_df
        except (
            OSError,
            json.JSONDecodeError,
            mailer.BatchAlreadyLocked,
            mailer.LedgerIntegrityError,
        ) as e:
            return render_error(f"正式发送启动被安全阻断：{e}")

    try:
        test_ledger = Path(mailer._default_test_ledger_path(str(sendable_path)))
        before_test_events = list(mailer._iter_ledger(str(test_ledger)) or [])
        if action == "test":
            outputs = []
            ok = True
            for template_name in test_templates:
                args = Namespace(
                    input=str(sendable_path),
                    config="",
                    config_data=config_data,
                    only=template_name,
                    redirect_to=redirect_to,
                    limit=1,
                    sleep=0,
                    at=None,
                    dry_run=False,
                    ledger=None,
                )
                run_ok, run_output = _capture(mailer.cmd_send, args)
                outputs.append(f"=== Template: {template_name} ===\n{run_output}")
                ok = ok and run_ok
            output = "\n\n".join(outputs)
            after_test_events = list(mailer._iter_ledger(str(test_ledger)) or [])
            new_events = after_test_events[len(before_test_events):]
            accepted_templates = {
                str(event.get("template", ""))
                for event in new_events
                if event.get("status") == "test_sent"
            }
            if ok and accepted_templates == set(test_templates):
                _write_test_marker(run_dir, df, test_templates, request.form)
            else:
                ok = False
                missing = sorted(set(test_templates).difference(accepted_templates))
                output = f"{output}\nTemplates without an accepted redirected test: {missing}".strip()
        else:
            args = Namespace(
                input=str(sendable_path),
                config="",
                config_data=config_data,
                only=request.form.get("only") or None,
                redirect_to=None,
                limit=limit,
                sleep=sleep_seconds,
                at=None,
                dry_run=action == "dry",
                ledger=None,
            )
            ok, output = _capture(mailer.cmd_send, args)
    except mailer.LedgerIntegrityError as e:
        ok, output = False, f"测试台账完整性错误：{e}"

    df = _read_sendable(str(sendable_path))
    command_has_errors = "unknown" in output.lower() or "✗" in output
    succeeded = ok and not command_has_errors
    return _render(
        message="操作已完成。" if succeeded else "操作已停止，或仍需人工检查。",
        message_kind="success" if succeeded else "error",
        send_output=output,
        sendable_path=str(sendable_path),
        run_dir=str(run_dir),
        action_token=next_action_token,
        metrics=_metric_counts(df),
        table_html=_table_html(df),
    )


def _load_locked_batch(sendable_path, run_dir):
    df = _read_sendable(sendable_path)
    mailer._validate_reviewed_batch(df)
    mailer._apply_ledger(df, mailer._default_ledger_path(str(sendable_path)))
    manifest = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    if not secrets.compare_digest(
        str(manifest.get("content_hash", "")), _content_hash(df)
    ):
        raise mailer.LedgerIntegrityError("批次内容与其清单不再一致。")
    return df, manifest


def _append_manual_status(sendable_path, df, idx, status, evidence, event_name):
    ledger = mailer._default_ledger_path(str(sendable_path))
    batch_id = str(df.at[idx, "batch_id"])
    mailer._ensure_ledger_anchor(ledger, batch_id)
    mailer._append_ledger(
        ledger,
        {
            "event": "manual_resolution",
            "row": int(idx),
            "batch_id": batch_id,
            "record_id": str(df.at[idx, "record_id"]),
            "email": str(df.at[idx, "email"]).strip().lower(),
            "status": status,
            "send_error": evidence,
            "send_time": str(df.at[idx, "send_time"])
            if "send_time" in df.columns
            else "",
            "resolution": event_name,
        },
    )
    df.at[idx, "status"] = status
    df.at[idx, "send_error"] = evidence


@app.post("/lifecycle")
def lifecycle_action():
    sendable_path, run_dir = _safe_paths(
        request.form.get("sendable_path", ""), request.form.get("run_dir", "")
    )
    if not sendable_path or not run_dir:
        return redirect(url_for("index"))
    next_token = _consume_action_token(run_dir, request.form.get("action_token", ""))
    if not next_token:
        return _render(
            message="操作已失效或批次正忙。请重新打开该批次。",
            message_kind="error",
        )
    action = request.form.get("lifecycle_action", "")
    try:
        with mailer.BatchLock(
            str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True
        ):
            with mailer.BatchLock(f"{sendable_path}.lock"):
                df, _manifest = _load_locked_batch(sendable_path, run_dir)
                if _archive_receipt(run_dir):
                    raise mailer.LedgerIntegrityError("该批次已归档，不能再修改。")
                phase = mailer._lifecycle_phase(str(sendable_path))
                batch_id = str(df["batch_id"].iloc[0])
                short_id = batch_id[:8]

                if action in {
                    "unknown_sent",
                    "unknown_suppress",
                    "error_retry",
                    "error_suppress",
                }:
                    email_address = request.form.get("resolve_email", "").strip().lower()
                    matches = df.index[
                        df["email"].astype(str).str.strip().str.lower().eq(email_address)
                    ].tolist()
                    if len(matches) != 1:
                        raise mailer.LedgerIntegrityError(
                            "必须填写本批次中唯一、完整的收件邮箱。"
                        )
                    idx = matches[0]
                    current = str(df.at[idx, "status"]).strip().lower()
                    evidence = request.form.get("evidence", "").strip()
                    if len(evidence) < 8:
                        raise mailer.LedgerIntegrityError("请填写至少 8 个字的核对依据。")
                    rules = {
                        "unknown_sent": (
                            "unknown",
                            "sent",
                            f"确认已接受 {email_address}",
                        ),
                        "unknown_suppress": (
                            "unknown",
                            "suppressed",
                            f"永久不重发 {email_address}",
                        ),
                        "error_retry": (
                            "error",
                            "retry_authorized",
                            f"重试明确失败 {email_address}",
                        ),
                        "error_suppress": (
                            "error",
                            "suppressed",
                            f"关闭失败 {email_address}",
                        ),
                    }
                    required_status, new_status, expected_phrase = rules[action]
                    if current != required_status:
                        raise mailer.LedgerIntegrityError(
                            f"该地址当前状态不是 {required_status}，请刷新后重试。"
                        )
                    if request.form.get("confirm_phrase", "").strip() != expected_phrase:
                        raise mailer.LedgerIntegrityError(
                            f"请输入准确短语：{expected_phrase}"
                        )
                    _append_manual_status(
                        sendable_path, df, idx, new_status, evidence, action
                    )
                    mailer._atomic_write_csv(df, str(sendable_path))
                    mailer._append_lifecycle(
                        str(sendable_path),
                        {
                            "event": action,
                            "batch_id": batch_id,
                            "row": int(idx),
                            "record_id": str(df.at[idx, "record_id"]),
                            "evidence": evidence,
                        },
                    )

                elif action == "suppress_ready":
                    counts = _status_counts(df)
                    ready_count = counts["ready"]
                    expected_phrase = f"停止剩余 {ready_count} 封"
                    evidence = request.form.get("evidence", "").strip()
                    if ready_count < 1:
                        raise mailer.LedgerIntegrityError("当前没有待发送邮件。")
                    if request.form.get("confirm_phrase", "").strip() != expected_phrase:
                        raise mailer.LedgerIntegrityError(
                            f"请输入准确短语：{expected_phrase}"
                        )
                    if len(evidence) < 8:
                        raise mailer.LedgerIntegrityError("请填写至少 8 个字的停止原因。")
                    for idx, row in df.iterrows():
                        status = str(row.get("status", "")).strip().lower()
                        initial = str(row.get("initial_status", "")).strip().lower()
                        sendable = str(row.get("sendable", "")).strip().lower()
                        if (
                            sendable in ("yes", "true", "1")
                            and status not in mailer.SUPPRESSED_STATUSES
                            and initial not in mailer.SUPPRESSED_STATUSES
                        ):
                            _append_manual_status(
                                sendable_path,
                                df,
                                idx,
                                "suppressed",
                                evidence,
                                "suppress_ready",
                            )
                    mailer._atomic_write_csv(df, str(sendable_path))
                    mailer._append_lifecycle(
                        str(sendable_path),
                        {
                            "event": "remaining_suppressed",
                            "batch_id": batch_id,
                            "count": ready_count,
                            "evidence": evidence,
                        },
                    )

                elif action == "close":
                    if phase != "OUTBOUND_OPEN":
                        raise mailer.LedgerIntegrityError(
                            f"当前阶段为 {phase}，不能关闭外发。"
                        )
                    counts = _status_counts(df)
                    if counts["ready"] or counts["unknown"] or counts["error"]:
                        raise mailer.LedgerIntegrityError(
                            "关闭前必须把待发送、结果不确定和明确失败全部处理为 0。"
                        )
                    if counts["blocked"]:
                        raise mailer.LedgerIntegrityError(
                            "批次仍有数据阻断行，不能归入已完成活动。"
                        )
                    expected_phrase = f"关闭外发 {short_id}"
                    if not request.form.get("confirm_close"):
                        raise mailer.LedgerIntegrityError(
                            "请确认关闭后本批次永远不能继续发信。"
                        )
                    if request.form.get("confirm_phrase", "").strip() != expected_phrase:
                        raise mailer.LedgerIntegrityError(
                            f"请输入准确短语：{expected_phrase}"
                        )
                    mailer._append_lifecycle(
                        str(sendable_path),
                        {
                            "event": "outbound_closed",
                            "batch_id": batch_id,
                            "counts": counts,
                        },
                    )
                    _write_manifest(
                        run_dir,
                        outbound_closed_at=datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                    )
                else:
                    raise mailer.LedgerIntegrityError("未知的生命周期操作。")
    except (
        OSError,
        json.JSONDecodeError,
        mailer.BatchAlreadyLocked,
        mailer.LedgerIntegrityError,
    ) as e:
        return _render(
            message=f"操作被安全阻断：{e}",
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_token,
        )
    return redirect(url_for("resume_run", run_name=run_dir.name))


@app.post("/bounces")
def bounces_action():
    sendable_path, run_dir = _safe_paths(
        request.form.get("sendable_path", ""), request.form.get("run_dir", "")
    )
    if not sendable_path or not run_dir:
        return redirect(url_for("index"))
    next_token = _consume_action_token(run_dir, request.form.get("action_token", ""))
    if not next_token:
        return _render(
            message="退信操作已失效或批次正忙。请重新打开批次。",
            message_kind="error",
        )
    action = request.form.get("bounce_action", "")
    try:
        with mailer.BatchLock(
            str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True
        ):
            with mailer.BatchLock(f"{sendable_path}.lock"):
                df, _manifest = _load_locked_batch(sendable_path, run_dir)
                if _archive_receipt(run_dir):
                    raise mailer.LedgerIntegrityError("归档批次不能再修改退信状态。")
                phase = mailer._lifecycle_phase(str(sendable_path))
                if phase != "OUTBOUND_CLOSED":
                    raise mailer.LedgerIntegrityError(
                        "必须先关闭外发，再扫描和应用退信。"
                    )
                batch_id = str(df["batch_id"].iloc[0])

                if action == "scan":
                    try:
                        lookback = int(request.form.get("lookback", "100") or 100)
                    except ValueError as e:
                        raise mailer.LedgerIntegrityError(
                            "扫描邮件数必须是整数。"
                        ) from e
                    if not 1 <= lookback <= 50000:
                        raise mailer.LedgerIntegrityError(
                            "扫描邮件数必须在 1 到 50000 之间。"
                        )
                    config_data, normalized = _imap_config_from_form(request.form)
                    marker_path = Path(run_dir) / "production.started"
                    try:
                        production_marker = json.loads(
                            marker_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as e:
                        raise mailer.LedgerIntegrityError(
                            "正式发送身份记录缺失或损坏，不能进行可归档的退信扫描。"
                        ) from e
                    production_identity = production_marker.get("smtp_identity")
                    if not isinstance(production_identity, dict):
                        raise mailer.LedgerIntegrityError(
                            "旧批次没有可验证的正式发送身份，不能盲目归档。"
                        )
                    expected_from = str(
                        production_identity.get("from_addr", "")
                    ).strip().lower()
                    expected_user = str(
                        production_identity.get("user", "")
                    ).strip().lower()
                    actual_from = str(normalized.get("from_addr", "")).strip().lower()
                    actual_imap_user = str(normalized.get("user", "")).strip().lower()
                    if not expected_from or actual_from != expected_from:
                        raise mailer.LedgerIntegrityError(
                            f"退信扫描的发件地址必须与正式发送一致：{expected_from}"
                        )
                    if actual_imap_user not in {expected_user, expected_from}:
                        raise mailer.LedgerIntegrityError(
                            "v0.2 仅允许使用正式 SMTP 账号或发件地址对应的 IMAP 邮箱扫描退信。"
                        )
                    args = Namespace(
                        input=str(sendable_path),
                        config="",
                        config_data=config_data,
                        lookback=lookback,
                        ledger=None,
                        apply=False,
                    )
                    ok, output, result = _capture_with_result(
                        mailer._cmd_bounces_locked, args
                    )
                    if not ok or not isinstance(result, dict):
                        raise mailer.LedgerIntegrityError(
                            f"退信扫描失败：{output}"
                        )
                    scan_id = secrets.token_hex(12)
                    mailer._append_lifecycle(
                        str(sendable_path),
                        {
                            "event": "bounce_scan",
                            "batch_id": batch_id,
                            "scan_id": scan_id,
                            "matched": result["matched"],
                            "unverified": result["unverified"],
                            "lookback": lookback,
                            "smtp_user": expected_user,
                            "from_addr": expected_from,
                            "imap_user": actual_imap_user,
                        },
                    )
                    report = {
                        "scan_id": scan_id,
                        "batch_id": batch_id,
                        "content_hash": _content_hash(df),
                        "checked_at": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                        "lookback": lookback,
                        "imap_host": normalized["imap_host"],
                        "imap_user": normalized["user"],
                        "from_addr": result["from_addr"],
                        "production_smtp_identity": production_identity,
                        "identity_verified": True,
                        "found": result["found"],
                        "matched": result["matched"],
                        "unverified": result["unverified"],
                        "applied": 0,
                        "unapplied": result["matched"],
                        "unverified_acknowledged": result["unverified"] == 0,
                        "unverified_note": "",
                        "coverage": result["coverage"],
                        "candidates": result["candidates"],
                        "unverified_candidates": result[
                            "unverified_candidates"
                        ],
                        "production_ledger_hash": _file_hash(
                            mailer._default_ledger_path(str(sendable_path))
                        ),
                        "lifecycle_ledger_hash": _file_hash(
                            mailer._default_lifecycle_path(str(sendable_path))
                        ),
                        "output": output,
                    }
                    _atomic_write_json(_bounce_scan_path(run_dir), report)

                elif action in ("apply", "acknowledge"):
                    report = _valid_bounce_scan(run_dir, df)
                    if not report:
                        raise mailer.LedgerIntegrityError(
                            "退信扫描证据已失效，请重新扫描。"
                        )
                    unverified = int(report.get("unverified", 0))
                    note = request.form.get("unverified_note", "").strip()
                    if unverified:
                        expected_ack = f"确认人工核对 {unverified} 条"
                        if (
                            not request.form.get("ack_unverified")
                            or request.form.get("ack_phrase", "").strip()
                            != expected_ack
                            or len(note) < 8
                        ):
                            raise mailer.LedgerIntegrityError(
                                f"存在无法自动关联的退信；请填写备注并输入：{expected_ack}"
                            )
                    if action == "apply":
                        matched = int(report.get("matched", 0))
                        expected_phrase = f"应用 {matched} 条退信"
                        if matched < 1:
                            raise mailer.LedgerIntegrityError(
                                "本次扫描没有可自动应用的退信。"
                            )
                        if request.form.get("confirm_phrase", "").strip() != expected_phrase:
                            raise mailer.LedgerIntegrityError(
                                f"请输入准确短语：{expected_phrase}"
                            )
                        applied = mailer._apply_bounce_candidates(
                            df,
                            str(sendable_path),
                            mailer._default_ledger_path(str(sendable_path)),
                            report.get("candidates", []),
                            str(report.get("from_addr", "")),
                        )
                        report["applied"] = applied
                        report["unapplied"] = matched - applied
                        event_name = "bounce_applied"
                    else:
                        event_name = "bounce_unverified_acknowledged"
                    if unverified:
                        report["unverified_acknowledged"] = True
                        report["unverified_note"] = note
                    mailer._append_lifecycle(
                        str(sendable_path),
                        {
                            "event": event_name,
                            "batch_id": batch_id,
                            "scan_id": report["scan_id"],
                            "applied": int(report.get("applied", 0)),
                            "unverified": unverified,
                            "note": note,
                        },
                    )
                    report["production_ledger_hash"] = _file_hash(
                        mailer._default_ledger_path(str(sendable_path))
                    )
                    report["lifecycle_ledger_hash"] = _file_hash(
                        mailer._default_lifecycle_path(str(sendable_path))
                    )
                    _atomic_write_json(_bounce_scan_path(run_dir), report)
                else:
                    raise mailer.LedgerIntegrityError("未知退信操作。")
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        mailer.BatchAlreadyLocked,
        mailer.LedgerIntegrityError,
    ) as e:
        return _render(
            message=f"退信操作被安全阻断：{e}",
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_token,
        )
    return redirect(url_for("resume_run", run_name=run_dir.name))


@app.post("/archive")
def archive_run():
    sendable_path, run_dir = _safe_paths(
        request.form.get("sendable_path", ""), request.form.get("run_dir", "")
    )
    if not sendable_path or not run_dir:
        return redirect(url_for("index"))
    next_token = _consume_action_token(run_dir, request.form.get("action_token", ""))
    if not next_token:
        return _render(
            message="归档操作已失效或批次正忙。请重新打开批次。",
            message_kind="error",
        )
    try:
        with mailer.BatchLock(
            str(RUNS_DIR / ".preview-dedupe.lock"), blocking=True
        ):
            with mailer.BatchLock(f"{sendable_path}.lock"):
                existing_receipt = _archive_receipt(run_dir)
                if existing_receipt:
                    return redirect(url_for("resume_run", run_name=run_dir.name))
                df, _manifest = _load_locked_batch(sendable_path, run_dir)
                if mailer._lifecycle_phase(str(sendable_path)) != "OUTBOUND_CLOSED":
                    raise mailer.LedgerIntegrityError(
                        "只有已关闭外发的批次才能归档。"
                    )
                counts = _status_counts(df)
                if (
                    counts["ready"]
                    or counts["unknown"]
                    or counts["error"]
                    or counts["blocked"]
                ):
                    raise mailer.LedgerIntegrityError(
                        "归档要求待发送、结果不确定、明确失败和数据阻断全部为 0。"
                    )
                report = _valid_bounce_scan(run_dir, df)
                if not report:
                    raise mailer.LedgerIntegrityError(
                        "缺少关闭外发后的有效退信扫描。"
                    )
                if int(report.get("unapplied", 0)):
                    raise mailer.LedgerIntegrityError("仍有已验证退信尚未应用。")
                coverage = report.get("coverage", {})
                if not coverage.get("complete") or not coverage.get("uidvalidity"):
                    raise mailer.LedgerIntegrityError(
                        "退信扫描没有完整覆盖活动开始后的邮箱范围，不能归档。"
                    )
                if report.get("identity_verified") is not True:
                    raise mailer.LedgerIntegrityError(
                        "退信扫描没有绑定本活动的正式发送身份，不能归档。"
                    )
                if (
                    int(report.get("unverified", 0))
                    and not report.get("unverified_acknowledged")
                ):
                    raise mailer.LedgerIntegrityError(
                        "仍有无法自动关联的退信尚未人工确认。"
                    )
                batch_id = str(df["batch_id"].iloc[0])
                expected_phrase = f"归档 {batch_id[:8]}"
                if not request.form.get("confirm_waited_bounce_window"):
                    raise mailer.LedgerIntegrityError(
                        "请先按邮箱服务商规则等待退信窗口，并在归档前重新扫描。"
                    )
                if not request.form.get("confirm_delivery_limits"):
                    raise mailer.LedgerIntegrityError(
                        "请确认 SMTP 接受不等于最终送达，晚到退信仍可能出现。"
                    )
                if request.form.get("confirm_phrase", "").strip() != expected_phrase:
                    raise mailer.LedgerIntegrityError(
                        f"请输入准确短语：{expected_phrase}"
                    )
                manifest_path = Path(run_dir) / "manifest.json"
                archived_at = str(_manifest.get("archived_at", ""))
                if not archived_at:
                    archived_at = datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    )
                    _write_manifest(run_dir, archived_at=archived_at)
                receipt = {
                    "batch_id": batch_id,
                    "content_hash": _content_hash(df),
                    "archived_at": archived_at,
                    "counts": counts,
                    "bounce_scan_id": report["scan_id"],
                    "bounce_report_hash": _file_hash(_bounce_scan_path(run_dir)),
                    "production_ledger_hash": _file_hash(
                        mailer._default_ledger_path(str(sendable_path))
                    ),
                    "manifest_hash": _file_hash(manifest_path),
                    "confirmation": expected_phrase,
                    "bounce_window_confirmation": True,
                    "reconciliation_identity": {
                        "production_smtp_identity": report.get(
                            "production_smtp_identity", {}
                        ),
                        "imap_user": report.get("imap_user", ""),
                        "from_addr": report.get("from_addr", ""),
                        "identity_verified": report.get("identity_verified") is True,
                    },
                }
                receipt_path = Path(run_dir) / "archive-receipt.json"
                if receipt_path.exists():
                    try:
                        pending_receipt = json.loads(
                            receipt_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as e:
                        raise mailer.LedgerIntegrityError(
                            "待完成的归档收据已损坏，禁止继续。"
                        ) from e
                    if pending_receipt != receipt:
                        raise mailer.LedgerIntegrityError(
                            "待完成的归档收据与当前批次不一致，禁止继续。"
                        )
                else:
                    _write_once_json(receipt_path, receipt)
                mailer._append_lifecycle(
                    str(sendable_path),
                    {
                        "event": "archived",
                        "batch_id": batch_id,
                        "counts": counts,
                        "bounce_scan_id": report["scan_id"],
                        "receipt_hash": _file_hash(receipt_path),
                        "manifest_hash": receipt["manifest_hash"],
                    },
                )
    except (
        OSError,
        json.JSONDecodeError,
        mailer.BatchAlreadyLocked,
        mailer.LedgerIntegrityError,
    ) as e:
        return _render(
            message=f"归档被安全阻断：{e}",
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_token,
        )
    return redirect(url_for("resume_run", run_name=run_dir.name))


@app.post("/new-activity")
def authorize_new_activity():
    sendable_path, run_dir = _safe_paths(
        request.form.get("sendable_path", ""), request.form.get("run_dir", "")
    )
    if not sendable_path or not run_dir:
        return redirect(url_for("index"))
    next_token = _consume_action_token(run_dir, request.form.get("action_token", ""))
    if not next_token:
        return _render(
            message="新活动授权已失效。请重新打开归档批次。",
            message_kind="error",
        )
    activity_name = request.form.get("activity_name", "").strip()
    if (
        not 2 <= len(activity_name) <= 60
        or any(char in activity_name for char in "\r\n\t/\\")
    ):
        return _render(
            message="活动名称必须为 2–60 个字符，且不能包含路径或换行符。",
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_token,
        )
    expected_phrase = f"新活动 {activity_name}"
    if request.form.get("confirm_phrase", "").strip() != expected_phrase:
        return _render(
            message=f"请输入准确短语：{expected_phrase}",
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_token,
        )
    if not _archive_receipt(run_dir):
        return _render(
            message="只有完整归档且审计收据有效的批次才能授权新活动。",
            message_kind="error",
            sendable_path=str(sendable_path),
            run_dir=str(run_dir),
            action_token=next_token,
        )
    activity_token = _new_activity_authorization(run_dir, activity_name)
    return _render(
        message="新活动授权已创建。请在 30 分钟内上传并预览新名单；这一步仍不会发送邮件。",
        message_kind="success",
        activity_token=activity_token,
        activity_from=run_dir.name,
        activity_name=activity_name,
    )


def main():
    url = "http://127.0.0.1:8501"
    try:
        server = make_server("127.0.0.1", 8501, app)
    except OSError as e:
        print("❌ MailPilot 无法启动：本机 8501 端口可能已被占用。")
        print("请关闭旧的 MailPilot 终端窗口后重试。")
        print(f"技术信息：{e}")
        return 1
    print(f"✅ MailPilot Agent 已启动：{url}")
    print("请保持此窗口打开。关闭窗口会停止本地网页，但不会丢失发送台账。")

    def open_browser():
        try:
            webbrowser.open(url)
        except Exception:
            pass

    opener = threading.Timer(0.2, open_browser)
    opener.daemon = True
    opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMailPilot 已安全停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

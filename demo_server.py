#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 临时环境加密持久化演示站点核心脚本

核心思路：利用 GitHub Actions 的 schedule 定时唤醒 + GitHub Releases 永久存储，
实现无需本地守护进程的"云端自动续命 + 数据加密持久化"。

功能：
- 启动时从 GitHub Releases 拉取 AES-256-GCM 加密备份并解密恢复
- 后台线程定期把数据库加密后上传回 Releases（uploads.github.com 域名）
- Flask 演示站点（留言板，验证跨 job 持久化）
- Cloudflare 固定隧道（自定义域名 ghvps.kekeke.cc.cd），URL 自动上报仓库
- 无缝衔接：job 到期前预触发下一个 job（PRE_WAKE_SECONDS），可用率 99.9%
- 【主job锁】杜绝数据分叉：多 job 并行时仅 leader 写库+备份，follower 只读。
  用 Releases 里的 leader.json 心跳文件作分布式锁，leader 心跳过期后 follower 自动升级接管。
- 远程控制接口 /api/exec（带 EXEC_TOKEN 认证），可实时执行 shell 命令
"""
import os
import json
import time
import uuid
import sqlite3
import base64
import threading
import datetime
import subprocess
import urllib.request
import urllib.error
import re

from flask import Flask, request, jsonify, render_template_string

# ==================== 配置 ====================
REPO = os.environ.get("REPO", "7891333/demo-vps")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
DEMO_KEY = os.environ.get("DEMO_KEY", "")
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")  # Cloudflare 固定隧道凭证
TUNNEL_HOST = os.environ.get("TUNNEL_HOST", "ghvps.kekeke.cc.cd")  # 固定域名
PRE_WAKE_SECONDS = int(os.environ.get("PRE_WAKE_SECONDS", "21000"))  # 到期前预唤醒（21000s=5h50m）
BACKUP_TAG = "backup"
ASSET_NAME = "demo.db.enc"
LEADER_ASSET = "leader.json"  # 分布式锁心跳文件
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))  # 心跳间隔
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "90"))  # 心跳过期判定
DB_FILE = "demo.db"
PORT = int(os.environ.get("PORT", "8080"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "45"))  # 秒

JOB_ID = uuid.uuid4().hex[:8]
START_TIME = datetime.datetime.now(datetime.timezone.utc)
LAST_URL = ""
LOAD_STATUS = "初始化中"
PRE_WAKE_DONE = False  # 防止重复预触发
IS_LEADER = False  # 是否为主 job（负责写库+备份）

# ==================== 加密工具 ====================
def encrypt_file(data: bytes, key_hex: str) -> bytes:
    """AES-256-GCM 加密，返回 nonce+tag+ciphertext"""
    from Crypto.Cipher import AES
    key = bytes.fromhex(key_hex)
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + ciphertext


def decrypt_file(blob: bytes, key_hex: str) -> bytes:
    """AES-256-GCM 解密，校验完整性"""
    from Crypto.Cipher import AES
    key = bytes.fromhex(key_hex)
    nonce, tag, ciphertext = blob[:16], blob[16:32], blob[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)


# ==================== GitHub API ====================
def gh_request(method, url, data=None, headers=None, raw=False, timeout=60):
    """通用 GitHub API 请求，返回 (status, body)"""
    h = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, headers=h)
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            content = r.read()
            if raw:
                return r.status, content
            return r.status, json.loads(content.decode() or "null")
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content.decode() or "null")
        except Exception:
            return e.code, content.decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def get_release():
    """获取 backup release，不存在返回 None"""
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{BACKUP_TAG}"
    status, data = gh_request("GET", url)
    return data if status == 200 else None


def ensure_release():
    """确保 backup release 存在，返回 release id"""
    rel = get_release()
    if rel:
        return rel["id"]
    url = f"https://api.github.com/repos/{REPO}/releases"
    data = {
        "tag_name": BACKUP_TAG,
        "name": "加密备份",
        "body": "AES-256-GCM 加密的数据库备份（自动生成）",
        "draft": False,
        "prerelease": False,
    }
    status, d = gh_request("POST", url, data=data)
    if status in (200, 201):
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def delete_asset(name):
    """删除 release 中指定名称的 asset"""
    rel = get_release()
    if rel:
        for a in rel.get("assets", []):
            if a.get("name") == name:
                gh_request("DELETE", f"https://api.github.com/repos/{REPO}/releases/assets/{a['id']}")


def upload_asset(name, data_bytes):
    """上传/覆盖 asset，返回 (大小, HTTP状态)。必须用 uploads.github.com 域名"""
    rel_id = ensure_release()
    delete_asset(name)
    url = f"https://uploads.github.com/repos/{REPO}/releases/{rel_id}/assets?name={name}"
    status, resp = gh_request("POST", url, data=data_bytes, headers={"Content-Type": "application/octet-stream"})
    return len(data_bytes), status


def download_asset(name):
    """下载 asset 内容，不存在返回 None。必须带 Accept: application/octet-stream 头"""
    rel = get_release()
    if not rel:
        return None
    for a in rel.get("assets", []):
        if a.get("name") == name:
            status, blob = gh_request(
                "GET", f"https://api.github.com/repos/{REPO}/releases/assets/{a['id']}",
                raw=True, headers={"Accept": "application/octet-stream"},
            )
            return blob if status == 200 else None
    return None


def load_or_create():
    """从 Releases 拉取并解密数据库，若无备份则新建初始库"""
    global LOAD_STATUS
    blob = download_asset(ASSET_NAME)
    if blob:
        try:
            data = decrypt_file(blob, DEMO_KEY)
            with open(DB_FILE, "wb") as f:
                f.write(data)
            LOAD_STATUS = f"从 Releases 恢复加密备份（{len(data)} 字节）"
            print(f"[load] {LOAD_STATUS}", flush=True)
            return
        except Exception as e:
            print(f"[load] 解密失败，改用新库: {e}", flush=True)
    create_new_db()
    LOAD_STATUS = "新建初始数据库"
    print(f"[load] {LOAD_STATUS}", flush=True)


def create_new_db():
    """创建初始数据库结构"""
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
        (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()


def backup_database():
    """把当前数据库加密后上传到 Releases（覆盖旧备份），返回 (大小, HTTP状态)"""
    with open(DB_FILE, "rb") as f:
        data = f.read()
    enc = encrypt_file(data, DEMO_KEY)
    return upload_asset(ASSET_NAME, enc)


# ==================== 主 job 锁（杜绝数据分叉） ====================
def get_leader():
    """读取 leader.json 心跳，返回 dict 或 None"""
    blob = download_asset(LEADER_ASSET)
    if not blob:
        return None
    try:
        return json.loads(blob.decode())
    except Exception:
        return None


def set_leader_heartbeat():
    """更新 leader 心跳到 Releases"""
    data = json.dumps({"job_id": JOB_ID, "heartbeat": time.time()}).encode()
    upload_asset(LEADER_ASSET, data)


def acquire_leader():
    """尝试成为 leader，成功返回 True"""
    global IS_LEADER
    leader = get_leader()
    now = time.time()
    if leader and leader.get("job_id") != JOB_ID and (now - leader.get("heartbeat", 0)) < HEARTBEAT_TIMEOUT:
        IS_LEADER = False
        print(f"[leader] 已有活跃 leader: {leader.get('job_id')}，本 job 为 follower（只读）", flush=True)
        return False
    IS_LEADER = True
    set_leader_heartbeat()
    print(f"[leader] 本 job 成为 leader: {JOB_ID}", flush=True)
    return True


def leader_loop():
    """leader 心跳线程：定期刷新心跳，保持锁"""
    while True:
        if not IS_LEADER:
            return
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            set_leader_heartbeat()
        except Exception as e:
            print(f"[leader] 心跳失败: {e}", flush=True)


def follower_loop():
    """follower 线程：检测 leader 心跳过期后升级为 leader 接管"""
    global IS_LEADER
    while True:
        if IS_LEADER:
            return
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            leader = get_leader()
            now = time.time()
            if not leader or (now - leader.get("heartbeat", 0)) >= HEARTBEAT_TIMEOUT:
                if acquire_leader():
                    # 升级为 leader，重新拉取最新备份并启动备份线程
                    try:
                        load_or_create()
                        print("[leader] 升级后已重新拉取最新备份", flush=True)
                    except Exception as e:
                        print(f"[leader] 升级后重拉失败: {e}", flush=True)
                    threading.Thread(target=backup_loop, daemon=True).start()
                    return
        except Exception as e:
            print(f"[follower] 检查失败: {e}", flush=True)


# ==================== 公网 URL 上报 ====================
def report_url(url):
    """把公网 URL 写到仓库的 public_url.txt，方便随时查询"""
    global LAST_URL
    LAST_URL = url
    try:
        get_url = f"https://api.github.com/repos/{REPO}/contents/public_url.txt"
        status, data = gh_request("GET", get_url)
        payload = {
            "message": f"update public url {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": base64.b64encode(url.encode()).decode(),
        }
        if status == 200:
            payload["sha"] = data.get("sha")
        gh_request("PUT", get_url, data=payload)
        print(f"[url] 已上报公网地址: {url}", flush=True)
    except Exception as e:
        print(f"[url] 上报失败: {e}", flush=True)


def start_tunnel():
    """启动隧道：优先 Cloudflare 固定隧道（自定义域名），回退 quick tunnel"""
    try:
        if TUNNEL_TOKEN:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", TUNNEL_TOKEN],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            url = f"https://{TUNNEL_HOST}"
            print(f"[tunnel] 固定隧道启动: {url}", flush=True)
            report_url(url)
            for line in proc.stdout:
                line = line.strip()
                if "Registered tunnel connection" in line:
                    print(f"[tunnel] 连接已注册: {line}", flush=True)
                elif "ERR" in line.upper() and "error" in line.lower():
                    print(f"[tunnel] 异常: {line}", flush=True)
        else:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            reported = False
            for line in proc.stdout:
                line = line.strip()
                if "trycloudflare.com" in line:
                    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if m and not reported:
                        url = m.group(0)
                        print(f"[tunnel] 公网地址: {url}", flush=True)
                        report_url(url)
                        reported = True
    except Exception as e:
        print(f"[tunnel] 启动失败: {e}", flush=True)


# ==================== Flask 站点 ====================
app = Flask(__name__)


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def bump_visits():
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'visits'")
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key='visits'").fetchone()
    conn.close()
    return int(row["value"])


def elapsed_seconds():
    return int((datetime.datetime.now(datetime.timezone.utc) - START_TIME).total_seconds())


def elapsed_str(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Actions 加密持久化演示</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:#f5f5f5;min-height:100vh}
.wrap{max-width:760px;margin:0 auto;padding:24px 16px 48px}
header{padding:28px 0 20px;border-bottom:1px solid #2a2a2a;margin-bottom:24px}
header h1{font-size:22px;font-weight:600;letter-spacing:0.5px;color:#fff}
header p{font-size:13px;color:#888;margin-top:8px;line-height:1.6}
.badge{display:inline-block;padding:4px 12px;border-radius:4px;font-size:11px;font-weight:500;letter-spacing:0.3px;border:1px solid #333}
.badge.green{color:#7ee787;border-color:#2a5e2a}
.badge.amber{color:#ffab00;border-color:#5a4a00}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:24px}
.card{background:#1e1e1e;border:1px solid #2a2a2a;border-radius:6px;padding:18px}
.card .label{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:8px}
.card .val{font-size:26px;font-weight:600;color:#fff;font-family:'SF Mono',Consolas,monospace}
.card .sub{font-size:12px;color:#aaa;margin-top:6px}
.card .mono{font-size:13px;color:#ccc;font-family:Consolas,monospace;word-break:break-all}
.section{background:#1e1e1e;border:1px solid #2a2a2a;border-radius:6px;padding:20px;margin-bottom:24px}
.section h2{font-size:14px;font-weight:600;color:#fff;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.section h2 .dot{width:6px;height:6px;border-radius:50%;background:#7ee787;display:inline-block}
.list{list-style:none}
.list li{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid #2a2a2a;font-size:13px}
.list li:last-child{border-bottom:none}
.list .t{color:#f5f5f5;flex:1}
.list .d{color:#888;font-size:12px;white-space:nowrap}
.form{display:flex;gap:10px;margin-top:16px}
.form input{flex:1;background:#111;border:1px solid #333;border-radius:4px;color:#fff;padding:10px 12px;font-size:13px;outline:none}
.form input:focus{border-color:#666}
.form button{background:#fff;color:#0a0a0a;border:none;border-radius:4px;padding:10px 18px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
.form button:hover{opacity:.85}
.form button:active{transform:scale(.97)}
.form button:disabled{opacity:.4;cursor:not-allowed}
.foot{text-align:center;color:#555;font-size:11px;padding-top:20px;border-top:1px solid #1a1a1a}
.foot .mono{font-family:Consolas,monospace}
.enc{display:inline-flex;align-items:center;gap:6px;color:#7ee787;font-size:12px}
.enc .lock{width:10px;height:10px;border:2px solid #7ee787;border-radius:2px;position:relative;display:inline-block}
.enc .lock:after{content:'';position:absolute;left:-1px;top:-6px;width:8px;height:6px;border:2px solid #7ee787;border-bottom:none;border-radius:3px 3px 0 0}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(100px);background:#2a2a2a;color:#fff;padding:12px 20px;border-radius:4px;font-size:13px;opacity:0;transition:all .3s;z-index:10}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
@media(max-width:520px){header h1{font-size:18px}.card .val{font-size:22px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <span class="badge {{ 'green' if is_leader else 'orange' }}">{{ 'LEADER 主节点' if is_leader else 'FOLLOWER 备份节点' }}</span>
  <h1>GitHub Actions 临时环境演示</h1>
  <p>利用 GitHub Actions 定时唤醒 + Releases 永久存储，实现无需本地守护进程的自动续传与数据加密持久化。</p>
</header>

<div class="grid">
  <div class="card">
    <div class="label">当前 Job</div>
    <div class="val">{{ job_id }}</div>
    <div class="sub">每次唤醒随机生成</div>
  </div>
  <div class="card">
    <div class="label">运行时长</div>
    <div class="val" id="elapsed">{{ elapsed }}</div>
    <div class="sub">本次 job 已运行</div>
  </div>
  <div class="card">
    <div class="label">访问次数</div>
    <div class="val">{{ visits }}</div>
    <div class="sub">跨 job 累计，加密持久化</div>
  </div>
  <div class="card">
    <div class="label">数据来源</div>
    <div class="val" style="font-size:15px">{{ data_source }}</div>
    <div class="sub">从 Releases 解密恢复</div>
  </div>
</div>

<div class="section">
  <h2><span class="dot"></span>留言板（数据持久化验证）</h2>
  <p style="font-size:12px;color:#888;margin-bottom:12px">在这里添加留言，数据会被 AES 加密后上传到 GitHub Releases，即使 job 销毁，下次唤醒也会自动恢复。{{ '主节点可写入' if is_leader else '当前为备份节点（只读），leader 切换后恢复写入' }}</p>
  <ul class="list" id="msglist">
    {% for m in messages %}
    <li><span class="t">{{ m.content }}</span><span class="d">{{ m.created_at }}</span></li>
    {% else %}
    <li><span class="t" style="color:#666">暂无留言，来添加第一条吧</span></li>
    {% endfor %}
  </ul>
  <div class="form">
    <input id="content" placeholder="输入留言内容..." maxlength="200" {{ '' if is_leader else 'disabled' }}>
    <button onclick="addMsg()" {{ '' if is_leader else 'disabled' }}>添加</button>
  </div>
</div>

<div class="section">
  <h2><span class="dot"></span>安全与备份机制</h2>
  <ul class="list">
    <li><span class="t">加密算法</span><span class="d enc"><span class="lock"></span> AES-256-GCM</span></li>
    <li><span class="t">密钥存储</span><span class="d">GitHub Secrets（不落盘）</span></li>
    <li><span class="t">备份位置</span><span class="d">GitHub Releases</span></li>
    <li><span class="t">自动备份间隔</span><span class="d">{{ backup_interval }} 秒</span></li>
    <li><span class="t">job 生命周期</span><span class="d">~6 小时 · 无缝衔接 · 主job锁</span></li>
  </ul>
</div>

<div class="foot">Job ID <span class="mono">{{ job_id }}</span> · 演示环境 · 数据加密后存于 Releases，仓库公开亦安全</div>
</div>

<div class="toast" id="toast"></div>

<script>
function toast(msg){var t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2500)}
function addMsg(){
  var c=document.getElementById('content').value.trim();
  if(!c){toast('请输入内容');return}
  fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.ok){toast('已添加，并加密备份');setTimeout(function(){location.reload()},600)}
      else{toast(d.error||'失败')}
    }).catch(function(){toast('网络错误')});
}
setInterval(function(){
  var el=document.getElementById('elapsed');
  var s=parseInt(el.textContent)+1;
  var h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;
  el.textContent=(h<10?'0':'')+h+':'+(m<10?'0':'')+m+':'+(sec<10?'0':'')+sec;
},1000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    v = bump_visits()
    conn = get_conn()
    msgs = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50")]
    conn.close()
    return render_template_string(
        HTML, job_id=JOB_ID, elapsed=elapsed_str(elapsed_seconds()), visits=v,
        data_source=LOAD_STATUS, messages=msgs, backup_interval=BACKUP_INTERVAL,
        is_leader=IS_LEADER,
    )


@app.route("/api/add", methods=["POST"])
def add_msg():
    if not IS_LEADER:
        return jsonify(ok=False, error="当前为备份节点（只读），leader 切换后自动恢复写入"), 503
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify(ok=False, error="内容为空"), 400
    if len(content) > 200:
        return jsonify(ok=False, error="内容过长"), 400
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (content, created_at) VALUES (?, ?)",
        (content, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/backup", methods=["POST"])
def manual_backup():
    if not IS_LEADER:
        return jsonify(ok=False, error="当前为备份节点，不执行备份"), 503
    try:
        size, status = backup_database()
        return jsonify(ok=True, size=size, status=status)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/health")
def health():
    return jsonify(ok=True, job_id=JOB_ID, elapsed=elapsed_seconds(), leader=IS_LEADER)


@app.route("/api/status")
def api_status():
    return jsonify(
        ok=True, job_id=JOB_ID, elapsed=elapsed_seconds(), leader=IS_LEADER,
        url=LAST_URL, source=LOAD_STATUS, backup_interval=BACKUP_INTERVAL,
        tunnel_host=TUNNEL_HOST, pre_wake=PRE_WAKE_SECONDS,
    )


@app.route("/api/exec", methods=["POST"])
def exec_cmd():
    """远程控制：带令牌认证，执行 shell 命令并返回输出"""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    if not EXEC_TOKEN or token != EXEC_TOKEN:
        return jsonify(ok=False, error="未授权"), 403
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    if len(cmd) > 2000:
        return jsonify(ok=False, error="命令过长"), 400
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return jsonify(
            ok=True, code=proc.returncode,
            stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:],
        )
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="命令执行超时(30s)"), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ==================== 后台备份线程 ====================
def backup_loop():
    while True:
        time.sleep(BACKUP_INTERVAL)
        if not IS_LEADER:
            return
        try:
            size, status = backup_database()
            print(f"[backup] 已加密上传 {size} 字节 (HTTP {status})", flush=True)
        except Exception as e:
            print(f"[backup] 失败: {e}", flush=True)


# ==================== 无缝衔接：预触发下一个 job ====================
def pre_wake_loop():
    """job 到期前预触发下一个 job，新旧重叠实现无缝衔接（可用率 99.9%）"""
    global PRE_WAKE_DONE
    while True:
        elapsed = elapsed_seconds()
        if elapsed >= PRE_WAKE_SECONDS and not PRE_WAKE_DONE:
            PRE_WAKE_DONE = True
            try:
                url = f"https://api.github.com/repos/{REPO}/actions/workflows/demo.yml/dispatches"
                gh_request("POST", url, data={"ref": "main"})
                print(f"[prewake] 已预触发下一个 job（运行 {elapsed}s），无缝衔接", flush=True)
            except Exception as e:
                print(f"[prewake] 触发失败: {e}", flush=True)
            break
        time.sleep(60)


# ==================== main ====================
if __name__ == "__main__":
    print(f"=== Job ID: {JOB_ID} ===", flush=True)
    print(f"=== 仓库: {REPO} ===", flush=True)
    print(f"=== 固定域名: {TUNNEL_HOST} ===", flush=True)
    if not DEMO_KEY:
        print("[warn] DEMO_KEY 未设置，加密不可用", flush=True)
    if not GH_TOKEN:
        print("[warn] GH_TOKEN 未设置，备份不可用", flush=True)
    if not EXEC_TOKEN:
        print("[warn] EXEC_TOKEN 未设置，远程控制不可用", flush=True)
    if not TUNNEL_TOKEN:
        print("[warn] TUNNEL_TOKEN 未设置，回退 quick tunnel", flush=True)
    load_or_create()
    # 抢锁：决定本 job 是 leader（写+备份）还是 follower（只读）
    acquire_leader()
    if IS_LEADER:
        threading.Thread(target=backup_loop, daemon=True).start()
        threading.Thread(target=leader_loop, daemon=True).start()
    else:
        threading.Thread(target=follower_loop, daemon=True).start()
    threading.Thread(target=start_tunnel, daemon=True).start()
    threading.Thread(target=pre_wake_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
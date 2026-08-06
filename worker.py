# -*- coding: utf-8 -*-
"""工作实例：WSS 终端 + API 命令执行 + 文件持久化 + 自动续命"""
import os
import io
import json
import time
import select
import threading
import datetime
import subprocess

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit

import config
import core
import terminal
import tunnels


# ==================== 实例初始化 ====================
def init_instance():
    """读取实例配置，设置专属 asset 名，恢复数据"""
    # 实例专属 asset（数据隔离，多实例互不干扰）
    config.ASSET_DB = f"inst-{config.INSTANCE_ID}.db.enc"
    config.ASSET_FILES = f"inst-{config.INSTANCE_ID}.files.tar.gz.enc"
    config.ASSET_LEADER = f"leader-{config.INSTANCE_ID}.json"
    # 读取实例配置（tunnel token 等）
    cfg = core.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default={})
    config.TUNNEL_TOKEN = cfg.get("tunnel_token", config.TUNNEL_TOKEN)
    config.TUNNEL_HOST = cfg.get("hostname", config.TUNNEL_HOST)
    return cfg


# ==================== Flask / SocketIO ====================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)

_sid_to_key = {}
JOB_STATE = {"last_url": "", "load_status": "初始化中"}
leader = None


def _elapsed():
    return int((datetime.datetime.now(datetime.timezone.utc) - core.START_TIME).total_seconds())


def _elapsed_str(sec):
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ==================== HTTP 路由 ====================
@app.route("/")
def index():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job=core.JOB_ID,
                   elapsed=_elapsed(), leader=leader.is_leader if leader else False,
                   url=JOB_STATE["last_url"], terminal="wss://" + config.TUNNEL_HOST + "/socket.io")


@app.route("/api/status")
def api_status():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job_id=core.JOB_ID,
                   elapsed=_elapsed(), leader=leader.is_leader if leader else False,
                   url=JOB_STATE["last_url"], source=JOB_STATE["load_status"],
                   tunnel_host=config.TUNNEL_HOST)


@app.route("/api/health")
def api_health():
    return jsonify(ok=True, instance=config.INSTANCE_ID, elapsed=_elapsed())


@app.route("/api/exec", methods=["POST"])
def api_exec():
    """命令执行：带 token 认证 + 超时控制"""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return jsonify(ok=False, error="未授权"), 403
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    if len(cmd) > 2000:
        return jsonify(ok=False, error="命令过长"), 400
    timeout = int(data.get("timeout", 30))
    timeout = max(1, min(timeout, 600))  # 1~600 秒
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return jsonify(ok=True, code=proc.returncode,
                       stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error=f"命令执行超时({timeout}s)"), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/backup", methods=["POST"])
def api_backup():
    if leader and not leader.is_leader:
        return jsonify(ok=False, error="当前为备份节点，不执行备份"), 503
    try:
        db_size, db_status = core.backup_database()
        res = core.backup_files()
        f_size, f_status = res if res else (None, None)
        return jsonify(ok=True, db_size=db_size, db_status=db_status, files_size=f_size, files_status=f_status)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/term/screen")
def api_term_screen():
    """返回指定终端会话的干净屏幕文本（pyte）"""
    session_key = request.args.get("session", "")
    return jsonify(ok=True, screen=terminal.get_screen(session_key))


# ==================== WSS 终端（bytes 传输） ====================
def _pty_reader(session_key, sid):
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    try:
        while sess.attached:
            r, _, _ = select.select([sess.fd], [], [], 1.0)
            if r:
                data = sess.read_output()
                if data is None:
                    break
                if data:
                    sess.feed(data)          # 喂给 pyte 维护干净屏幕
                    socketio.emit("output", data, to=sid)  # 原始 bytes 传输
            else:
                wpid, status = os.waitpid(sess.pid, os.WNOHANG)
                if wpid == sess.pid:
                    socketio.emit("exit", {"code": status}, to=sid)
                    break
    except Exception:
        pass


@socketio.on("connect")
def ws_connect(auth):
    token = ""
    session_key = ""
    if isinstance(auth, dict):
        token = auth.get("token", "")
        session_key = auth.get("session", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return False
    if not session_key:
        session_key = f"{config.INSTANCE_ID}-{core.JOB_ID}"
    sess = terminal.get_or_create_session(session_key)
    _sid_to_key[request.sid] = session_key
    threading.Thread(target=_pty_reader, args=(session_key, request.sid), daemon=True).start()
    socketio.emit("session", {"session_key": session_key}, to=request.sid)


@socketio.on("input")
def ws_input(data):
    session_key = _sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    payload = data if isinstance(data, bytes) else data.encode()
    sess.write_input(payload)


@socketio.on("resize")
def ws_resize(data):
    session_key = _sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    try:
        sess.resize(int(data.get("rows", 24)), int(data.get("cols", 80)))
    except Exception:
        pass


@socketio.on("disconnect")
def ws_disconnect():
    session_key = _sid_to_key.pop(request.sid, "")
    if session_key:
        terminal.detach_session(session_key)


# ==================== 备份/续命/隧道线程 ====================
def _backup_loop():
    while True:
        time.sleep(config.BACKUP_INTERVAL)
        if leader and not leader.is_leader:
            return
        try:
            size, status = core.backup_database()
            print(f"[backup] 数据库已加密上传 {size} 字节 (HTTP {status})", flush=True)
        except Exception as e:
            print(f"[backup] 数据库备份失败: {e}", flush=True)
        try:
            res = core.backup_files()
            if res:
                size, status = res
                print(f"[backup] 文件已加密上传 {size} 字节 (HTTP {status})", flush=True)
        except Exception as e:
            print(f"[backup] 文件备份失败: {e}", flush=True)


def _worker_pre_wake():
    """worker 续命：先检查实例是否仍存在（未被关闭）"""
    done = False
    while True:
        elapsed = _elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            # 检查实例配置是否还存在（被关闭则删除，不续命）
            cfg = core.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default=None)
            if cfg is None:
                print(f"[prewake] 实例 {config.INSTANCE_ID} 已关闭，不再续命", flush=True)
                return
            try:
                url = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
                core.gh_request("POST", url, data={"ref": "main", "inputs": {"INSTANCE_ID": config.INSTANCE_ID}})
                print(f"[prewake] 已预触发下一个 worker（{config.INSTANCE_ID}）", flush=True)
            except Exception as e:
                print(f"[prewake] 触发失败: {e}", flush=True)
            break
        time.sleep(60)


def _start_tunnel():
    if not config.TUNNEL_TOKEN:
        print("[tunnel] 无 tunnel token，跳过", flush=True)
        return
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", config.TUNNEL_TOKEN],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        url = f"https://{config.TUNNEL_HOST}"
        JOB_STATE["last_url"] = url
        print(f"[tunnel] 固定隧道启动: {url}", flush=True)
        for line in proc.stdout:
            line = line.strip()
            if "Registered tunnel connection" in line:
                print("[tunnel] 连接已注册", flush=True)
    except Exception as e:
        print(f"[tunnel] 启动失败: {e}", flush=True)


# ==================== 入口 ====================
def run():
    global leader
    # 实例初始化
    init_cfg = init_instance()
    os.makedirs(config.FILES_DIR, exist_ok=True)
    JOB_STATE["load_status"] = core.load_or_create()
    print(f"=== Worker 实例 {config.INSTANCE_ID} 启动 ===", flush=True)
    print(f"=== 固定域名: {config.TUNNEL_HOST} ===", flush=True)
    # leader 锁
    from core import LeaderLock
    leader = LeaderLock()
    leader.acquire()
    if leader.is_leader:
        threading.Thread(target=_backup_loop, daemon=True).start()
        threading.Thread(target=leader.heartbeat_loop, daemon=True).start()
    else:
        def _on_promote():
            JOB_STATE["load_status"] = core.load_or_create()
            threading.Thread(target=_backup_loop, daemon=True).start()
        threading.Thread(target=leader.follower_loop, args=(_on_promote,), daemon=True).start()
    threading.Thread(target=_start_tunnel, daemon=True).start()
    threading.Thread(target=_worker_pre_wake, daemon=True).start()
    terminal.start_cleanup()
    socketio.run(app, host="0.0.0.0", port=config.PORT, allow_unsafe_werkzeug=True)
# -*- coding: utf-8 -*-
"""Web 层：Flask 应用 + SocketIO + HTTP 路由 + WSS 终端事件"""
import os
import select
import threading
import datetime
import subprocess

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit

import config
import core
import terminal

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)

# sid -> session_key 映射（用于断开时定位会话）
_sid_to_key = {}

# 当前 job 状态
JOB_STATE = {"last_url": "", "load_status": "初始化中"}

# leader 锁（由 create_app 注入）
leader = None


# ==================== 数据库/时间辅助 ====================
def _bump_visits():
    conn = core._db_conn()
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'visits'")
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key='visits'").fetchone()
    conn.close()
    return int(row["value"])


def _elapsed():
    return int((datetime.datetime.now(datetime.timezone.utc) - core.START_TIME).total_seconds())


def _elapsed_str(sec):
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ==================== HTTP 路由 ====================
@app.route("/")
def index():
    visits = _bump_visits()
    conn = core._db_conn()
    msgs = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50")]
    conn.close()
    return render_template(
        "index.html",
        job_id=core.JOB_ID,
        elapsed=_elapsed_str(_elapsed()),
        elapsed_sec=_elapsed(),
        visits=visits,
        data_source=JOB_STATE["load_status"],
        messages=msgs,
        backup_interval=config.BACKUP_INTERVAL,
        is_leader=leader.is_leader,
        tunnel_host=config.TUNNEL_HOST,
    )


@app.route("/api/add", methods=["POST"])
def api_add():
    if not leader.is_leader:
        return jsonify(ok=False, error="当前为备份节点（只读），leader 切换后自动恢复写入"), 503
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify(ok=False, error="内容为空"), 400
    if len(content) > 200:
        return jsonify(ok=False, error="内容过长"), 400
    conn = core._db_conn()
    conn.execute(
        "INSERT INTO messages (content, created_at) VALUES (?, ?)",
        (content, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/backup", methods=["POST"])
def api_backup():
    if not leader.is_leader:
        return jsonify(ok=False, error="当前为备份节点，不执行备份"), 503
    try:
        db_size, db_status = core.backup_database()
        res = core.backup_files()
        f_size, f_status = res if res else (None, None)
        return jsonify(ok=True, db_size=db_size, db_status=db_status,
                       files_size=f_size, files_status=f_status)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/health")
def api_health():
    return jsonify(ok=True, job_id=core.JOB_ID, elapsed=_elapsed(), leader=leader.is_leader)


@app.route("/api/status")
def api_status():
    return jsonify(
        ok=True, job_id=core.JOB_ID, elapsed=_elapsed(), leader=leader.is_leader,
        url=JOB_STATE["last_url"], source=JOB_STATE["load_status"],
        backup_interval=config.BACKUP_INTERVAL, tunnel_host=config.TUNNEL_HOST,
        pre_wake=config.PRE_WAKE_SECONDS, ws_terminal=True,
    )


@app.route("/api/exec", methods=["POST"])
def api_exec():
    """一次性远程命令执行"""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return jsonify(ok=False, error="未授权"), 403
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    if len(cmd) > 2000:
        return jsonify(ok=False, error="命令过长"), 400
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return jsonify(ok=True, code=proc.returncode,
                       stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="命令执行超时(30s)"), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ==================== WSS 终端事件 ====================
def _pty_reader(session_key, sid):
    """读取 PTY 输出并推送给客户端"""
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
                    socketio.emit("output", data.decode("utf-8", errors="replace"), to=sid)
            else:
                wpid, status = os.waitpid(sess.pid, os.WNOHANG)
                if wpid == sess.pid:
                    socketio.emit("exit", {"code": status}, to=sid)
                    break
    except Exception:
        pass


@socketio.on("connect")
def ws_connect(auth):
    """认证 + 创建/复用终端会话（session_key 相同则复用，支持断线重连）"""
    token = ""
    session_key = ""
    if isinstance(auth, dict):
        token = auth.get("token", "")
        session_key = auth.get("session", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        print(f"[ws] 拒绝未授权连接: {request.sid}", flush=True)
        return False
    if not session_key:
        session_key = f"anon-{request.sid}"
    sess = terminal.get_or_create_session(session_key)
    _sid_to_key[request.sid] = session_key
    threading.Thread(target=_pty_reader, args=(session_key, request.sid), daemon=True).start()
    socketio.emit("session", {"session_key": session_key}, to=request.sid)
    print(f"[ws] 终端连接: sid={request.sid} session={session_key}", flush=True)


@socketio.on("input")
def ws_input(data):
    session_key = _sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    payload = data.encode() if isinstance(data, str) else data
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
        print(f"[ws] 终端断开: session={session_key}（保留{config.SESSION_TTL}s 等待重连）", flush=True)


# ==================== 应用工厂 ====================
def create_app(leader_lock):
    """注入 leader 锁，返回 (app, socketio)"""
    global leader
    leader = leader_lock
    terminal.start_cleanup()
    return app, socketio
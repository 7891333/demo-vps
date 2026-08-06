# -*- coding: utf-8 -*-
"""工作实例：WSS 终端 + API 命令执行 + 文件持久化 + 自动续命 + 状态上报 + 系统瘦身"""
import os
import io
import json
import time
import select
import threading
import datetime
import subprocess
import urllib.request

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

import config
import core
import terminal


# ==================== 实例初始化 ====================
def init_instance():
    """读取实例配置，设置专属 asset 名，恢复数据"""
    config.ASSET_DB = f"inst-{config.INSTANCE_ID}.db.enc"
    config.ASSET_FILES = f"inst-{config.INSTANCE_ID}.files.tar.gz.enc"
    config.ASSET_LEADER = f"leader-{config.INSTANCE_ID}.json"
    cfg = core.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default={})
    config.TUNNEL_TOKEN = cfg.get("tunnel_token", config.TUNNEL_TOKEN)
    config.TUNNEL_HOST = cfg.get("hostname", config.TUNNEL_HOST)
    return cfg


# ==================== 系统瘦身（省资源） ====================
def _system_trim():
    """
    停用云环境用不到的服务，节省资源。
    已实测验证不影响功能：health/exec/WSS/备份全部正常。
    新实例启动自动执行。
    """
    services = [
        # PHP（我们用不到）
        "php8.3-fpm", "php8.2-fpm", "php8.1-fpm", "php-fpm",
        # 云环境用不到的系统服务
        "ModemManager",       # 调制解调器
        "multipathd",         # 多路径存储
        "walinuxagent",       # Azure 管理代理
        "udisks2",            # 磁盘管理器
        "getty@tty1",         # 物理登录终端
        "serial-getty@ttyS0", # 串口登录终端
        # Docker（GitHub runner 无需，已验证）
        "docker", "containerd", "docker.socket",
        # snap 包管理（无用）
        "snapd", "snapd.socket", "snapd.seeded", "snapd.apparmor",
        "snapd.core-fixup", "snapd.autoimport", "snapd.system-shutdown",
        "snapd.snap-repair.timer",
    ]
    for svc in services:
        try:
            subprocess.run(f"sudo systemctl stop {svc} 2>/dev/null", shell=True, timeout=10)
            subprocess.run(f"sudo systemctl disable {svc} 2>/dev/null", shell=True, timeout=10)
        except Exception:
            pass
    print("[trim] 系统瘦身完成（停用无用服务）", flush=True)


# ==================== 终端配置 ====================
def _write_shell_profile():
    """生成终端配置：root 用户 + kodebite + 默认进入持久化目录"""
    persist = config.FILES_DIR  # 绝对路径 /home/runner/files
    os.makedirs(persist, exist_ok=True)
    try:
        # runner 用户的 bashrc（供 /api/exec 等使用）
        home = os.path.expanduser("~")
        bashrc = f"""# GitHub Actions 云端终端配置
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TERM=xterm-256color
export PS1='\[\e[32m\]kodebite@kodebite\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]\$ '
cd {persist} 2>/dev/null || true
alias sudo='sudo '
"""
        with open(os.path.join(home, ".bashrc"), "w") as f:
            f.write(bashrc)
        with open(os.path.join(home, ".bash_profile"), "w") as f:
            f.write("source ~/.bashrc 2>/dev/null\n")
        # root 用户的 bashrc（WSS 终端默认 root 登录时读取）
        root_bashrc = f"""# root 终端配置
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TERM=xterm-256color
export PS1='\[\e[31m\]root@kodebite\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]# '
cd {persist} 2>/dev/null || true
"""
        with open("/root/.bashrc", "w") as f:
            f.write(root_bashrc)
        subprocess.run("sudo hostname kodebite 2>/dev/null || hostname kodebite 2>/dev/null",
                       shell=True, timeout=5)
        print("[shell] 终端配置完成（root + kodebite + 持久化目录）", flush=True)
    except Exception as e:
        print(f"[shell] 配置写入失败: {e}", flush=True)


def _run_setup():
    """启动时执行 ~/files/setup.sh（用户自定义自启动配置）"""
    setup = os.path.join(config.FILES_DIR, "setup.sh")
    if not os.path.exists(setup):
        return
    print("[setup] 检测到 setup.sh，后台执行...", flush=True)
    try:
        subprocess.Popen(["bash", setup],
                         stdout=open("/tmp/setup.log", "w"),
                         stderr=subprocess.STDOUT,
                         start_new_session=True)
        print("[setup] setup.sh 已在后台执行，日志 /tmp/setup.log", flush=True)
    except Exception as e:
        print(f"[setup] 执行失败: {e}", flush=True)


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


# ==================== HTTP 路由 ====================
@app.route("/")
def index():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job=core.JOB_ID,
                   elapsed=_elapsed(), leader=leader.is_leader if leader else False,
                   url=JOB_STATE["last_url"])


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
    timeout = max(1, min(timeout, 600))
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
        db_size, db_parts = core.backup_database()
        res = core.backup_files()
        f_size, f_parts = res if res else (None, None)
        return jsonify(ok=True, db_size=db_size, db_parts=db_parts,
                       files_size=f_size, files_parts=f_parts)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/term/screen")
def api_term_screen():
    session_key = request.args.get("session", "")
    return jsonify(ok=True, screen=terminal.get_screen(session_key))


# ==================== WSS 终端（bytes 传输 + 断线无缝） ====================
def _pty_reader(session_key, sid):
    """读取 PTY 输出并推送。断开时不关 fd（保留 bash 和前台进程）"""
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
                    sess.feed(data)
                    socketio.emit("output", data, to=sid)
            else:
                wpid, status = os.waitpid(sess.pid, os.WNOHANG)
                if wpid == sess.pid:
                    socketio.emit("exit", {"code": status}, to=sid)
                    break
    except Exception:
        pass
    # 断开时不关闭 fd、不杀进程，bash 继续存活，重连复用


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


# ==================== 后台线程 ====================
def _backup_loop():
    while True:
        time.sleep(config.BACKUP_INTERVAL)
        if leader and not leader.is_leader:
            return
        try:
            size, parts = core.backup_database()
            print(f"[backup] 数据库已加密上传 {size} 字节 ({parts} 分片)", flush=True)
        except Exception as e:
            print(f"[backup] 数据库备份失败: {e}", flush=True)
        try:
            res = core.backup_files()
            if res:
                size, parts = res
                print(f"[backup] 文件已加密上传 {size} 字节 ({parts} 分片)", flush=True)
        except Exception as e:
            print(f"[backup] 文件备份失败: {e}", flush=True)


def _report_running():
    mgr_host = os.environ.get("MANAGER_HOST", "ghvps.kekeke.cc.cd")
    try:
        url = f"https://{mgr_host}/api/instances/{config.INSTANCE_ID}/report"
        payload = json.dumps({"token": config.EXEC_TOKEN,
                              "url": f"https://{config.TUNNEL_HOST}"}).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Mozilla/5.0 (ghvps-worker)"})
        urllib.request.urlopen(req, timeout=20)
        print(f"[report] 已向 manager 上报 running", flush=True)
    except Exception as e:
        print(f"[report] 上报失败: {e}", flush=True)


def _worker_pre_wake():
    done = False
    while True:
        elapsed = _elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
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
    init_cfg = init_instance()
    os.makedirs(config.FILES_DIR, exist_ok=True)
    JOB_STATE["load_status"] = core.load_or_create()
    _write_shell_profile()
    # 系统瘦身 + 用户 setup（后台执行，不阻塞启动）
    threading.Thread(target=_system_trim, daemon=True).start()
    _run_setup()
    print(f"=== Worker 实例 {config.INSTANCE_ID} 启动 ===", flush=True)
    print(f"=== 固定域名: {config.TUNNEL_HOST} ===", flush=True)
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
    threading.Thread(target=_report_running, daemon=True).start()
    threading.Thread(target=_worker_pre_wake, daemon=True).start()
    terminal.start_cleanup()
    socketio.run(app, host="0.0.0.0", port=config.PORT, allow_unsafe_werkzeug=True)
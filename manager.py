# -*- coding: utf-8 -*-
"""管理实例：账号/实例管理 + 健康监控自动恢复 + API 认证 + 并发控制（纯 API）"""
import os
import time
import json
import functools
import threading
import subprocess
import datetime
import urllib.request
import urllib.error

from flask import Flask, request, jsonify

import config
import core
import accounts
import instances

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()

leader = None
_fail_counts = {}  # inst_id -> 连续失败次数


def _elapsed():
    return int((datetime.datetime.now(datetime.timezone.utc) - core.START_TIME).total_seconds())


# ==================== API 认证 ====================
def _check_token():
    """从请求中提取并校验 token（Authorization Bearer 或 ?token=）"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = (request.args.get("token") or "").strip()
    if not token:
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return False
    return True


def require_auth(f):
    """认证装饰器：除 health 外的接口都要 token"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_token():
            return jsonify(ok=False, error="未授权，请携带 token"), 401
        return f(*args, **kwargs)
    return wrapper


def _api_token_headers():
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {config.EXEC_TOKEN}",
            "User-Agent": "Mozilla/5.0 (ghvps-manager)"}


# ==================== 基础状态 ====================
@app.route("/api/health")
def api_health():
    return jsonify(ok=True, role="manager", job=core.JOB_ID, elapsed=_elapsed(),
                   leader=leader.is_leader if leader else False)


@app.route("/api/status")
@require_auth
def api_status():
    accts = accounts.list_accounts()
    insts = instances.list_instances()
    return jsonify(ok=True, role="manager", job=core.JOB_ID, elapsed=_elapsed(),
                   accounts=accts, instances=insts)


# ==================== 账号管理 ====================
@app.route("/api/accounts", methods=["GET"])
@require_auth
def api_list_accounts():
    return jsonify(ok=True, accounts=accounts.list_accounts())


@app.route("/api/accounts", methods=["POST"])
@require_auth
def api_add_account():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    token = (data.get("token") or "").strip()
    if not name or not token:
        return jsonify(ok=False, error="name 和 token 必填"), 400
    threading.Thread(target=_do_provision, args=(name, token, data), daemon=True).start()
    return jsonify(ok=True, msg=f"账号 {name} 正在全自动配置（fork+secrets），稍后查看 /api/accounts")


def _do_provision(name, token, data):
    res = accounts.auto_provision_account(name, token, repo=data.get("repo"),
                                          max_conc=data.get("max_concurrency"))
    print(f"[account] 添加账号 {name}: {res}", flush=True)


@app.route("/api/accounts/<name>", methods=["DELETE"])
@require_auth
def api_remove_account(name):
    res = accounts.remove_account(name)
    return jsonify(res)


# ==================== 实例管理 ====================
@app.route("/api/instances", methods=["POST"])
@require_auth
def api_create_instance():
    res = instances.create_instance()
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/instances", methods=["GET"])
@require_auth
def api_list_instances():
    return jsonify(ok=True, instances=instances.list_instances())


@app.route("/api/instances/<inst_id>", methods=["GET"])
@require_auth
def api_get_instance(inst_id):
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    return jsonify(ok=True, instance=inst)


@app.route("/api/instances/<inst_id>", methods=["DELETE"])
@require_auth
def api_close_instance(inst_id):
    res = instances.close_instance(inst_id)
    _fail_counts.pop(inst_id, None)
    return jsonify(res)


@app.route("/api/instances/<inst_id>/report", methods=["POST"])
def api_instance_report(inst_id):
    """worker 启动后上报状态（内部接口，token 认证）"""
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    data = request.get_json(silent=True) or {}
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    inst["status"] = "running"
    inst["url"] = data.get("url", inst.get("url"))
    inst["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    all_insts = instances.load_instances()
    for i in all_insts:
        if i["id"] == inst_id:
            i.update(inst)
            break
    instances.save_instances(all_insts)
    _fail_counts.pop(inst_id, None)
    return jsonify(ok=True)


@app.route("/api/instances/<inst_id>/exec", methods=["POST"])
@require_auth
def api_instance_exec(inst_id):
    """在指定实例上执行命令（带超时）"""
    data = request.get_json(silent=True) or {}
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    host = inst.get("hostname")
    if not host:
        return jsonify(ok=False, error="实例无域名"), 404
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    timeout = int(data.get("timeout", 30))
    timeout = max(1, min(timeout, 600))
    payload = json.dumps({"token": config.EXEC_TOKEN, "cmd": cmd, "timeout": timeout}).encode()
    url = f"https://{host}/api/exec"
    try:
        req = urllib.request.Request(url, data=payload, headers=_api_token_headers())
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            return jsonify(ok=True, result=json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return jsonify(ok=False, error=f"实例返回 {e.code}: {body[:200]}"), 502
    except Exception as e:
        return jsonify(ok=False, error=f"无法连接实例: {e}"), 502


# ==================== 健康监控 + 自动恢复 ====================


def _account_suspended(account):
    """检测账号是否被封（GitHub 返回 Account suspended）"""
    try:
        status, d = core.gh_request("GET", "https://api.github.com/user", token=account.get("token"))
        if status == 403:
            msg = d.get("message", "") if isinstance(d, dict) else str(d)
            if "suspended" in str(msg).lower():
                return True
    except Exception:
        pass
    return False


def _auto_cleanup_account(account):
    """账号被封：自动关闭该账号所有实例 + 移除账号"""
    print(f"[auto-cleanup] 检测到账号 {account['name']} 被封，自动清理...", flush=True)
    insts = instances.list_instances()
    for inst in insts:
        if inst.get("account") == account.get("name") and not inst.get("closed"):
            try:
                instances.close_instance(inst["id"])
                print(f"[auto-cleanup] 已关闭实例 {inst['id']}", flush=True)
            except Exception as e:
                print(f"[auto-cleanup] 关闭 {inst['id']} 失败: {e}", flush=True)
    # 移除账号
    try:
        accounts.remove_account(account["name"])
        print(f"[auto-cleanup] 已移除账号 {account['name']}", flush=True)
    except Exception as e:
        print(f"[auto-cleanup] 移除账号失败: {e}", flush=True)


def _check_health(host):
    try:
        req = urllib.request.Request(f"https://{host}/api/health",
                                     headers={"User-Agent": "Mozilla/5.0 (ghvps-monitor)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def _restart_instance(inst):
    """自动重启实例：用账号 token 触发新 worker"""
    account = next((a for a in accounts.load_accounts()
                    if a["name"] == inst.get("account")), None)
    if not account:
        return
    repo = account.get("repo") or config.REPO
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    core.gh_request("POST", url, token=account.get("token"),
                    data={"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
    print(f"[monitor] 实例 {inst['id']} 已自动重启", flush=True)


def _health_monitor_loop():
    """每 60 秒巡检 running 实例，连续失败 3 次自动重启"""
    while True:
        time.sleep(60)
        if not (leader and leader.is_leader):
            continue
        try:
            insts = instances.list_instances()
            changed = False
            for inst in insts:
                if inst.get("status") != "running" or inst.get("closed"):
                    continue
                host = inst.get("hostname")
                if not host:
                    continue
                if _check_health(host):
                    _fail_counts[inst["id"]] = 0
                else:
                    n = _fail_counts.get(inst["id"], 0) + 1
                    _fail_counts[inst["id"]] = n
                    print(f"[monitor] 实例 {inst['id']} 健康检查失败 {n}/3", flush=True)
                    if n >= 3:
                        # 检测该实例账号是否被封
                        account = next((a for a in accounts.load_accounts()
                                        if a["name"] == inst.get("account")), None)
                        if account and _account_suspended(account):
                            # 账号被封：自动清理整个账号
                            _auto_cleanup_account(account)
                            _fail_counts[inst["id"]] = 0
                        else:
                            # 实例异常：自动重启
                            _restart_instance(inst)
                            _fail_counts[inst["id"]] = 0
                            inst["status"] = "restarting"
                        changed = True
            if changed:
                instances.save_instances(insts)
        except Exception as e:
            print(f"[monitor] 巡检异常: {e}", flush=True)


# ==================== 隧道 ====================
def _start_tunnel():
    if not config.TUNNEL_TOKEN:
        print("[tunnel] 无 TUNNEL_TOKEN，跳过", flush=True)
        return
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", config.TUNNEL_TOKEN],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        print(f"[tunnel] 管理实例隧道: https://{config.TUNNEL_HOST}", flush=True)
        for line in proc.stdout:
            line = line.strip()
            if "Registered tunnel connection" in line:
                print("[tunnel] 连接已注册", flush=True)
    except Exception as e:
        print(f"[tunnel] 启动失败: {e}", flush=True)




def _auto_update_loop():
    """自动更新：定期检查主仓库版本，新版本则滚动重启 manager"""
    current_sha = config.CURRENT_SHA
    if not current_sha:
        return
    while True:
        time.sleep(300)
        try:
            url = f"https://api.github.com/repos/{config.MAIN_REPO}/commits/main"
            status, d = core.gh_request("GET", url)
            latest = d.get("sha", "")
            if latest and latest != current_sha:
                print(f"[update] 检测到新版本 {latest[:10]}，触发新 manager", flush=True)
                url2 = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
                core.gh_request("POST", url2, data={"ref": "main"})
                print("[update] 已触发新 manager，60秒后旧 manager 退出", flush=True)
                time.sleep(60)
                os._exit(0)
        except Exception as e:
            print(f"[update] 检查失败: {e}", flush=True)


# ==================== 续命 ====================
def _manager_pre_wake():
    done = False
    while True:
        elapsed = _elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            try:
                url = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
                core.gh_request("POST", url, data={"ref": "main"})
                print(f"[prewake] 已预触发下一个 manager（运行 {elapsed}s）", flush=True)
            except Exception as e:
                print(f"[prewake] 触发失败: {e}", flush=True)
            break
        time.sleep(60)


# ==================== 入口 ====================
def run():
    global leader
    print(f"=== Manager 启动: {core.JOB_ID} ===", flush=True)
    from core import LeaderLock
    leader = LeaderLock()
    leader.acquire()
    if leader.is_leader:
        threading.Thread(target=leader.heartbeat_loop, daemon=True).start()
        threading.Thread(target=_health_monitor_loop, daemon=True).start()
    else:
        threading.Thread(target=leader.follower_loop, args=(lambda: None,), daemon=True).start()
    threading.Thread(target=_manager_pre_wake, daemon=True).start()
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    threading.Thread(target=_start_tunnel, daemon=True).start()
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.PORT, app, threaded=True, use_reloader=False)
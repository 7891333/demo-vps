# -*- coding: utf-8 -*-
"""管理实例：账号/实例管理 + 健康监控自动恢复 + API 认证 + 任务系统 + 日志"""
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
import log
import tasks
import accounts
import instances

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
logger = log.setup_logger("manager")

leader = None
_fail_counts = {}  # inst_id -> 连续失败次数
_worker_heartbeats = {}  # inst_id -> {job_id, last_seen}（内部心跳，不占GitHub配额）


def _elapsed():
    return int((datetime.datetime.now(datetime.timezone.utc) - core.START_TIME).total_seconds())


# ==================== 任务注册 ====================
@tasks.register_handler("add_account")
def _task_add_account(params, task):
    """账号添加任务（幂等，可重试）"""
    logger.info(f"[task] 处理账号添加: {params.get('name')}")
    res = accounts.auto_provision_account(
        params.get("name"), params.get("token"),
        repo=params.get("repo"), max_conc=params.get("max_concurrency"))
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "未知错误"))
    logger.info(f"[task] 账号 {params.get('name')} 配置完成")


# ==================== API 认证 ====================
def _check_token():
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
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_token():
            return jsonify(ok=False, error="未授权，请携带 token"), 401
        return f(*args, **kwargs)
    return wrapper


def _require_leader():
    """写操作要求 leader（防止多 manager 并行写数据冲突）"""
    if not (leader and leader.is_leader):
        return False
    return True


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


@app.route("/api/logs")
@require_auth
def api_logs():
    """查看服务器完整日志"""
    limit = int(request.args.get("limit", 300))
    limit = max(10, min(limit, 2000))
    level = request.args.get("level")
    logs = log.get_logs(limit=limit, level=level)
    return jsonify(ok=True, logs=logs, stats=log.get_stats())


# ==================== 账号管理（任务化） ====================


@app.route("/api/worker/heartbeat", methods=["POST"])
def api_worker_heartbeat():
    """worker 内部心跳（不占 GitHub 配额）"""
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    data = request.get_json(silent=True) or {}
    inst_id = data.get("inst_id", "")
    job_id = data.get("job_id", "")
    if inst_id:
        _worker_heartbeats[inst_id] = {"job_id": job_id, "last_seen": time.time()}
    return jsonify(ok=True)


@app.route("/api/worker/leader")
def api_worker_leader():
    """查询实例的 leader（判断该 worker 是否最新）"""
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    inst_id = request.args.get("inst_id", "")
    job_id = request.args.get("job_id", "")
    hb = _worker_heartbeats.get(inst_id)
    is_leader = bool(hb and hb.get("job_id") == job_id)
    return jsonify(ok=True, is_leader=is_leader, current=hb)

@app.route("/api/accounts", methods=["GET"])
@require_auth
def api_list_accounts():
    return jsonify(ok=True, accounts=accounts.list_accounts())


@app.route("/api/accounts", methods=["POST"])
@require_auth
def api_add_account():
    """添加账号：进入任务队列（持久化+幂等+可恢复），不再丢任务"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    token = (data.get("token") or "").strip()
    if not name or not token:
        return jsonify(ok=False, error="name 和 token 必填"), 400
    # 持久化任务（dedup: 同名任务不重复添加）
    task = tasks.add_task("add_account", {
        "name": name, "token": token,
        "repo": data.get("repo"), "max_concurrency": data.get("max_concurrency"),
    }, dedup_key=f"add_account:{name}")
    logger.info(f"[api] 账号添加任务入队: {name} ({task['id']})")
    return jsonify(ok=True, msg=f"账号 {name} 配置任务已入队（自动执行，可查询 /api/tasks）",
                   task_id=task["id"])


@app.route("/api/tasks", methods=["GET"])
@require_auth
def api_list_tasks():
    """查看任务队列"""
    return jsonify(ok=True, tasks=tasks.load_tasks())


@app.route("/api/accounts/<name>", methods=["DELETE"])
@require_auth
def api_remove_account(name):
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    res = accounts.remove_account(name)
    logger.info(f"[api] 删除账号 {name}: {res}")
    return jsonify(res)


# ==================== 实例管理 ====================
@app.route("/api/instances", methods=["POST"])
@require_auth
def api_create_instance():
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    res = instances.create_instance()
    logger.info(f"[api] 创建实例: {res.get('msg', res.get('error'))}")
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
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    res = instances.close_instance(inst_id)
    _fail_counts.pop(inst_id, None)
    logger.info(f"[api] 关闭实例 {inst_id}: {res.get('msg', res.get('error'))}")
    return jsonify(res)


@app.route("/api/instances/<inst_id>/report", methods=["POST"])
def api_instance_report(inst_id):
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    data = request.get_json(silent=True) or {}
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    inst["status"] = "running"
    inst["url"] = data.get("url", inst.get("url"))
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
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
    logger.warning(f"[auto-cleanup] 账号 {account['name']} 被封，自动清理")
    insts = instances.list_instances()
    for inst in insts:
        if inst.get("account") == account.get("name") and not inst.get("closed"):
            try:
                instances.close_instance(inst["id"])
            except Exception as e:
                logger.error(f"[auto-cleanup] 关闭 {inst['id']} 失败: {e}")
    try:
        accounts.remove_account(account["name"])
    except Exception as e:
        logger.error(f"[auto-cleanup] 移除账号失败: {e}")


def _check_health(host):
    try:
        req = urllib.request.Request(f"https://{host}/api/health",
                                     headers={"User-Agent": "Mozilla/5.0 (ghvps-monitor)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def _restart_instance(inst):
    account = next((a for a in accounts.load_accounts()
                    if a["name"] == inst.get("account")), None)
    if not account:
        return
    repo = account.get("repo") or config.REPO
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    core.gh_request("POST", url, token=account.get("token"),
                    data={"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
    logger.info(f"[monitor] 实例 {inst['id']} 已自动重启")


def _health_monitor_loop():
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
                    logger.warning(f"[monitor] 实例 {inst['id']} 失败 {n}/3")
                    if n >= 3:
                        account = next((a for a in accounts.load_accounts()
                                        if a["name"] == inst.get("account")), None)
                        if account and _account_suspended(account):
                            _auto_cleanup_account(account)
                        else:
                            _restart_instance(inst)
                            inst["status"] = "restarting"
                        _fail_counts[inst["id"]] = 0
                        changed = True
            if changed:
                instances.save_instances(insts)
        except Exception as e:
            logger.error(f"[monitor] 巡检异常: {e}")


# ==================== 隧道 ====================
def _start_tunnel():
    if not config.TUNNEL_TOKEN:
        return
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", config.TUNNEL_TOKEN],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logger.info(f"[tunnel] 管理实例隧道: https://{config.TUNNEL_HOST}")
        for line in proc.stdout:
            line = line.strip()
            if "Registered tunnel connection" in line:
                logger.info("[tunnel] 连接已注册")
    except Exception as e:
        logger.error(f"[tunnel] 启动失败: {e}")


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
                logger.info(f"[prewake] 已预触发下一个 manager（{elapsed}s）")
            except Exception as e:
                logger.error(f"[prewake] 触发失败: {e}")
            break
        time.sleep(60)


def _auto_update_loop():
    current_sha = config.CURRENT_SHA
    if not current_sha:
        return
    while True:
        time.sleep(600)
        try:
            url = f"https://api.github.com/repos/{config.MAIN_REPO}/commits/main"
            status, d = core.gh_request("GET", url)
            latest = d.get("sha", "")
            if latest and latest != current_sha:
                logger.info(f"[update] 检测到新版本 {latest[:10]}，滚动重启")
                url2 = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
                status2, _ = core.gh_request("POST", url2, data={"ref": "main"})
                if status2 not in (200, 204):
                    # 触发失败：继续运行，不退出（防止全挂）
                    logger.error(f"[update] 触发新 manager 失败({status2})，继续运行")
                    continue
                time.sleep(60)
                os._exit(0)
        except Exception as e:
            logger.error(f"[update] 检查失败: {e}")


# ==================== 入口 ====================
def run():
    global leader
    logger.info(f"=== Manager 启动: {core.JOB_ID} ===")
    from core import LeaderLock
    leader = LeaderLock()
    leader.acquire()
    if leader.is_leader:
        threading.Thread(target=leader.heartbeat_loop, daemon=True).start()
        threading.Thread(target=_health_monitor_loop, daemon=True).start()
        # 任务系统：恢复未完成任务 + 启动执行器
        tasks.recover_pending()
        tasks.start_worker()
    else:
        threading.Thread(target=leader.follower_loop, args=(lambda: None,), daemon=True).start()
    threading.Thread(target=_manager_pre_wake, daemon=True).start()
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    threading.Thread(target=_start_tunnel, daemon=True).start()
    # 请求日志
    log.request_logger(app)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.PORT, app, threaded=True, use_reloader=False)
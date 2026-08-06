# -*- coding: utf-8 -*-
"""管理实例：账号管理 + 实例创建/关闭/查询 + 并发控制（纯 API）"""
import os
import time
import threading
import datetime
import urllib.request
import json

from flask import Flask, request, jsonify

import config
import core
import accounts
import instances

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()

leader = None


def _elapsed():
    return int((datetime.datetime.now(datetime.timezone.utc) - core.START_TIME).total_seconds())


# ==================== 基础状态 ====================
@app.route("/api/health")
def api_health():
    return jsonify(ok=True, role="manager", job=core.JOB_ID, elapsed=_elapsed(),
                   leader=leader.is_leader if leader else False)


@app.route("/api/status")
def api_status():
    accts = accounts.list_accounts()
    insts = instances.list_instances()
    return jsonify(ok=True, role="manager", job=core.JOB_ID, elapsed=_elapsed(),
                   accounts=accts, instances=insts)


# ==================== 账号管理 ====================
@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    return jsonify(ok=True, accounts=accounts.list_accounts())


@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    token = (data.get("token") or "").strip()
    if not name or not token:
        return jsonify(ok=False, error="name 和 token 必填"), 400
    res = accounts.add_account(name, token,
                               repo=data.get("repo"),
                               max_conc=data.get("max_concurrency"))
    return jsonify(res)


@app.route("/api/accounts/<name>", methods=["DELETE"])
def api_remove_account(name):
    res = accounts.remove_account(name)
    return jsonify(res)


# ==================== 实例管理 ====================
@app.route("/api/instances", methods=["POST"])
def api_create_instance():
    res = instances.create_instance()
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/instances", methods=["GET"])
def api_list_instances():
    return jsonify(ok=True, instances=instances.list_instances())


@app.route("/api/instances/<inst_id>", methods=["GET"])
def api_get_instance(inst_id):
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    return jsonify(ok=True, instance=inst)


@app.route("/api/instances/<inst_id>", methods=["DELETE"])
def api_close_instance(inst_id):
    res = instances.close_instance(inst_id)
    return jsonify(res)


@app.route("/api/instances/<inst_id>/exec", methods=["POST"])
def api_instance_exec(inst_id):
    """在指定实例上执行命令（带超时）"""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return jsonify(ok=False, error="未授权"), 403
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
    # 转发到目标实例
    payload = json.dumps({"token": token, "cmd": cmd, "timeout": timeout}).encode()
    url = f"https://{host}/api/exec"
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            return jsonify(ok=True, result=json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return jsonify(ok=False, error=f"实例返回 {e.code}: {body[:200]}"), 502
    except Exception as e:
        return jsonify(ok=False, error=f"无法连接实例: {e}"), 502


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
    else:
        threading.Thread(target=leader.follower_loop, args=(lambda: None,), daemon=True).start()
    threading.Thread(target=_manager_pre_wake, daemon=True).start()
    # 用 werkzeug 运行（生产可换 gunicorn）
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.PORT, app, threaded=True, use_reloader=False)
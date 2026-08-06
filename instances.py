# -*- coding: utf-8 -*-
"""实例管理：创建/关闭/查询工作实例，并发检测与自动分配"""
import json
import time
import datetime

import config
import core
import tunnels
import accounts


# ==================== 实例清单（管理仓库 Releases） ====================
def load_instances(token=None):
    data = core.load_json_enc(config.ASSET_INSTANCES, token=token, default=[])
    return data if isinstance(data, list) else []


def save_instances(instances, token=None):
    core.save_json_enc(config.ASSET_INSTANCES, instances, token=token)


def _next_inst_id(instances):
    """生成下一个实例 ID（inst1, inst2...）"""
    nums = []
    for inst in instances:
        try:
            nums.append(int(inst["id"].replace("inst", "")))
        except Exception:
            pass
    return f"inst{max(nums) + 1 if nums else 1}"


# ==================== 账号仓库操作 ====================
def _account_repo_url(repo, path):
    return f"https://api.github.com/repos/{repo}{path}"


def _save_instance_config(account, inst_id, payload):
    """把实例配置（含 tunnel token）加密存到账号仓库 Releases"""
    repo = account["repo"]
    token = account["token"]
    asset_name = f"inst-{inst_id}.json.enc"
    url = _account_repo_url(repo, f"/releases/tags/{config.BACKUP_TAG}")
    status, d = core.gh_request("GET", url, token=token)
    if status != 200:
        core.gh_request("POST", _account_repo_url(repo, "/releases"), token=token,
                        data={"tag_name": config.BACKUP_TAG, "name": "实例配置", "body": ""})
        status, d = core.gh_request("GET", url, token=token)
    rel_id = d["id"]
    for a in d.get("assets", []):
        if a.get("name") == asset_name:
            core.gh_request("DELETE", _account_repo_url(repo, f"/releases/assets/{a['id']}"), token=token)
    enc = core.encrypt(json.dumps(payload).encode())
    up_url = f"https://uploads.github.com/repos/{repo}/releases/{rel_id}/assets?name={asset_name}"
    core.gh_request("POST", up_url, token=token, data=enc,
                    headers={"Content-Type": "application/octet-stream"})


def _trigger_worker(account, inst_id):
    """用账号 token 触发该账号仓库的 worker workflow"""
    repo = account["repo"]
    url = _account_repo_url(repo, f"/actions/workflows/{config.WORKER_WORKFLOW}/dispatches")
    status, d = core.gh_request("POST", url, token=account["token"],
                                data={"ref": "main", "inputs": {"INSTANCE_ID": inst_id}})
    if status not in (200, 204):
        raise RuntimeError(f"触发 worker 失败: {status} {d}")
    time.sleep(4)
    runs_url = _account_repo_url(repo, "/actions/runs?per_page=1")
    status, d = core.gh_request("GET", runs_url, token=account["token"])
    run_id = None
    if status == 200 and d.get("workflow_runs"):
        run_id = d["workflow_runs"][0]["id"]
    return run_id


def _cancel_worker(account, run_id):
    if not run_id:
        return
    repo = account["repo"]
    url = _account_repo_url(repo, f"/actions/runs/{run_id}/cancel")
    core.gh_request("POST", url, token=account["token"])


# ==================== 创建实例 ====================
def create_instance(manager_token=None):
    """
    创建新工作实例（全自动）：
    选账号 → 同步fork最新代码 → 建隧道 → 存配置 → 触发 worker → 记录清单
    """
    # 1. 负载均衡选账号
    sel = accounts.select_best_account(token=manager_token, workflow=config.WORKER_WORKFLOW)
    if not sel:
        return {"ok": False, "error": "所有账号并发已满，请稍后再试"}
    account, running = sel

    instances = load_instances(token=manager_token)
    inst_id = _next_inst_id(instances)
    hostname = f"{inst_id}.{config.BASE_DOMAIN}"

    # 2. 同步账号 fork 仓库到最新（保证新实例用最新代码）
    try:
        accounts.sync_fork(account)
        time.sleep(2)
    except Exception as e:
        print(f"[create] fork 同步异常（继续）: {e}", flush=True)

    # 3. 创建 CF 隧道
    try:
        tunnel_id, tunnel_token = tunnels.create_tunnel(hostname)
    except Exception as e:
        return {"ok": False, "error": f"创建隧道失败: {e}"}

    # 4. 存实例配置到账号仓库
    try:
        _save_instance_config(account, inst_id, {
            "inst_id": inst_id,
            "hostname": hostname,
            "tunnel_token": tunnel_token,
            "tunnel_id": tunnel_id,
            "account": account["name"],
        })
    except Exception as e:
        tunnels.delete_tunnel(tunnel_id, hostname)
        return {"ok": False, "error": f"保存实例配置失败: {e}"}

    # 5. 触发 worker
    try:
        run_id = _trigger_worker(account, inst_id)
    except Exception as e:
        tunnels.delete_tunnel(tunnel_id, hostname)
        return {"ok": False, "error": f"触发 worker 失败: {e}"}

    # 6. 记录实例清单
    inst = {
        "id": inst_id,
        "hostname": hostname,
        "account": account["name"],
        "account_repo": account["repo"],
        "tunnel_id": tunnel_id,
        "run_id": run_id,
        "status": "starting",
        "url": f"https://{hostname}",
        "closed": False,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    instances.append(inst)
    save_instances(instances, token=manager_token)

    return {"ok": True, "instance": inst, "msg": f"实例 {inst_id} 创建中，地址 https://{hostname}"}


# ==================== 关闭实例 ====================
def close_instance(inst_id, manager_token=None):
    instances = load_instances(token=manager_token)
    inst = next((i for i in instances if i["id"] == inst_id), None)
    if not inst:
        return {"ok": False, "error": f"实例 {inst_id} 不存在"}

    account = next((a for a in accounts.load_accounts(token=manager_token)
                    if a["name"] == inst.get("account")), None)
    if account:
        _cancel_worker(account, inst.get("run_id"))
        try:
            asset_name = f"inst-{inst_id}.json.enc"
            url = _account_repo_url(account["repo"], f"/releases/tags/{config.BACKUP_TAG}")
            status, d = core.gh_request("GET", url, token=account["token"])
            if status == 200:
                for a in d.get("assets", []):
                    if a.get("name") == asset_name:
                        core.gh_request("DELETE", _account_repo_url(account["repo"], f"/releases/assets/{a['id']}"), token=account["token"])
        except Exception:
            pass

    try:
        tunnels.delete_tunnel(inst.get("tunnel_id"), inst.get("hostname"))
    except Exception:
        pass

    inst["closed"] = True
    inst["status"] = "closed"
    save_instances(instances, token=manager_token)
    return {"ok": True, "msg": f"实例 {inst_id} 已关闭"}


# ==================== 查询实例 ====================
def list_instances(manager_token=None):
    return load_instances(token=manager_token)


def get_instance(inst_id, manager_token=None):
    instances = load_instances(token=manager_token)
    return next((i for i in instances if i["id"] == inst_id), None)


def worker_report(inst_id, url, manager_token=None):
    """worker 启动后上报 URL 和状态"""
    instances = load_instances(token=manager_token)
    inst = next((i for i in instances if i["id"] == inst_id), None)
    if inst:
        inst["url"] = url
        inst["status"] = "running"
        inst["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_instances(instances, token=manager_token)
    return {"ok": True}
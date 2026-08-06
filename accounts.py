# -*- coding: utf-8 -*-
"""多账号管理：配置存储 + 负载均衡 + 全自动创建（fork+配secrets）+ fork自动同步"""
import json
import time
import base64

from nacl.public import PublicKey, SealedBox

import config
import core


def load_accounts(token=None):
    """从 Releases 读取账号配置（加密），返回账号列表"""
    data = core.load_json_enc(config.ASSET_ACCOUNTS, token=token, default=[])
    return data if isinstance(data, list) else []


def save_accounts(accounts, token=None):
    """保存账号配置（加密）"""
    core.save_json_enc(config.ASSET_ACCOUNTS, accounts, token=token)


def add_account(name, gh_token, repo=None, max_conc=None, token=None):
    """添加账号（仅报备，不自动创建仓库）"""
    accounts = load_accounts(token=token)
    for a in accounts:
        if a.get("name") == name:
            a["token"] = gh_token
            if repo:
                a["repo"] = repo
            if max_conc:
                a["max_concurrency"] = max_conc
            save_accounts(accounts, token=token)
            return {"ok": True, "msg": f"账号 {name} 已更新"}
    accounts.append({
        "name": name,
        "token": gh_token,
        "repo": repo or config.REPO,
        "max_concurrency": max_conc or config.DEFAULT_MAX_CONCURRENCY,
    })
    save_accounts(accounts, token=token)
    return {"ok": True, "msg": f"账号 {name} 已添加"}


def remove_account(name, token=None):
    accounts = load_accounts(token=token)
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return {"ok": False, "msg": f"账号 {name} 不存在"}
    save_accounts(new, token=token)
    return {"ok": True, "msg": f"账号 {name} 已删除"}


def list_accounts(token=None):
    """返回账号列表（脱敏，不显示完整 token）"""
    accounts = load_accounts(token=token)
    result = []
    for a in accounts:
        tok = a.get("token", "")
        masked = (tok[:6] + "***" + tok[-4:]) if len(tok) > 12 else "***"
        result.append({
            "name": a.get("name"),
            "token_masked": masked,
            "repo": a.get("repo"),
            "max_concurrency": a.get("max_concurrency"),
        })
    return result


# ==================== fork 自动同步 ====================
def sync_fork(account):
    """
    把账号 fork 仓库同步到上游最新（merge-upstream）。
    返回 True/False
    """
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        if repo == config.REPO:
            return True  # 主仓库无需同步
        url = f"https://api.github.com/repos/{repo}/merge-upstream"
        status, d = core.gh_request("POST", url, token=token, data={"branch": "main"})
        ok = status in (200, 201)
        if ok:
            print(f"[sync] 已同步 {repo} 到上游最新", flush=True)
        else:
            print(f"[sync] {repo} 同步状态: {status} {d.get('message','')}", flush=True)
        return ok
    except Exception as e:
        print(f"[sync] 同步失败: {e}", flush=True)
        return False


# ==================== GitHub Secrets 配置（libsodium sealed box） ====================
def _set_repo_secret(account_token, repo, secret_name, secret_value):
    """用 GitHub API 配置仓库 secret（libsodium sealed box 加密）"""
    try:
        url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
        status, d = core.gh_request("GET", url, token=account_token)
        if status != 200:
            return False
        key = d["key"]
        key_id = d["key_id"]
        pub = PublicKey(base64.b64decode(key))
        sealed = SealedBox(pub)
        encrypted = sealed.encrypt(str(secret_value).encode())
        encrypted_b64 = base64.b64encode(encrypted).decode()
        url = f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}"
        status, _ = core.gh_request("PUT", url, token=account_token,
                                    data={"encrypted_value": encrypted_b64, "key_id": key_id})
        return status in (200, 201, 204)
    except Exception as e:
        print(f"[secrets] 配置 {secret_name} 失败: {e}", flush=True)
        return False


def _ensure_repo(account_token, repo_name):
    """确保账号有仓库（不存在则 fork 主仓库），返回完整 repo 名"""
    full = f"{repo_name}"
    status, _ = core.gh_request("GET", f"https://api.github.com/repos/{full}", token=account_token)
    if status == 200:
        return full, True
    print(f"[repo] 账号无仓库，fork 主仓库...", flush=True)
    status, d = core.gh_request("POST", f"https://api.github.com/repos/{config.REPO}/forks",
                                token=account_token, data={"default_branch_only": True})
    if status not in (200, 202):
        return None, False
    for _ in range(60):
        time.sleep(5)
        status, _ = core.gh_request("GET", f"https://api.github.com/repos/{full}", token=account_token)
        if status == 200:
            print(f"[repo] fork 完成: {full}", flush=True)
            return full, True
    return None, False


def auto_provision_account(name, account_token, repo=None, max_conc=None, manager_token=None):
    """
    全自动创建账号：
    ① 验证 token → ② 确保仓库（自动 fork）→ ③ 同步最新代码 → ④ 配置 secrets → ⑤ 报备
    """
    status, user = core.gh_request("GET", "https://api.github.com/user", token=account_token)
    if status != 200:
        return {"ok": False, "error": f"token 无效（{status}）"}
    login = user.get("login", "")

    if not repo:
        repo = f"{login}/{config.REPO.split('/')[-1]}"
    repo, ok = _ensure_repo(account_token, repo)
    if not ok:
        return {"ok": False, "error": "仓库准备失败（fork 超时或失败）"}

    # 同步最新代码（fork 仓库）
    acc = {"repo": repo, "token": account_token}
    sync_fork(acc)
    time.sleep(3)  # 等同步完成

    ok1 = _set_repo_secret(account_token, repo, "GH_TOKEN", account_token)
    ok2 = _set_repo_secret(account_token, repo, "DEMO_KEY", config.DEMO_KEY)
    ok3 = _set_repo_secret(account_token, repo, "EXEC_TOKEN", config.EXEC_TOKEN)
    if not (ok1 and ok2 and ok3):
        return {"ok": False, "error": "secrets 配置失败"}

    return add_account(name, account_token, repo=repo, max_conc=max_conc, token=manager_token)


# ==================== 负载均衡 ====================
def _account_usage(account, workflow=None):
    """查询账号【自己仓库】当前 worker 运行数（并发检测）"""
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        url = f"https://api.github.com/repos/{repo}/actions/runs?status=in_progress&per_page=100"
        status, data = core.gh_request("GET", url, token=token)
        if status != 200:
            return 0
        runs = data.get("workflow_runs", [])
        if workflow:
            return sum(1 for r in runs if workflow in r.get("path", ""))
        return len(runs)
    except Exception:
        return 0


def select_best_account(token=None, workflow=None):
    """负载均衡：选并发余量最大的账号。返回 (account, running) 或 None"""
    accounts = load_accounts(token=token)
    if not accounts:
        return None
    best = None
    for acc in accounts:
        running = _account_usage(acc, workflow=workflow)
        max_c = acc.get("max_concurrency", config.DEFAULT_MAX_CONCURRENCY)
        if running >= max_c:
            continue
        if best is None or (max_c - running) > (best["max_concurrency"] - best["running"]):
            best = {"account": acc, "running": running, "max_concurrency": max_c}
    if best is None:
        return None
    return best["account"], best["running"]
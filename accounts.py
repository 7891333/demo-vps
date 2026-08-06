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
    """保存账号配置（防数据丢失：空数据不覆盖已有数据）"""
    if not accounts:
        existing = load_accounts(token=token)
        if existing:
            print("[protect] 拒绝空数据覆盖账号配置", flush=True)
            return
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
    """用 GitHub API 配置仓库 secret（libsodium sealed box 加密），幂等：已配则跳过"""
    try:
        # 检查是否已配置
        chk = core.gh_request("GET", f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
                              token=account_token)
        if chk[0] == 200 and isinstance(chk[1], dict) and chk[1].get("name"):
            return True  # 已配置，跳过
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




def _wait_workflow_ready(account_token, repo, workflow="worker.yml", timeout=120):
    """fork 后等待 workflow 被 GitHub 注册；超时则推送空 commit 触发扫描"""
    import time
    import base64
    url = f"https://api.github.com/repos/{repo}/actions/workflows"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, d = core.gh_request("GET", url, token=account_token)
            if status == 200:
                paths = [w.get("path", "") for w in d.get("workflows", [])]
                if any(workflow in p for p in paths):
                    return True
        except Exception:
            pass
        # 前 60 秒轮询；60 秒后推送空 commit 触发扫描（fork 后 GitHub 可能不主动扫描）
        if time.time() > deadline - 60:
            try:
                # 获取 README 并更新触发 push
                rd = core.gh_request("GET", f"https://api.github.com/repos/{repo}/contents/README.md",
                                     token=account_token)
                if rd[0] == 200 and isinstance(rd[1], dict) and rd[1].get("sha"):
                    sha = rd[1]["sha"]
                    content = rd[1].get("content", "")
                    new_content = base64.b64encode(
                        (base64.b64decode(content).decode(errors="replace") + "\n").encode()).decode()
                    core.gh_request("PUT", f"https://api.github.com/repos/{repo}/contents/README.md",
                                    token=account_token,
                                    data={"message": "trigger workflow scan",
                                          "content": new_content, "sha": sha})
                    print("[repo] 已推送空 commit 触发 workflow 扫描", flush=True)
            except Exception:
                pass
            deadline = time.time() + timeout  # 重置超时（等待扫描生效）
        time.sleep(5)
    return False


def _check_repo_secret(account_token, repo, secret_name):
    """检查仓库 secret 是否已配置（幂等）"""
    url = f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}"
    status, _ = core.gh_request("GET", url, token=account_token)
    return status == 200


def auto_provision_account(name, account_token, repo=None, max_conc=None, manager_token=None):
    """
    全自动创建账号（幂等，可重复执行）：
    ① 验证 token → ② 确保仓库（存在则跳过）→ ③ 同步代码 → ④ 等待 workflow
    → ⑤ 配 secrets（已配跳过）→ ⑥ 报备（已报备更新）
    """
    # ① 验证 token
    status, user = core.gh_request("GET", "https://api.github.com/user", token=account_token)
    if status != 200:
        return {"ok": False, "error": f"token 无效（{status}）"}
    login = user.get("login", "")

    # ② 确保仓库（幂等：已存在则跳过 fork）
    if not repo:
        repo = f"{login}/{config.REPO.split('/')[-1]}"
    repo, ok = _ensure_repo(account_token, repo)
    if not ok:
        return {"ok": False, "error": "仓库准备失败（fork 超时或失败）"}

    # ③ 同步最新代码（幂等）
    acc = {"repo": repo, "token": account_token}
    sync_fork(acc)
    time.sleep(3)

    # ④ 等待 workflow 注册（关键；已注册则快速通过）
    if not _wait_workflow_ready(account_token, repo):
        return {"ok": False, "error": "workflow 注册超时（稍后自动重试）"}

    # ⑤ 配 secrets（幂等：已配置的跳过）
    needed = {"GH_TOKEN": account_token, "DEMO_KEY": config.DEMO_KEY,
              "EXEC_TOKEN": config.EXEC_TOKEN}
    all_ok = True
    for sname, sval in needed.items():
        if not _check_repo_secret(account_token, repo, sname):
            if not _set_repo_secret(account_token, repo, sname, sval):
                all_ok = False
                print(f"[secrets] {sname} 配置失败", flush=True)
    if not all_ok:
        return {"ok": False, "error": "secrets 配置失败（将自动重试）"}

    # ⑥ 报备（幂等：已报备则更新）
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
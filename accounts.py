# -*- coding: utf-8 -*-
"""多账号管理：配置存储 + 负载均衡选择"""
import json

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
    """添加账号。gh_token 为该账号的 GitHub token"""
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
            # path 形如 ".github/workflows/worker.yml"，用包含匹配
            return sum(1 for r in runs if workflow in r.get("path", ""))
        return len(runs)
    except Exception:
        return 0


def select_best_account(token=None, workflow=None):
    """负载均衡：选并发余量最大的账号。返回 (account, running) 或 None"""
    accounts = load_accounts(token=token)
    if not accounts:
        return None
    best = None  # {"account", "running", "max_concurrency"}
    for acc in accounts:
        running = _account_usage(acc, workflow=workflow)
        max_c = acc.get("max_concurrency", config.DEFAULT_MAX_CONCURRENCY)
        if running >= max_c:
            continue  # 该账号已满
        if best is None or (max_c - running) > (best["max_concurrency"] - best["running"]):
            best = {"account": acc, "running": running, "max_concurrency": max_c}
    if best is None:
        return None
    return best["account"], best["running"]
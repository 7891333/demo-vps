# -*- coding: utf-8 -*-
"""
帝国自动更新脚本：主仓库 push 后自动执行
① 同步所有账号 fork 仓库到最新
② 触发所有 running worker 滚动重启（无缝）
③ 触发新 manager
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import core


def gh_request(method, url, token, data=None):
    """GitHub API 请求，返回 (status, body)"""
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json",
         "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)


def main():
    print("=== 帝国自动更新启动 ===", flush=True)
    accounts = core.load_json_enc(config.ASSET_ACCOUNTS, default=[])
    instances = core.load_json_enc(config.ASSET_INSTANCES, default=[])
    print(f"账号数: {len(accounts)} | 实例数: {len(instances)}", flush=True)

    # 1. 同步所有 fork
    print("--- 同步所有 fork ---", flush=True)
    for acc in accounts:
        repo = acc.get("repo") or config.REPO
        if repo == config.REPO:
            continue
        url = f"https://api.github.com/repos/{repo}/merge-upstream"
        status, _ = gh_request("POST", url, acc.get("token"), {"branch": "main"})
        print(f"  {repo}: HTTP {status}", flush=True)
        time.sleep(2)

    # 2. 滚动重启所有 running worker
    print("--- 滚动重启 worker ---", flush=True)
    running = [i for i in instances if i.get("status") == "running" and not i.get("closed")]
    for inst in running:
        acc = next((a for a in accounts if a["name"] == inst.get("account")), None)
        if not acc:
            continue
        repo = acc.get("repo") or config.REPO
        url = f"https://api.github.com/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
        status, _ = gh_request("POST", url, acc.get("token"),
                               {"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
        print(f"  {inst['id']} 触发重启: HTTP {status}", flush=True)
        time.sleep(3)

    # 3. 触发新 manager
    print("--- 更新 manager ---", flush=True)
    url = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
    status, _ = gh_request("POST", url, config.GH_TOKEN, {"ref": "main"})
    print(f"  manager 触发: HTTP {status}", flush=True)

    print("=== 帝国自动更新完成 ===", flush=True)


if __name__ == "__main__":
    main()
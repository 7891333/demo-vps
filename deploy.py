#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键无缝部署脚本：推送代码 → 触发新 job（不取消旧）→ 监控就绪

用法：
  python3 deploy.py [--workflow manager|worker] [--repo owner/repo] [--token ghp_xxx]
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error
import subprocess


def run(cmd):
    """本地执行命令"""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def api(method, url, token, data=None, timeout=30):
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    if data is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    else:
        body = None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
            try:
                return r.status, json.loads(content.decode() or "null")
            except Exception:
                return r.status, content.decode()
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content.decode() or "null")
        except Exception:
            return e.code, content.decode(errors="replace")


def main_flow():
    repo = os.environ.get("REPO", "7891333/demo-vps")
    token = os.environ.get("GH_TOKEN", "")
    workflow = os.environ.get("WORKFLOW", "manager.yml")
    if len(sys.argv) > 1 and sys.argv[1] in ("manager", "worker"):
        workflow = sys.argv[1] + ".yml"
    if not token:
        print("❌ 需要 GH_TOKEN（环境变量）")
        sys.exit(1)

    print(f"=== 部署 {workflow} 到 {repo} ===")

    # 1. 推送本地代码
    print("[1/4] 推送代码...")
    code, out, err = run("cd " + os.path.expanduser("~/demo-vps") +
                          " && git add -A && git commit -m 'deploy update' --allow-empty 2>&1 | tail -1"
                          " && git fetch origin && git pull --rebase origin main 2>&1 | tail -1"
                          " && git push origin main 2>&1 | tail -1")
    print(out.strip() or err.strip())

    # 2. 触发新 workflow（不取消旧 job → 无缝）
    print("[2/4] 触发新 workflow...")
    wf = workflow
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches"
    status, d = api("POST", url, token, data={"ref": "main"})
    if status not in (200, 204):
        print(f"❌ 触发失败: {status} {d}")
        sys.exit(1)
    print("✅ 已触发，等待启动...")

    # 3. 等待新 run 出现
    print("[3/4] 等待新 job 启动...")
    new_run = None
    for _ in range(30):
        time.sleep(5)
        status, d = api("GET", f"https://api.github.com/repos/{repo}/actions/runs?per_page=3", token)
        if status == 200:
            runs = d.get("workflow_runs", [])
            if runs and runs[0].get("path") == wf and runs[0].get("status") == "in_progress":
                new_run = runs[0]
                break
    if not new_run:
        print("⚠️ 未检测到新 run，请检查")
        sys.exit(1)
    print(f"✅ 新 run: {new_run['id']}")

    # 4. 等待服务就绪（health check）
    print("[4/4] 等待服务就绪...")
    host = os.environ.get("DEPLOY_HOST", "ghvps.kekeke.cc.cd")
    base = f"https://{host}"
    ready = False
    for i in range(40):
        time.sleep(5)
        try:
            req = urllib.request.Request(base + "/api/health")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            pass
    if ready:
        print(f"✅ 部署完成！服务在线：{base}")
    else:
        print("⚠️ 服务未检测到就绪（新 job 可能还在启动），请稍后检查")

    print("\n📌 提示：旧 job 会自动交接（leader 锁），无需手动取消。")
    print("📌 查看运行: https://github.com/7891333/demo-vps/actions")


if __name__ == "__main__":
    main_flow()
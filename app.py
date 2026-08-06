# -*- coding: utf-8 -*-
"""应用入口：启动各服务线程并运行 Web 服务"""
import os
import threading

import config
import core
import web
from core import LeaderLock

# ==================== 启动日志 ====================
print(f"=== Job ID: {core.JOB_ID} ===", flush=True)
print(f"=== 仓库: {config.REPO} ===", flush=True)
print(f"=== 固定域名: {config.TUNNEL_HOST} ===", flush=True)
for name, ok in [("DEMO_KEY", config.DEMO_KEY), ("GH_TOKEN", config.GH_TOKEN),
                 ("EXEC_TOKEN", config.EXEC_TOKEN), ("TUNNEL_TOKEN", config.TUNNEL_TOKEN)]:
    if not ok:
        print(f"[warn] {name} 未设置", flush=True)

# ==================== 数据恢复 ====================
os.makedirs(config.FILES_DIR, exist_ok=True)
web.JOB_STATE["load_status"] = core.load_or_create()

# ==================== 备份线程（leader 专用） ====================
def _backup_loop():
    while True:
        import time
        time.sleep(config.BACKUP_INTERVAL)
        if not lock.is_leader:
            return
        try:
            size, status = core.backup_database()
            print(f"[backup] 数据库已加密上传 {size} 字节 (HTTP {status})", flush=True)
        except Exception as e:
            print(f"[backup] 数据库备份失败: {e}", flush=True)
        try:
            res = core.backup_files()
            if res:
                size, status = res
                print(f"[backup] 文件已加密上传 {size} 字节 (HTTP {status})", flush=True)
        except Exception as e:
            print(f"[backup] 文件备份失败: {e}", flush=True)


# ==================== 主 job 锁 ====================
lock = LeaderLock()
lock.acquire()

# ==================== 后台线程 ====================
if lock.is_leader:
    threading.Thread(target=_backup_loop, daemon=True).start()
    threading.Thread(target=lock.heartbeat_loop, daemon=True).start()
else:
    def _on_promote():
        web.JOB_STATE["load_status"] = core.load_or_create()
        threading.Thread(target=_backup_loop, daemon=True).start()

    threading.Thread(target=lock.follower_loop, args=(_on_promote,), daemon=True).start()

# ==================== 隧道 & 无缝衔接 ====================
def _on_tunnel_url(url):
    web.JOB_STATE["last_url"] = url
    core.report_url(url)


threading.Thread(target=core.start_tunnel, args=(_on_tunnel_url,), daemon=True).start()
threading.Thread(target=core.pre_wake_loop, daemon=True).start()

# ==================== 启动 Web ====================
app, socketio = web.create_app(lock)
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=config.PORT, allow_unsafe_werkzeug=True)
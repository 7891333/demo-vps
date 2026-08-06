# -*- coding: utf-8 -*-
"""核心模块：加密、GitHub API、数据/文件存储、主job锁、隧道、URL上报"""
import io
import os
import json
import time
import uuid
import base64
import tarfile
import threading
import datetime
import subprocess
import urllib.request
import urllib.error

from Crypto.Cipher import AES

import config


# ==================== 加密工具 ====================
def encrypt(data: bytes) -> bytes:
    """AES-256-GCM 加密，返回 nonce+tag+ciphertext"""
    key = bytes.fromhex(config.DEMO_KEY)
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + ciphertext


def decrypt(blob: bytes) -> bytes:
    """AES-256-GCM 解密，校验完整性"""
    key = bytes.fromhex(config.DEMO_KEY)
    nonce, tag, ciphertext = blob[:16], blob[16:32], blob[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)


# ==================== GitHub API ====================
def gh_request(method, url, data=None, headers=None, raw=False, timeout=60):
    """通用 GitHub API 请求，返回 (status, body)"""
    h = {"Authorization": f"token {config.GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, headers=h)
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            content = r.read()
            if raw:
                return r.status, content
            return r.status, json.loads(content.decode() or "null")
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content.decode() or "null")
        except Exception:
            return e.code, content.decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def get_release():
    """获取 backup release，不存在返回 None"""
    url = f"https://api.github.com/repos/{config.REPO}/releases/tags/{config.BACKUP_TAG}"
    status, data = gh_request("GET", url)
    return data if status == 200 else None


def ensure_release():
    """确保 backup release 存在，返回 release id"""
    rel = get_release()
    if rel:
        return rel["id"]
    url = f"https://api.github.com/repos/{config.REPO}/releases"
    data = {
        "tag_name": config.BACKUP_TAG,
        "name": "加密备份",
        "body": "AES-256-GCM 加密的数据库+文件备份（自动生成）",
        "draft": False,
        "prerelease": False,
    }
    status, d = gh_request("POST", url, data=data)
    if status in (200, 201):
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def _delete_asset(name):
    rel = get_release()
    if rel:
        for a in rel.get("assets", []):
            if a.get("name") == name:
                gh_request("DELETE", f"https://api.github.com/repos/{config.REPO}/releases/assets/{a['id']}")


def upload_asset(name: str, data_bytes: bytes):
    """上传/覆盖 asset，返回 (大小, HTTP状态)。必须用 uploads.github.com 域名"""
    rel_id = ensure_release()
    _delete_asset(name)
    url = f"https://uploads.github.com/repos/{config.REPO}/releases/{rel_id}/assets?name={name}"
    status, _ = gh_request("POST", url, data=data_bytes, headers={"Content-Type": "application/octet-stream"})
    return len(data_bytes), status


def download_asset(name: str):
    """下载 asset 内容，不存在返回 None。必须带 Accept: application/octet-stream 头"""
    rel = get_release()
    if not rel:
        return None
    for a in rel.get("assets", []):
        if a.get("name") == name:
            status, blob = gh_request(
                "GET", f"https://api.github.com/repos/{config.REPO}/releases/assets/{a['id']}",
                raw=True, headers={"Accept": "application/octet-stream"},
            )
            return blob if status == 200 else None
    return None


# ==================== 数据/文件 存储 ====================
def create_new_db():
    conn = _db_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
        (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()


def _db_conn():
    import sqlite3
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def load_or_create() -> str:
    """从 Releases 恢复数据库+文件，返回加载状态描述"""
    status = "新建初始数据库"
    blob = download_asset(config.ASSET_DB)
    if blob:
        try:
            data = decrypt(blob)
            with open(config.DB_FILE, "wb") as f:
                f.write(data)
            status = f"从 Releases 恢复加密备份（{len(data)} 字节）"
        except Exception as e:
            print(f"[load] 数据库解密失败，改用新库: {e}", flush=True)
            create_new_db()
    else:
        create_new_db()
    restore_files()
    return status


def backup_database():
    """备份数据库，返回 (大小, HTTP状态)"""
    with open(config.DB_FILE, "rb") as f:
        data = f.read()
    return upload_asset(config.ASSET_DB, encrypt(data))


def backup_files():
    """备份 files/ 目录，无目录返回 None"""
    if not os.path.isdir(config.FILES_DIR):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(config.FILES_DIR, arcname="files")
    return upload_asset(config.ASSET_FILES, encrypt(buf.getvalue()))


def restore_files():
    """还原 files/ 目录（解包到主目录 ~/files）"""
    blob = download_asset(config.ASSET_FILES)
    if not blob:
        return
    try:
        data = decrypt(blob)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(path=os.path.expanduser("~"))
        os.makedirs(config.FILES_DIR, exist_ok=True)
        print(f"[files] 已恢复文件目录（{len(data)} 字节）", flush=True)
    except Exception as e:
        print(f"[files] 恢复失败: {e}", flush=True)


# ==================== 主 job 锁 ====================
class LeaderLock:
    """基于 Releases leader.json 心跳的分布式锁"""

    def __init__(self):
        self.is_leader = False

    def _read(self):
        blob = download_asset(config.ASSET_LEADER)
        if not blob:
            return None
        try:
            return json.loads(blob.decode())
        except Exception:
            return None

    def _heartbeat(self):
        data = json.dumps({"job_id": JOB_ID, "heartbeat": time.time()}).encode()
        upload_asset(config.ASSET_LEADER, data)

    def acquire(self):
        leader = self._read()
        now = time.time()
        if leader and leader.get("job_id") != JOB_ID and (now - leader.get("heartbeat", 0)) < config.HEARTBEAT_TIMEOUT:
            self.is_leader = False
            print(f"[leader] 已有活跃 leader: {leader.get('job_id')}，本 job 为 follower（只读）", flush=True)
            return False
        self.is_leader = True
        self._heartbeat()
        print(f"[leader] 本 job 成为 leader: {JOB_ID}", flush=True)
        return True

    def heartbeat_loop(self):
        while True:
            if not self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                self._heartbeat()
            except Exception as e:
                print(f"[leader] 心跳失败: {e}", flush=True)

    def follower_loop(self, on_promote):
        """follower 检测心跳过期后升级为 leader"""
        while True:
            if self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                leader = self._read()
                now = time.time()
                if not leader or (now - leader.get("heartbeat", 0)) >= config.HEARTBEAT_TIMEOUT:
                    if self.acquire():
                        on_promote()
                        return
            except Exception as e:
                print(f"[follower] 检查失败: {e}", flush=True)


# ==================== 隧道 & URL 上报 ====================
def report_url(url: str):
    """把公网 URL 写到仓库 public_url.txt"""
    try:
        get_url = f"https://api.github.com/repos/{config.REPO}/contents/public_url.txt"
        status, data = gh_request("GET", get_url)
        payload = {
            "message": f"update public url {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": base64.b64encode(url.encode()).decode(),
        }
        if status == 200:
            payload["sha"] = data.get("sha")
        gh_request("PUT", get_url, data=payload)
        print(f"[url] 已上报公网地址: {url}", flush=True)
    except Exception as e:
        print(f"[url] 上报失败: {e}", flush=True)


def start_tunnel(on_url):
    """启动固定隧道，回退 quick tunnel。on_url 回调上报地址"""
    import re
    try:
        if config.TUNNEL_TOKEN:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", config.TUNNEL_TOKEN],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            url = f"https://{config.TUNNEL_HOST}"
            print(f"[tunnel] 固定隧道启动: {url}", flush=True)
            on_url(url)
            for line in proc.stdout:
                line = line.strip()
                if "Registered tunnel connection" in line:
                    print(f"[tunnel] 连接已注册", flush=True)
                elif "ERR" in line.upper() and "error" in line.lower():
                    print(f"[tunnel] 异常: {line}", flush=True)
        else:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{config.PORT}", "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            reported = False
            for line in proc.stdout:
                line = line.strip()
                if "trycloudflare.com" in line:
                    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if m and not reported:
                        on_url(m.group(0))
                        reported = True
    except Exception as e:
        print(f"[tunnel] 启动失败: {e}", flush=True)


# ==================== 无缝衔接 ====================
def pre_wake_loop():
    """到期前预触发下一个 job"""
    done = False
    while True:
        elapsed = int((datetime.datetime.now(datetime.timezone.utc) - START_TIME).total_seconds())
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            try:
                url = f"https://api.github.com/repos/{config.REPO}/actions/workflows/demo.yml/dispatches"
                gh_request("POST", url, data={"ref": "main"})
                print(f"[prewake] 已预触发下一个 job（运行 {elapsed}s）", flush=True)
            except Exception as e:
                print(f"[prewake] 触发失败: {e}", flush=True)
            break
        time.sleep(60)


# ==================== 全局状态 ====================
JOB_ID = uuid.uuid4().hex[:8]
START_TIME = datetime.datetime.now(datetime.timezone.utc)
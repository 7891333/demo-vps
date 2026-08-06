# -*- coding: utf-8 -*-
"""共享核心：加密、GitHub API（多账号）、Releases 存储、leader锁、续命"""
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

# 全局 job 标识
JOB_ID = uuid.uuid4().hex[:8]
START_TIME = datetime.datetime.now(datetime.timezone.utc)


# ==================== 加密 ====================
def encrypt(data: bytes) -> bytes:
    key = bytes.fromhex(config.DEMO_KEY)
    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + ct


def decrypt(blob: bytes) -> bytes:
    key = bytes.fromhex(config.DEMO_KEY)
    nonce, tag, ct = blob[:16], blob[16:32], blob[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ct, tag)


# ==================== GitHub API（支持多账号） ====================
def gh_request(method, url, token=None, data=None, headers=None, raw=False, timeout=60):
    """通用 GitHub API 请求。token 为空则用 config.GH_TOKEN"""
    tok = token or config.GH_TOKEN
    h = {"Authorization": f"token {tok}", "Accept": "application/vnd.github.v3+json"}
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


def get_release(token=None):
    url = f"https://api.github.com/repos/{config.REPO}/releases/tags/{config.BACKUP_TAG}"
    status, data = gh_request("GET", url, token=token)
    return data if status == 200 else None


def ensure_release(token=None):
    rel = get_release(token=token)
    if rel:
        return rel["id"]
    url = f"https://api.github.com/repos/{config.REPO}/releases"
    data = {
        "tag_name": config.BACKUP_TAG, "name": "加密备份",
        "body": "AES-256-GCM 加密备份", "draft": False, "prerelease": False,
    }
    status, d = gh_request("POST", url, token=token, data=data)
    if status in (200, 201):
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def _delete_asset(name, token=None):
    rel = get_release(token=token)
    if rel:
        for a in rel.get("assets", []):
            if a.get("name") == name:
                gh_request("DELETE", f"https://api.github.com/repos/{config.REPO}/releases/assets/{a['id']}", token=token)


def upload_asset(name, data_bytes, token=None):
    """上传 asset。必须用 uploads.github.com"""
    rel_id = ensure_release(token=token)
    _delete_asset(name, token=token)
    url = f"https://uploads.github.com/repos/{config.REPO}/releases/{rel_id}/assets?name={name}"
    status, _ = gh_request("POST", url, token=token, data=data_bytes,
                           headers={"Content-Type": "application/octet-stream"})
    return len(data_bytes), status


def download_asset(name, token=None):
    """下载 asset，必须带 Accept: application/octet-stream"""
    rel = get_release(token=token)
    if not rel:
        return None
    for a in rel.get("assets", []):
        if a.get("name") == name:
            status, blob = gh_request(
                "GET", f"https://api.github.com/repos/{config.REPO}/releases/assets/{a['id']}",
                token=token, raw=True, headers={"Accept": "application/octet-stream"})
            return blob if status == 200 else None
    return None


# ==================== 加密 JSON 存取 ====================
def save_json_enc(asset_name, obj, token=None):
    """把对象加密后存为 asset"""
    return upload_asset(asset_name, encrypt(json.dumps(obj).encode()), token=token)


def load_json_enc(asset_name, token=None, default=None):
    """读取并解密 asset 为对象"""
    blob = download_asset(asset_name, token=token)
    if not blob:
        return default
    try:
        return json.loads(decrypt(blob).decode())
    except Exception:
        return default


# ==================== 数据库/文件 ====================
def _db_conn():
    import sqlite3
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def create_new_db():
    conn = _db_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
                 (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    conn.close()


def load_or_create(token=None):
    """恢复数据库+文件，返回状态描述"""
    status = "新建初始数据库"
    blob = download_asset(config.ASSET_DB, token=token)
    if blob:
        try:
            data = decrypt(blob)
            with open(config.DB_FILE, "wb") as f:
                f.write(data)
            status = f"从 Releases 恢复加密备份（{len(data)} 字节）"
        except Exception:
            create_new_db()
    else:
        create_new_db()
    restore_files(token=token)
    return status


def backup_database(token=None):
    with open(config.DB_FILE, "rb") as f:
        data = f.read()
    return upload_asset(config.ASSET_DB, encrypt(data), token=token)


def backup_files(token=None):
    if not os.path.isdir(config.FILES_DIR):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(config.FILES_DIR, arcname="files")
    return upload_asset(config.ASSET_FILES, encrypt(buf.getvalue()), token=token)


def restore_files(token=None):
    blob = download_asset(config.ASSET_FILES, token=token)
    if not blob:
        return
    try:
        data = decrypt(blob)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(path=os.path.expanduser("~"))
        os.makedirs(config.FILES_DIR, exist_ok=True)
    except Exception:
        pass


# ==================== 主 job 锁 ====================
class LeaderLock:
    def __init__(self, token=None):
        self.is_leader = False
        self.token = token

    def _read(self):
        blob = download_asset(config.ASSET_LEADER, token=self.token)
        if not blob:
            return None
        try:
            return json.loads(blob.decode())
        except Exception:
            return None

    def _heartbeat(self):
        data = json.dumps({"job_id": JOB_ID, "heartbeat": time.time()}).encode()
        upload_asset(config.ASSET_LEADER, data, token=self.token)

    def acquire(self):
        leader = self._read()
        now = time.time()
        if leader and leader.get("job_id") != JOB_ID and (now - leader.get("heartbeat", 0)) < config.HEARTBEAT_TIMEOUT:
            self.is_leader = False
            return False
        self.is_leader = True
        self._heartbeat()
        return True

    def heartbeat_loop(self):
        while True:
            if not self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                self._heartbeat()
            except Exception:
                pass

    def follower_loop(self, on_promote):
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
            except Exception:
                pass


# ==================== 续命（预触发下一个 job） ====================
def pre_wake_loop(token=None, workflow=None):
    """到期前预触发下一个 job，实现无缝衔接"""
    wf = workflow or config.WORKER_WORKFLOW
    done = False
    while True:
        elapsed = int((datetime.datetime.now(datetime.timezone.utc) - START_TIME).total_seconds())
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            try:
                url = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{wf}/dispatches"
                gh_request("POST", url, token=token, data={"ref": "main"})
                print(f"[prewake] 已预触发下一个 job（运行 {elapsed}s）", flush=True)
            except Exception as e:
                print(f"[prewake] 触发失败: {e}", flush=True)
            break
        time.sleep(60)


# ==================== 查询账号并发 ====================
def count_running_runs(token, workflow=None):
    """查询指定账号当前运行的 job 数（并发检测）"""
    url = f"https://api.github.com/repos/{config.REPO}/actions/runs?status=in_progress&per_page=100"
    status, data = gh_request("GET", url, token=token)
    if status != 200:
        return None
    runs = data.get("workflow_runs", [])
    if workflow:
        return sum(1 for r in runs if r.get("path") == workflow)
    return len(runs)
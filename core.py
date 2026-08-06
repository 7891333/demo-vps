# -*- coding: utf-8 -*-
"""共享核心：加密、GitHub API（多账号）、Releases 存储（并发分片）、leader锁、续命"""
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
from concurrent.futures import ThreadPoolExecutor

from Crypto.Cipher import AES

import config

# 全局 job 标识
JOB_ID = uuid.uuid4().hex[:8]
START_TIME = datetime.datetime.now(datetime.timezone.utc)

# 分片大小（单 asset 上限 2GB，用 500MB 安全）
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(500 * 1024 * 1024)))
# 并发上传/下载线程数（避免触发 GitHub 频率限制）
CHUNK_CONCURRENCY = int(os.environ.get("CHUNK_CONCURRENCY", "5"))


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
def gh_request(method, url, token=None, data=None, headers=None, raw=False, timeout=180):
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


_release_cache = {}


def ensure_release(token=None):
    # 缓存 release id，减少 API 调用（省配额）
    cache_key = token or config.GH_TOKEN
    if cache_key in _release_cache:
        return _release_cache[cache_key]
    rel = get_release(token=token)
    if rel:
        _release_cache[cache_key] = rel["id"]
        return rel["id"]
    url = f"https://api.github.com/repos/{config.REPO}/releases"
    data = {
        "tag_name": config.BACKUP_TAG, "name": "加密备份",
        "body": "AES-256-GCM 加密备份", "draft": False, "prerelease": False,
    }
    status, d = gh_request("POST", url, token=token, data=data)
    if status in (200, 201):
        _release_cache[cache_key] = d.get("id")
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def _delete_asset(name, token=None):
    rel = get_release(token=token)
    if rel:
        for a in rel.get("assets", []):
            if a.get("name") == name:
                gh_request("DELETE", f"https://api.github.com/repos/{config.REPO}/releases/assets/{a['id']}", token=token)


def upload_asset(name, data_bytes, token=None):
    """上传单个 asset（必须用 uploads.github.com）"""
    rel_id = ensure_release(token=token)
    _delete_asset(name, token=token)
    url = f"https://uploads.github.com/repos/{config.REPO}/releases/{rel_id}/assets?name={name}"
    status, _ = gh_request("POST", url, token=token, data=data_bytes,
                           headers={"Content-Type": "application/octet-stream"}, timeout=180)
    return len(data_bytes), status


def download_asset(name, token=None):
    """下载单个 asset，必须带 Accept: application/octet-stream"""
    rel = get_release(token=token)
    if not rel:
        return None
    for a in rel.get("assets", []):
        if a.get("name") == name:
            status, blob = gh_request(
                "GET", f"https://api.github.com/repos/{config.REPO}/releases/assets/{a['id']}",
                token=token, raw=True, headers={"Accept": "application/octet-stream"}, timeout=180)
            return blob if status == 200 else None
    return None


# ==================== 并发分片上传/下载 ====================
def upload_asset_chunked(name, data_bytes, token=None, concurrency=None):
    """
    分片加密上传大文件（并发）。
    小文件：直接上传单 asset。
    大文件：切块加密并发上传为 name.part0/1/...，再上传 manifest。
    返回 (总大小, 分片数)
    """
    conc = concurrency or CHUNK_CONCURRENCY
    if len(data_bytes) <= CHUNK_SIZE:
        size, status = upload_asset(name, encrypt(data_bytes), token=token)
        return size, 1
    parts = (len(data_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = [(i, data_bytes[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]) for i in range(parts)]

    def _upload(args):
        i, chunk = args
        upload_asset(f"{name}.part{i}", encrypt(chunk), token=token)
        return i, len(chunk)

    with ThreadPoolExecutor(max_workers=conc) as ex:
        for i, size in ex.map(_upload, chunks):
            print(f"[chunk] {name}.part{i} 上传 {size} 字节", flush=True)
    upload_asset(f"{name}.manifest", encrypt(json.dumps({"parts": parts}).encode()), token=token)
    return len(data_bytes), parts


def download_asset_chunked(name, token=None, concurrency=None):
    """并发下载并合并分片文件，返回解密后的原始明文"""
    conc = concurrency or CHUNK_CONCURRENCY
    manifest_blob = download_asset(f"{name}.manifest", token=token)
    if manifest_blob:
        try:
            manifest = json.loads(decrypt(manifest_blob).decode())
            parts = int(manifest["parts"])
            results = [None] * parts

            def _download(i):
                blob = download_asset(f"{name}.part{i}", token=token)
                return i, decrypt(blob) if blob else None

            with ThreadPoolExecutor(max_workers=conc) as ex:
                for i, data in ex.map(_download, range(parts)):
                    results[i] = data
            if any(d is None for d in results):
                raise RuntimeError("部分分片缺失")
            return b"".join(results)
        except Exception as e:
            print(f"[chunk] 分片合并失败: {e}", flush=True)
            return None
    # 无 manifest，单文件
    blob = download_asset(name, token=token)
    if blob:
        try:
            return decrypt(blob)
        except Exception:
            return None
    return None


# ==================== 加密 JSON 存取 ====================
def save_json_enc(asset_name, obj, token=None):
    return upload_asset(asset_name, encrypt(json.dumps(obj).encode()), token=token)


def load_json_enc(asset_name, token=None, default=None):
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
    blob = download_asset_chunked(config.ASSET_DB, token=token)
    if blob:
        try:
            with open(config.DB_FILE, "wb") as f:
                f.write(blob)
            status = f"从 Releases 恢复加密备份（{len(blob)} 字节）"
        except Exception:
            create_new_db()
    else:
        create_new_db()
    restore_files(token=token)
    return status


def backup_database(token=None):
    with open(config.DB_FILE, "rb") as f:
        data = f.read()
    return upload_asset_chunked(config.ASSET_DB, data, token=token)


def backup_files(token=None):
    if not os.path.isdir(config.FILES_DIR):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(config.FILES_DIR, arcname="files")
    data = buf.getvalue()
    return upload_asset_chunked(config.ASSET_FILES, data, token=token)


def restore_files(token=None):
    data = download_asset_chunked(config.ASSET_FILES, token=token)
    if not data:
        return
    try:
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
        """follower 每 FOLLOWER_CHECK 秒检查 leader，过期立即升级（缩短交接缝）"""
        while True:
            if self.is_leader:
                return
            time.sleep(config.FOLLOWER_CHECK)
            try:
                leader = self._read()
                now = time.time()
                if not leader or (now - leader.get("heartbeat", 0)) >= config.HEARTBEAT_TIMEOUT:
                    if self.acquire():
                        on_promote()
                        return
            except Exception:
                pass

    def check_and_takeover(self):
        """新实例启动时立即检查：leader 过期则直接接管（不等 follower 周期）"""
        if not self.is_leader:
            self.acquire()


# ==================== 续命 ====================
def pre_wake_loop(token=None, workflow=None):
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


# ==================== 自动更新（帝国自动化） ====================
def auto_update_loop(workflow, instance_id=None, token=None):
    """定期检查主仓库版本，发现新版本则触发滚动重启，随后退出自己"""
    current_sha = config.CURRENT_SHA
    if not current_sha:
        print("[update] 未设置 CURRENT_SHA，自动更新禁用", flush=True)
        return
    while True:
        time.sleep(300)  # 每 5 分钟检查
        try:
            url = f"https://api.github.com/repos/{config.MAIN_REPO}/commits/main"
            status, d = gh_request("GET", url, token=token)
            latest = d.get("sha", "")
            if latest and latest != current_sha:
                print(f"[update] 检测到新版本 {latest[:10]} != {current_sha[:10]}，滚动重启", flush=True)
                trigger_url = f"https://api.github.com/repos/{config.REPO}/actions/workflows/{workflow}/dispatches"
                data = {"ref": "main"}
                if instance_id:
                    data["inputs"] = {"INSTANCE_ID": instance_id}
                status2, _ = gh_request("POST", trigger_url, token=token, data=data)
                print(f"[update] 已触发新实例 (HTTP {status2})，60 秒后旧实例退出", flush=True)
                time.sleep(60)
                os._exit(0)
        except Exception as e:
            print(f"[update] 检查失败: {e}", flush=True)

# ==================== 任务持久化（manager 后台任务队列） ====================
def save_tasks(tasks, token=None):
    """保存任务队列（加密）"""
    save_json_enc(config.ASSET_TASKS, tasks, token=token)


def load_tasks(token=None):
    """加载任务队列"""
    data = load_json_enc(config.ASSET_TASKS, token=token, default=[])
    return data if isinstance(data, list) else []


def add_task(task_type, params, token=None):
    """添加任务，返回任务 dict"""
    tasks = load_tasks(token=token)
    task = {
        "id": f"{task_type}-{uuid.uuid4().hex[:8]}",
        "type": task_type,
        "params": params,
        "status": "pending",   # pending/running/done/failed
        "error": "",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    tasks.append(task)
    save_tasks(tasks, token=token)
    return task


def update_task(task_id, token=None, **fields):
    """更新任务状态"""
    tasks = load_tasks(token=token)
    for t in tasks:
        if t.get("id") == task_id:
            t.update(fields)
            t["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            break
    save_tasks(tasks, token=token)

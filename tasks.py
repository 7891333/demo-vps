# -*- coding: utf-8 -*-
"""
任务持久化与执行（生产级）：
- 任务持久化到 Releases（重启后自动恢复未完成任务）
- 后台任务执行器：串行执行 pending 任务
- 幂等执行：每个 handler 检查已完成步骤，重复执行安全
- 自动重试：失败重试 3 次（指数退避）
- 任务去重：同类型+同标识参数合并
- 任务超时：超时标记失败
- 历史清理：保留最近 N 个任务
"""
import time
import uuid
import threading

import config
import core
import log

logger = log.setup_logger("task")

MAX_RETRIES = 3          # 失败重试次数
RETRY_DELAY = [5, 15, 45]  # 重试间隔（指数退避）
TASK_TIMEOUT = 900       # 任务超时（秒）
MAX_HISTORY = 50         # 保留最近任务数

# 任务执行器注册表: task_type -> handler(params, task) 
_handlers = {}
_lock = threading.Lock()


# ==================== 任务存储 ====================
def load_tasks():
    tasks = core.load_json_enc(config.ASSET_TASKS, default=[])
    return tasks if isinstance(tasks, list) else []


def save_tasks(tasks):
    core.save_json_enc(config.ASSET_TASKS, tasks)


# ==================== 任务操作 ====================
def add_task(task_type, params, dedup_key=None):
    """
    添加任务。
    dedup_key: 去重标识（同 key 的 pending/running 任务不重复添加）
    """
    with _lock:
        tasks = load_tasks()
        # 去重：同类型同 key 且未完成的，不重复添加
        if dedup_key:
            for t in tasks:
                if (t["type"] == task_type
                        and t.get("dedup_key") == dedup_key
                        and t["status"] in ("pending", "running")):
                    logger.info(f"[task] 任务已存在，跳过: {t['id']}")
                    return t
        task = {
            "id": f"t-{int(time.time())}-{uuid.uuid4().hex[:6]}",
            "type": task_type,
            "params": params,
            "dedup_key": dedup_key,
            "status": "pending",
            "retries": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "started_at": None,
            "error": "",
        }
        tasks.append(task)
        _trim_history(tasks)
        save_tasks(tasks)
        logger.info(f"[task] 添加任务 {task['id']} ({task_type})")
        return task


def update_task(task_id, **kw):
    with _lock:
        tasks = load_tasks()
        for t in tasks:
            if t["id"] == task_id:
                for k, v in kw.items():
                    t[k] = v
                t["updated_at"] = time.time()
                break
        save_tasks(tasks)


def _trim_history(tasks):
    """保留最近 MAX_HISTORY 个任务（清理历史）"""
    if len(tasks) > MAX_HISTORY:
        # 保留最新的 MAX_HISTORY 个
        tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
        del tasks[MAX_HISTORY:]


def get_pending_tasks():
    """获取待执行/执行中的任务（用于重启恢复）"""
    tasks = load_tasks()
    return [t for t in tasks if t["status"] in ("pending", "running")]


# ==================== 任务执行器 ====================
def register_handler(task_type):
    """注册任务处理器装饰器：@tasks.register_handler("type")"""
    def decorator(fn):
        _handlers[task_type] = fn
        return fn
    return decorator


def _execute(task):
    """执行单个任务（含重试）"""
    handler = _handlers.get(task["type"])
    if not handler:
        update_task(task["id"], status="failed", error="无处理器")
        logger.error(f"[task] {task['id']} 无处理器: {task['type']}")
        return
    task["started_at"] = time.time()
    update_task(task["id"], status="running", started_at=task["started_at"])
    retries = task.get("retries", 0)
    try:
        handler(task["params"], task)
        update_task(task["id"], status="done", error="")
        logger.info(f"[task] {task['id']} 完成")
    except Exception as e:
        retries += 1
        if retries <= MAX_RETRIES:
            delay = RETRY_DELAY[min(retries - 1, len(RETRY_DELAY) - 1)]
            update_task(task["id"], status="pending", retries=retries,
                        error=f"{e}")
            logger.warning(f"[task] {task['id']} 失败(第{retries}次): {e}，{delay}s后重试")
            time.sleep(delay)
        else:
            update_task(task["id"], status="failed", retries=retries, error=f"{e}")
            logger.error(f"[task] {task['id']} 最终失败: {e}")


def _worker_loop():
    """后台任务执行器：循环取 pending 任务执行"""
    while True:
        try:
            tasks = load_tasks()
            pending = [t for t in tasks if t["status"] == "pending"]
            # 检查超时的 running 任务
            for t in tasks:
                if (t["status"] == "running" and t.get("started_at")
                        and time.time() - t["started_at"] > TASK_TIMEOUT):
                    update_task(t["id"], status="failed", error="任务超时")
                    logger.error(f"[task] {t['id']} 超时")
            if pending:
                for t in pending[:1]:  # 一次执行一个（串行）
                    _execute(t)
            else:
                time.sleep(2)
        except Exception as e:
            logger.error(f"[task] 执行器异常: {e}")
            time.sleep(5)


def start_worker():
    """启动任务执行器（manager 专用）"""
    threading.Thread(target=_worker_loop, daemon=True).start()
    logger.info("[task] 任务执行器已启动")


def recover_pending():
    """重启恢复：把 pending/running 任务标记为 pending 重跑"""
    tasks = load_tasks()
    changed = False
    for t in tasks:
        if t["status"] in ("pending", "running", "failed"):
            # 失败的允许重跑（如果还有重试次数）
            if t["status"] == "failed" and t.get("retries", 0) >= MAX_RETRIES:
                continue
            t["status"] = "pending"
            t["started_at"] = None
            changed = True
    if changed:
        save_tasks(tasks)
        logger.info(f"[task] 已恢复未完成任务，待执行: {len(get_pending_tasks())}")
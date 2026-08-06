# -*- coding: utf-8 -*-
"""
统一日志系统（生产级）：
- 三级输出：控制台 + 内存环形缓冲（可查询）+ 文件（自动轮转）
- 分级日志：DEBUG/INFO/WARNING/ERROR（环境变量 LOG_LEVEL 控制）
- 请求日志：自动记录每个 API 调用（方法/路径/耗时/状态）
- 错误统计：错误/警告计数，可查询
- 日志查询：get_logs(limit, level) 按级别过滤
"""
import os
import time
import logging
import threading
from logging.handlers import RotatingFileHandler

# ==================== 配置 ====================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_RING_LINES = int(os.environ.get("LOG_RING_LINES", "3000"))
LOG_FILE = os.path.join(os.path.expanduser("~"), "ghvps.log")
LOG_FILE_MAX_BYTES = int(os.environ.get("LOG_FILE_MAX_MB", "5")) * 1024 * 1024
LOG_FILE_BACKUP = int(os.environ.get("LOG_FILE_BACKUP", "3"))

# ==================== 内存环形缓冲 ====================
_ring = []          # 每项: {"time": ts, "level": str, "msg": str}
_ring_lock = threading.Lock()
_stats = {"error": 0, "warning": 0}   # 错误/警告计数
_stats_lock = threading.Lock()


class RingBufferHandler(logging.Handler):
    """内存环形缓冲 handler"""
    def emit(self, record):
        try:
            msg = self.format(record)
            entry = {"time": time.time(), "level": record.levelname, "msg": msg}
            with _ring_lock:
                _ring.append(entry)
                if len(_ring) > MAX_RING_LINES:
                    del _ring[:len(_ring) - MAX_RING_LINES]
            # 错误统计
            if record.levelno >= logging.ERROR:
                with _stats_lock:
                    _stats["error"] += 1
            elif record.levelno >= logging.WARNING:
                with _stats_lock:
                    _stats["warning"] += 1
        except Exception:
            pass


# ==================== Logger 工厂 ====================
_loggers = {}
_loggers_lock = threading.Lock()


def setup_logger(name="ghvps"):
    """获取/创建统一 logger（每个模块共享同一个 root logger）"""
    with _loggers_lock:
        if name in _loggers:
            return _loggers[name]
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.setLevel(LOG_LEVEL)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S")
            # 控制台
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            logger.addHandler(ch)
            # 内存环形缓冲
            rb = RingBufferHandler()
            rb.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
            logger.addHandler(rb)
            # 文件（自动轮转，防无限膨胀）
            try:
                fh = RotatingFileHandler(
                    LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES,
                    backupCount=LOG_FILE_BACKUP, encoding="utf-8")
                fh.setFormatter(fmt)
                logger.addHandler(fh)
            except Exception:
                pass
        _loggers[name] = logger
        return logger


def get_logger(module=None):
    """模块日志入口：logger = log.get_logger(__name__)"""
    return setup_logger(module or "ghvps")


# ==================== 日志查询 API ====================
def get_logs(limit=500, level=None):
    """
    查询最近日志。
    level: None=全部, "INFO"/"WARNING"/"ERROR"=按级别过滤
    """
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    min_lv = levels.get(level, 0)
    with _ring_lock:
        entries = list(_ring)
    if min_lv:
        entries = [e for e in entries if levels.get(e["level"], 20) >= min_lv]
    return entries[-limit:]


def get_stats():
    """错误/警告统计"""
    with _stats_lock:
        return dict(_stats)


def request_logger(app):
    """Flask 请求日志：记录每个 API 调用的方法/路径/耗时/状态"""
    from flask import request
    import functools

    @app.before_request
    def _log_req_start():
        request.environ["_req_start"] = time.time()

    @app.after_request
    def _log_req_end(response):
        start = request.environ.get("_req_start", time.time())
        dur = (time.time() - start) * 1000
        logger = setup_logger("api")
        logger.info("%s %s -> %d (%.0fms)", request.method,
                    request.path, response.status_code, dur)
        return response

    return app
# -*- coding: utf-8 -*-
"""WSS 交互式终端会话管理：PTY 伪终端 + 断线重连保持会话 + UTF-8 修复"""
import os
import pty
import time
import fcntl
import signal
import struct
import termios
import threading

import config

# 会话表: session_key -> Session
SESSIONS = {}
_sessions_lock = threading.Lock()


class Session:
    """一个终端会话（对应一个 PTY/bash 进程）"""

    def __init__(self, key):
        self.key = key
        self.pid, self.fd = self._spawn()
        self.last_active = time.time()  # 最近活跃时间
        self.connected = True  # 当前是否有客户端连接

    @staticmethod
    def _spawn():
        """创建 PTY 并启动 bash（UTF-8 locale，修复中文乱码）"""
        pid, fd = pty.fork()
        if pid == 0:  # 子进程
            env = os.environ.copy()
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            env["TERM"] = "xterm-256color"
            os.execvpe("/bin/bash", ["bash", "--login"], env)
        return pid, fd

    def read_output(self, chunk=4096):
        """读取 PTY 输出，返回字节串"""
        try:
            data = os.read(self.fd, chunk)
            return data
        except OSError:
            return None

    def write_input(self, data: bytes):
        """写入客户端输入到 PTY"""
        try:
            os.write(self.fd, data)
            self.last_active = time.time()
        except OSError:
            pass

    def resize(self, rows: int, cols: int):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def destroy(self):
        """销毁会话"""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


def get_or_create_session(session_key: str) -> Session:
    """根据 session_key 获取已有会话（断线重连复用），否则新建"""
    with _sessions_lock:
        sess = SESSIONS.get(session_key)
        if sess:
            sess.attached = True
            sess.last_active = time.time()
            return sess
        sess = Session(session_key)
        SESSIONS[session_key] = sess
        return sess


def detach_session(session_key: str):
    """客户端断开时标记会话为未连接（保留 SESSION_TTL 等待重连）"""
    with _sessions_lock:
        sess = SESSIONS.get(session_key)
        if sess:
            sess.attached = False
            sess.last_active = time.time()


def destroy_session(session_key: str):
    """销毁指定会话"""
    with _sessions_lock:
        sess = SESSIONS.pop(session_key, None)
    if sess:
        sess.destroy()


def cleanup_loop():
    """清理过期会话（超过 SESSION_TTL 未重连的）"""
    while True:
        time.sleep(30)
        now = time.time()
        with _sessions_lock:
            stale = [k for k, s in SESSIONS.items()
                     if not getattr(s, "attached", True) and (now - s.last_active) > config.SESSION_TTL]
            for k in stale:
                s = SESSIONS.pop(k)
                s.destroy()
                print(f"[session] 会话过期已清理: {k}", flush=True)


def start_cleanup():
    threading.Thread(target=cleanup_loop, daemon=True).start()
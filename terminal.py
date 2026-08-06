# -*- coding: utf-8 -*-
"""WSS 交互式终端：PTY + bytes 传输 + pyte + 断线无缝 + 默认root"""
import os
import pty
import time
import fcntl
import signal
import struct
import termios
import threading

import pyte

import config

# 会话表: session_key -> Session
SESSIONS = {}
_lock = threading.Lock()


class Session:
    def __init__(self, key, cols=120, rows=35):
        self.key = key
        self.cols = cols
        self.rows = rows
        self.pid, self.fd = self._spawn()
        self.last_active = time.time()
        self.attached = True
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    @staticmethod
    def _spawn():
        """创建 PTY 并启动 root 交互 shell（sudo -i，免密）"""
        pid, fd = pty.fork()
        if pid == 0:
            env = os.environ.copy()
            env["LANG"] = "C.UTF-8"
            env["LC_ALL"] = "C.UTF-8"
            env["TERM"] = "xterm-256color"
            env["GHVPS_PERSIST_DIR"] = config.FILES_DIR
            # 直接进入 root 登录 shell（sudo 免密）
            os.execvpe("sudo", ["sudo", "-i"], env)
        return pid, fd

    def feed(self, data: bytes):
        try:
            self.stream.feed(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def get_screen(self):
        try:
            return "\n".join(self.screen.display)
        except Exception:
            return ""

    def read_output(self, chunk=8192):
        try:
            return os.read(self.fd, chunk)
        except OSError:
            return None

    def write_input(self, data: bytes):
        try:
            os.write(self.fd, data)
            self.last_active = time.time()
        except OSError:
            pass

    def resize(self, rows, cols):
        try:
            self.rows, self.cols = rows, cols
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            self.screen.resize(rows, cols)
        except Exception:
            pass

    def destroy(self):
        try:
            os.kill(self.pid, signal.SIGHUP)
            time.sleep(0.2)
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


def get_or_create_session(session_key: str) -> Session:
    with _lock:
        sess = SESSIONS.get(session_key)
        if sess:
            sess.attached = True
            sess.last_active = time.time()
            return sess
        sess = Session(session_key)
        SESSIONS[session_key] = sess
        return sess


def detach_session(session_key: str):
    with _lock:
        sess = SESSIONS.get(session_key)
        if sess:
            sess.attached = False
            sess.last_active = time.time()


def destroy_session(session_key: str):
    with _lock:
        sess = SESSIONS.pop(session_key, None)
    if sess:
        sess.destroy()


def get_screen(session_key: str):
    sess = SESSIONS.get(session_key)
    return sess.get_screen() if sess else ""


def cleanup_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with _lock:
            stale = [k for k, s in SESSIONS.items()
                     if not s.attached and (now - s.last_active) > config.SESSION_TTL]
            for k in stale:
                SESSIONS.pop(k).destroy()
                print(f"[session] 会话过期已清理: {k}", flush=True)


def start_cleanup():
    threading.Thread(target=cleanup_loop, daemon=True).start()
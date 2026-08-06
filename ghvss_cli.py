#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端交互式终端客户端（类 SSH）

特性：
- 交互式 bash 终端（PTY），支持 vi/top 等全屏程序
- 自动重连：断线后指数退避自动重连，保持同一会话（bash 历史、运行中命令不丢）
- 会话持久化：session_key 存本地，重连复用同一 PTY

用法：
  python3 ghvss_cli.py <EXEC_TOKEN>
  EXEC_TOKEN=xxx python3 ghvss_cli.py
"""
import os
import sys
import tty
import time
import uuid
import struct
import fcntl
import termios
import threading

import socketio

URL = os.environ.get("GHVPS_URL", "https://ghvps.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")
SESSION_FILE = os.path.expanduser("~/.ghvps_session")


def _load_session():
    """读取或创建持久化 session_key（保证重连复用同一会话）"""
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            k = f.read().strip()
            if k:
                return k
    k = uuid.uuid4().hex
    with open(SESSION_FILE, "w") as f:
        f.write(k)
    return k


SESSION = _load_session()

# 自动重连：reconnection_attempts=0 表示无限重连，指数退避
sio = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=1,
    reconnection_delay_max=3,
    randomization_factor=0.2,
)


@sio.on("output")
def on_output(data):
    sys.stdout.write(data)
    sys.stdout.flush()


@sio.on("exit")
def on_exit(data):
    sio.disconnect()


@sio.event
def connect():
    # 连接后发送当前终端窗口大小
    try:
        rows, cols = struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0\0\0\0\0\0\0\0")
        )[:2]
        sio.emit("resize", {"rows": rows, "cols": cols})
    except Exception:
        pass
    sys.stderr.write("\r\n[已连接云端终端] 断线自动重连，会话保持\r\n")
    sys.stderr.flush()


@sio.event
def disconnect():
    sys.stderr.write("\r\n[连接断开，自动重连中...]\r\n")
    sys.stderr.flush()


def _send_loop():
    """读取本地 stdin 逐字节发送"""
    try:
        while True:
            ch = os.read(0, 1)
            if not ch:
                break
            sio.emit("input", ch.decode(errors="replace"))
    except Exception:
        pass
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def main():
    global TOKEN
    if len(sys.argv) > 1:
        TOKEN = sys.argv[1]
    if not TOKEN:
        print("用法: python3 ghvss_cli.py <EXEC_TOKEN>", file=sys.stderr)
        sys.exit(1)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sio.connect(URL, auth={"token": TOKEN, "session": SESSION},
                    transports=["websocket"], wait_timeout=25)
        threading.Thread(target=_send_loop, daemon=True).start()
        sio.wait()
    except Exception as e:
        print(f"\r\n[错误] {e}\r\n", file=sys.stderr)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端交互式终端客户端（类 SSH）

特性：
- 交互式 bash 终端（PTY），支持 vi/top 等全屏程序
- bytes 传输（修复中文乱码）
- 自动重连 + 会话保持（断线后 bash 历史/运行中命令不丢）
- 复制干净屏幕：连接后按 Ctrl+O 获取干净屏幕文本

用法：
  python3 ghvss_cli.py <EXEC_TOKEN> [URL]
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
import urllib.request

import socketio

URL = os.environ.get("GHVPS_URL", "https://ghvps.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")
SESSION_FILE = os.path.expanduser("~/.ghvps_session")


def _load_session():
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

sio = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=1,
    reconnection_delay_max=3,
    randomization_factor=0.2,
)


def _get_clean_screen():
    """从服务端获取 pyte 干净屏幕文本"""
    try:
        url = f"{URL}/api/term/screen?session={SESSION}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                return data.get("screen", "")
    except Exception:
        pass
    return ""


@sio.on("output")
def on_output(data):
    # bytes 直接写，避免乱码
    if isinstance(data, bytes):
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.write(data)
    sys.stdout.flush()


@sio.on("exit")
def on_exit(data):
    sio.disconnect()


@sio.event
def connect():
    try:
        rows, cols = struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0\0\0\0\0\0\0\0")
        )[:2]
        sio.emit("resize", {"rows": rows, "cols": cols})
    except Exception:
        pass
    sys.stderr.write("\r\n[已连接云端终端] Ctrl+O 复制干净屏幕 · 断线自动重连\r\n")
    sys.stderr.flush()


@sio.event
def disconnect():
    sys.stderr.write("\r\n[连接断开，自动重连中...]\r\n")
    sys.stderr.flush()


def _send_loop():
    try:
        while True:
            ch = os.read(0, 1)  # bytes
            if not ch:
                break
            # Ctrl+O (0x0f) 获取干净屏幕
            if ch == b"\x0f":
                screen = _get_clean_screen()
                if screen:
                    sys.stdout.write("\r\n" + screen + "\r\n")
                    sys.stdout.flush()
                continue
            sio.emit("input", ch)  # 直接发 bytes
    except Exception:
        pass
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def main():
    global TOKEN, URL, API
    if len(sys.argv) > 1:
        TOKEN = sys.argv[1]
    if len(sys.argv) > 2:
        URL = sys.argv[2].rstrip("/")
    API = URL
    if not TOKEN:
        print("用法: python3 ghvss_cli.py <EXEC_TOKEN> [URL]", file=sys.stderr)
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
    import json
    main()
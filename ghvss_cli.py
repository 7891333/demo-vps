#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端交互式终端客户端（类 SSH 体验）

特性：
- 自动重连（断线后自动恢复，无感）
- 会话保持（固定 session_id，重连后云端 bash 状态/历史/运行中命令不丢失）
- 批量发送 bytes（修复中文乱码与粘贴截断）
- 支持 vi / top / htop 等交互程序（PTY 伪终端）

用法：
  python3 ghvss_cli.py <EXEC_TOKEN>
  # 或通过环境变量
  EXEC_TOKEN=xxx python3 ghvss_cli.py
"""
import os
import sys
import tty
import uuid
import threading
import termios
import socketio

URL = os.environ.get("GHVPS_URL", "https://ghvps.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")
SESSION_FILE = os.path.expanduser("~/.ghvps_session")


def get_session_id():
    """获取固定 session_id（存本地文件，保证重连复用同一云端会话）"""
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE) as f:
                sid = f.read().strip()
                if sid:
                    return sid
        sid = uuid.uuid4().hex[:12]
        with open(SESSION_FILE, "w") as f:
            f.write(sid)
        return sid
    except Exception:
        return "default"


# 自动重连：reconnection=True，无限重试，2s起步指数退避
sio = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=2,
    reconnection_delay_max=10,
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
    sys.stderr.write("\r\n[已连接云端终端] 输入 exit 或 Ctrl+D 退出\r\n")
    sys.stderr.flush()


@sio.event
def disconnect():
    # 自动重连由 socketio 处理，这里只提示（不退出）
    sys.stderr.write("\r\n[连接断开，自动重连中...]\r\n")
    sys.stderr.flush()


def send_loop():
    """批量读取 stdin 并发送（发送 bytes，避免乱码/截断）"""
    try:
        while True:
            data = os.read(0, 4096)  # 批量读，raw 模式下粘贴会整段返回
            if not data:
                break
            if sio.connected:
                sio.emit("input", data)  # 直接发 bytes
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
    session_id = get_session_id()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)  # raw 模式（关闭行缓冲/echo）
        sio.connect(
            URL,
            auth={"token": TOKEN, "session_id": session_id},
            transports=["websocket"],
            wait_timeout=25,
        )
        threading.Thread(target=send_loop, daemon=True).start()
        sio.wait()
    except Exception as e:
        print(f"\r\n[错误] {e}\r\n", file=sys.stderr)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
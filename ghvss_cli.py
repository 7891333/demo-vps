#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端交互式终端客户端（类 SSH 体验）

连接云端 WSS 终端（Flask-SocketIO + PTY），实现本地终端体验：
- 实时双向输入输出
- 支持 vi / top / htop 等交互式程序（PTY 伪终端）
- 支持 Ctrl+C、Tab 补全等终端控制

用法：
  python3 ghvps_cli.py <EXEC_TOKEN>
  # 或通过环境变量
  EXEC_TOKEN=xxx python3 ghvps_cli.py
"""
import os
import sys
import tty
import threading
import termios
import socketio

URL = os.environ.get("GHVPS_URL", "https://ghvps.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")

sio = socketio.Client()


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
    sys.stderr.write("\r\n[连接已断开]\r\n")
    sys.stderr.flush()
    os._exit(0)


def send_loop():
    """读取本地 stdin 逐字节发送给云端"""
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
        tty.setraw(fd)  # 进入 raw 模式（关闭行缓冲/echo）
        sio.connect(URL, auth={"token": TOKEN}, transports=["websocket"], wait_timeout=20)
        threading.Thread(target=send_loop, daemon=True).start()
        sio.wait()
    except Exception as e:
        print(f"\r\n[错误] {e}\r\n", file=sys.stderr)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
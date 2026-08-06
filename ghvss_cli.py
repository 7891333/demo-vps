#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端实例管理客户端（完整版）

功能：
- 主菜单：查看实例 / 新建实例 / 连接终端 / 关闭实例 / 账号管理
- WSS 交互式终端（bytes 传输无乱码 + 自动重连 + 会话保持 + 输出积压保护）
- Ctrl+O 复制干净屏幕

用法：
  python3 ghvss_cli.py [EXEC_TOKEN] [MANAGER_URL] [INSTANCE_URL可选]
"""
import os
import sys
import time
import json
import uuid
import tty
import struct
import fcntl
import termios
import threading
import urllib.request
import urllib.error

import socketio

MANAGER = os.environ.get("GHVPS_MANAGER", "https://ghvps.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")
SESSION_FILE = os.path.expanduser("~/.ghvps_session")

# 输出积压保护（防止大量输出卡死本地终端）
MAX_PENDING = 256 * 1024


def _load_session():
    """读取或创建持久化 session_key（断线重连保持会话）"""
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


# ==================== HTTP API ====================
def api(method, url, data=None, timeout=60):
    """请求 manager API，返回 dict"""
    h = {"Content-Type": "application/json",
         "Authorization": f"Bearer {TOKEN}",
         "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def mgr(path):
    return MANAGER.rstrip("/") + path


# ==================== 终端连接 ====================
def _get_clean_screen(url):
    try:
        req = urllib.request.Request(
            url.rstrip("/") + f"/api/term/screen?session={SESSION}",
            headers={"User-Agent": "Mozilla/5.0 (ghvss-cli)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
            if d.get("ok"):
                return d.get("screen", "")
    except Exception:
        pass
    return ""


def connect_terminal(host):
    """连接实例 WSS 终端（完整功能）"""
    url = f"https://{host}" if not host.startswith("http") else host
    _pending = {"v": 0}

    sio = socketio.Client(
        reconnection=True, reconnection_attempts=0,
        reconnection_delay=1, reconnection_delay_max=3, randomization_factor=0.2,
    )

    @sio.on("output")
    def on_output(data):
        # 积压保护：超过阈值丢弃部分输出，防本地卡死
        if _pending["v"] > MAX_PENDING:
            return
        _pending["v"] += len(data) if isinstance(data, bytes) else len(data.encode())
        try:
            if isinstance(data, bytes):
                sys.stdout.buffer.write(data)
            else:
                sys.stdout.write(data)
            sys.stdout.flush()
        except Exception:
            pass
        finally:
            _pending["v"] = 0

    @sio.on("exit")
    def on_exit(data):
        sio.disconnect()

    @sio.event
    def connect():
        try:
            rows, cols = struct.unpack(
                "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0\0\0\0\0\0\0\0"))[:2]
            sio.emit("resize", {"rows": rows, "cols": cols})
        except Exception:
            pass
        sys.stderr.write("\r\n[已连接终端] Ctrl+O 复制干净屏幕 · 断线自动重连 · Ctrl+C 退出\r\n")
        sys.stderr.flush()

    @sio.event
    def disconnect():
        sys.stderr.write("\r\n[连接断开，自动重连中...]\r\n")
        sys.stderr.flush()

    def send_loop():
        try:
            while True:
                ch = os.read(0, 1)
                if not ch:
                    break
                if ch == b"\x0f":  # Ctrl+O
                    screen = _get_clean_screen(url)
                    if screen:
                        sys.stdout.write("\r\n" + screen + "\r\n")
                        sys.stdout.flush()
                    continue
                if ch == b"\x03":  # Ctrl+C 退出终端返回菜单
                    sio.disconnect()
                    break
                sio.emit("input", ch)
        except Exception:
            pass
        finally:
            try:
                sio.disconnect()
            except Exception:
                pass

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sio.connect(url, auth={"token": TOKEN, "session": SESSION},
                    transports=["websocket"], wait_timeout=25)
        threading.Thread(target=send_loop, daemon=True).start()
        sio.wait()
    except Exception as e:
        print(f"\r\n[错误] {e}\r\n")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ==================== 菜单操作 ====================
def list_instances():
    d = api("GET", mgr("/api/instances"))
    insts = d.get("instances", [])
    if not insts:
        print("  暂无实例")
        return
    print(f"  {'ID':<8}{'域名':<30}{'状态':<10}{'账号':<10}")
    print("  " + "-" * 58)
    for i in insts:
        print(f"  {i['id']:<8}{i.get('hostname',''):<30}{i.get('status',''):<10}{i.get('account','')}")


def create_instance():
    print("  正在创建新实例（自动配置隧道+启动）...")
    d = api("POST", mgr("/api/instances"))
    if d.get("ok"):
        inst = d.get("instance", {})
        print(f"  ✅ 创建成功: {inst.get('id')} → https://{inst.get('hostname')}")
    else:
        print(f"  ❌ 失败: {d.get('error')}")


def close_instance():
    list_instances()
    inst_id = input("  输入要关闭的实例 ID: ").strip()
    if not inst_id:
        return
    d = api("DELETE", f"{mgr('/api/instances')}/{inst_id}")
    print(f"  {'✅ ' + d.get('msg','') if d.get('ok') else '❌ ' + d.get('error','')}")


def add_account():
    print("  （全自动：验证token→fork仓库→配secrets→报备）")
    name = input("  账号名称: ").strip()
    token = input("  GitHub Token: ").strip()
    if not name or not token:
        print("  名称和 token 必填")
        return
    d = api("POST", mgr("/api/accounts"), {"name": name, "token": token})
    print(f"  {'✅ ' + d.get('msg','') if d.get('ok') else '❌ ' + d.get('error','')}")
    if d.get("ok"):
        print("  ⏳ 正在后台自动配置（fork+secrets），约30秒后生效")


def list_accounts():
    d = api("GET", mgr("/api/accounts"))
    for a in d.get("accounts", []):
        print(f"  {a['name']} | token:{a.get('token_masked')} | repo:{a.get('repo')} | 并发:{a.get('max_concurrency')}")


def pick_and_connect():
    d = api("GET", mgr("/api/instances"))
    insts = [i for i in d.get("instances", []) if i.get("status") in ("running", "starting")]
    if not insts:
        print("  没有可连接的实例")
        return
    for idx, i in enumerate(insts):
        print(f"  [{idx}] {i['id']} → https://{i.get('hostname')} ({i.get('status')})")
    try:
        sel = int(input("  选择实例序号: ").strip())
        inst = insts[sel]
    except Exception:
        print("  无效选择")
        return
    print(f"  连接 {inst['id']} ... (Ctrl+C 返回菜单)")
    connect_terminal(f"https://{inst.get('hostname')}")


# ==================== 主菜单 ====================
def main_menu():
    print("\n" + "=" * 52)
    print("  GitHub Actions 云端实例管理")
    print(f"  Manager: {MANAGER}")
    print("=" * 52)
    print("  [1] 查看所有实例")
    print("  [2] 新建实例")
    print("  [3] 连接实例终端")
    print("  [4] 关闭实例")
    print("  [5] 查看账号")
    print("  [6] 添加账号")
    print("  [0] 退出")
    return input("\n  请选择: ").strip()


def main():
    global MANAGER, TOKEN
    # 参数：token [manager_url] [instance_url]
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        # JSON 模式：单次操作，便于脚本调用
        op = sys.argv[2] if len(sys.argv) > 2 else "instances"
        if op == "instances":
            print(json.dumps(api("GET", mgr("/api/instances"))))
        elif op == "create":
            print(json.dumps(api("POST", mgr("/api/instances"))))
        elif op == "close" and len(sys.argv) > 3:
            print(json.dumps(api("DELETE", f"{mgr('/api/instances')}/{sys.argv[3]}")))
        elif op == "accounts":
            print(json.dumps(api("GET", mgr("/api/accounts"))))
        return
    if len(sys.argv) > 1:
        TOKEN = sys.argv[1]
    if len(sys.argv) > 2:
        MANAGER = sys.argv[2].rstrip("/")
    if not TOKEN:
        print("用法: python3 ghvss_cli.py <EXEC_TOKEN> [MANAGER_URL] [INSTANCE_URL]")
        sys.exit(1)
    # 如果给了第三个参数（实例 URL），直接连接终端
    if len(sys.argv) > 3:
        connect_terminal(sys.argv[3])
        return
    while True:
        try:
            choice = main_menu()
            if choice == "1":
                list_instances()
            elif choice == "2":
                create_instance()
            elif choice == "3":
                pick_and_connect()
            elif choice == "4":
                close_instance()
            elif choice == "5":
                list_accounts()
            elif choice == "6":
                add_account()
            elif choice == "0":
                print("  再见！")
                break
            else:
                print("  无效选择")
        except KeyboardInterrupt:
            print("\n  返回菜单")
        except Exception as e:
            print(f"  错误: {e}")


if __name__ == "__main__":
    main()
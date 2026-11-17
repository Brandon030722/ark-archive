"""Start the local workbench with the Mac's current Wi-Fi IPv4 address."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from scripts.serve_web import lan_ipv4

ROOT = Path(__file__).resolve().parents[1]
PROOFS = ".local/stage68-complex-recovery"
MANAGED_PROOFS = ".local/workbench/proofs"
LAUNCHCTL = "/bin/launchctl"
MODEL_STATUS = {
    "ready": "就绪",
    "campaign_frozen": "暂停，上一笔模型请求的结果或费用待确认",
    "price_verification_expired": "暂停，价格凭据已过期",
    "request_inflight": "正在处理已有请求",
    "live_disabled": "当前服务未启用真实模型",
    "price_verification_invalid": "暂停，价格凭据无法核验",
    "proof_refresh_in_progress": "正在核验 DeepSeek 官方价格，稍后刷新状态",
    "price_source_changed": "暂停，官方价格页面有变更，需要复核",
    "proof_refresh_failed": "暂停，无法完成官方价格的联网核验",
    "runtime_profile_unavailable": "暂停，模型来源凭据未通过核验",
    "configuration_missing": "暂停，模型配置不完整",
    "runtime_policy_unavailable": "暂停，模型的启动检查未通过",
}


def wifi_ipv4() -> str | None:
    try:
        ports = subprocess.check_output(
            ["/usr/sbin/networksetup", "-listallhardwareports"], text=True,
            env={**os.environ, "LC_ALL": "C"}, timeout=5,
        )
        match = re.search(r"^Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (en\d+)$", ports, re.MULTILINE)
        if match is None:
            return None
        address = subprocess.check_output(
            ["/usr/sbin/ipconfig", "getifaddr", match.group(1)], text=True, timeout=5,
            stderr=subprocess.DEVNULL,
        ).strip()
        return lan_ipv4(address)
    except (OSError, subprocess.SubprocessError, argparse.ArgumentTypeError):
        return None


def probe(client: httpx.Client, origin: str) -> str | None:
    try:
        status = client.get(origin + "/v1/agent/status")
        status.raise_for_status()
        model = status.json()["web_model"]
        release = client.get(origin + "/v1/releases/active")
        release.raise_for_status()
        if type(model["ready"]) is bool and isinstance(model["reason"], str) and release.json()["id"]:
            return "ready" if model["ready"] else model["reason"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass
    return None


def occupied(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def show_addresses(client, local, mobile, reason, *, open_browser):
    print(f"电脑访问：{local}/", flush=True)
    if mobile and probe(client, mobile) is not None:
        print(f"手机访问：{mobile}/  （手机与 Mac 连同一 Wi-Fi）", flush=True)
    elif mobile:
        print("当前 Wi-Fi 地址无法访问工作台。切换 Wi-Fi 后可停止服务并重新启动。", flush=True)

    print(f"模型状态：{MODEL_STATUS.get(reason, reason)}", flush=True)
    if open_browser:
        subprocess.run(["/usr/bin/open", local + "/"], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, choices=range(1, 65536), metavar="PORT", default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--lan", action="store_true", help="share on the current trusted Wi-Fi")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--status", action="store_true", help="check the local and Wi-Fi addresses")
    modes.add_argument("--stop", action="store_true", help="stop the background workbench")
    args = parser.parse_args()
    os.chdir(ROOT)
    address = wifi_ipv4() if args.lan else None
    if args.lan and address is None and not (args.serve or args.stop or args.status):
        sys.exit("没有可用的 Wi-Fi 局域网地址。连接 Wi-Fi 后重试，或双击本机启动。")
    if args.serve:
        serve(args.port, address)
        return
    local = f"http://127.0.0.1:{args.port}"
    mobile = f"http://{address}:{args.port}" if address else None

    label = f"local.arknights.workbench.{args.port}"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    agents = Path.home() / "Library" / "LaunchAgents"
    plist = agents / f"{label}.plist"
    logs = Path.home() / "Library" / "Logs" / "ArknightsWorkbench"
    logfile = logs / f"{args.port}.log"
    registered = subprocess.run([LAUNCHCTL, "print", target], capture_output=True).returncode == 0
    if registered:
        try:
            active_config = plistlib.loads(plist.read_bytes())
        except (OSError, ValueError):
            sys.exit("已有后台服务，无法确认项目目录。请用其他端口启动。")
        if Path(active_config.get("WorkingDirectory", "")).resolve() != ROOT.resolve():
            sys.exit("这个端口已由另一个方舟档案目录使用。请停止原服务或换一个端口。")
    change_mode = registered and not args.status and (("--lan" in active_config["ProgramArguments"]) != args.lan)
    if args.stop or change_mode:
        if registered:
            task_path = ROOT / ".local" / "http-chat-tasks.sqlite3"
            if task_path.is_file():
                with sqlite3.connect(task_path.as_uri() + "?mode=ro", uri=True) as db:
                    active = db.execute(
                        "SELECT 1 FROM http_chat_task WHERE state IN ('queued','running','waiting') LIMIT 1"
                    ).fetchone()
                if active:
                    sys.exit("仍有查询正在处理。请等本轮结束后再停止工作台。")
            subprocess.run([LAUNCHCTL, "bootout", target], check=True)
            deadline = time.monotonic() + 15
            while (subprocess.run([LAUNCHCTL, "print", target], capture_output=True).returncode == 0
                   or occupied(args.port)):
                if time.monotonic() >= deadline:
                    sys.exit("上一服务仍在退出，请稍后重试。")
                time.sleep(0.25)
        plist.unlink(missing_ok=True)
        registered = False
        if args.stop:
            print("后台工作台已停止。")
            return
    with httpx.Client(trust_env=False, timeout=3) as client:
        reason = probe(client, local)
        if reason is None and args.status:
            sys.exit(f"工作台未就绪。日志：{logfile}")
        if not registered and not args.status and occupied(args.port):
            sys.exit(f"端口 {args.port} 正被其他服务使用。")
        if reason is None and not registered:
            if not (ROOT / ".local/postgres/data/PG_VERSION").is_file():
                sys.exit("找不到已有项目数据库，请先完成项目数据库配置。")
            agents.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            config = {
                "Label": label, "WorkingDirectory": str(ROOT),
                "EnvironmentVariables": {"PATH": os.environ.get("PATH", os.defpath)},
                "ProgramArguments": [sys.executable, "-m", "scripts.launch_workbench", "--serve", "--port", str(args.port)] + (["--lan"] if args.lan else []),
                "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
                "StandardOutPath": str(logfile), "StandardErrorPath": str(logfile),
            }
            with tempfile.TemporaryDirectory(dir=agents) as tmp:
                candidate = Path(tmp) / plist.name
                candidate.write_bytes(plistlib.dumps(config))
                candidate.replace(plist)
            # launchd can briefly keep a removed label busy after bootout.
            for attempt in range(5):
                result = subprocess.run([LAUNCHCTL, "bootstrap", domain, str(plist)], capture_output=True)
                if result.returncode == 0:
                    break
                if not change_mode or attempt == 4 or result.returncode != 5:
                    sys.exit("后台启动失败：" + result.stderr.decode(errors="replace").strip())
                time.sleep(1)
        deadline = time.monotonic() + 20
        while reason is None and time.monotonic() < deadline:
            time.sleep(0.25)
            reason = probe(client, local)
        if reason is None:
            sys.exit(f"工作台未就绪。请查看启动日志：{logfile}")
        show_addresses(client, local, mobile, reason, open_browser=not args.no_browser and not args.status)
        print(f"服务在后台运行。日志：{logfile}", flush=True)


def serve(port, address):
    if not (ROOT / ".local/postgres/data/PG_VERSION").is_file():
        sys.exit("找不到已有项目数据库。")
    subprocess.run([sys.executable, "scripts/dev_postgres.py", "start"], check=True)
    command = [
        sys.executable, "-m", "scripts.serve_web", "--live", "--unlimited", "--keep-web-available",
        "--managed-proofs", MANAGED_PROOFS,
        "--current-model-sources", f"{PROOFS}/official-model-verified",
        "--journal", ".local/stage40-web-live/api", "--request-limit", "202",
        "--port", str(port), "--semantic-review", "--execution-engine", "langgraph",
    ]
    if not (ROOT / PROOFS / "official-model-verified").is_dir():
        from scripts.first_run import configure_local_database
        configure_local_database()
        command = [sys.executable, "-m", "scripts.serve_web", "--personal", "--live", "--port", str(port)]
    if address:
        command.extend(["--lan-host", address])
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()

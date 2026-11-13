"""本地 MCP 自检：验证规则服务可被真实 MCP 客户端发现并调用。

⚠️ 这不是 Nexent 平台证据。平台侧「Agent 调试/发布入口发起调用」的记录，
只能在有可用 Nexent 实例后由 run_nexent_eval.py 产生。本脚本只证明
MCP 服务本身可启动、工具签名正确、命中与零命中两种返回都符合预期。

同时逐个实测 HTTP transport，把成功与失败都如实写入证据文件：
- sse：旧式 HTTP/SSE，Nexent Remote URL 文档提到的形式；
- streamable-http：MCP SDK 2.3.0 的现代 HTTP transport。

每次自检还记录：启动前端口是否空闲、本次启动的 PID、netstat 读到的监听 PID
（确认端口确实属于本次进程），以及 ExceptionGroup 展开后的**最内层错误**。
旧报告只保留外层异常组文本，不足以判定失败原因。
"""

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"

PORTFOLIO = {
    "id": "P-STOCK",
    "holdings": [
        {"symbol": "SIM-A", "stop_loss": "95.00", "take_profit": "106.00"}
    ],
}

TRANSPORTS = (
    {"name": "sse", "path": "/sse", "offset": 0},
    {"name": "streamable-http", "path": "/mcp", "offset": 1},
)


def innermost_errors(exc: BaseException, limit: int = 16) -> list[str]:
    """把 ExceptionGroup 展开到叶子异常，并沿 cause/context 链继续下钻。

    旧报告只留外层「unhandled errors in a TaskGroup (1 sub-exception)」，
    看不到真正的 404 / 连接关闭原因，不能据此推断失败机理。
    """
    leaves: list[str] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending and len(leaves) < limit:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        nested = getattr(current, "exceptions", None)
        if nested:
            pending = list(nested) + pending
            continue
        leaves.append(f"{type(current).__name__}: {current}")
        for related in (current.__cause__, current.__context__):
            if related is not None and related is not current:
                pending.append(related)
    return leaves


def port_is_free(port: int) -> bool:
    probe_socket = socket.socket()
    try:
        probe_socket.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe_socket.close()


def listener_pids(port: int) -> list[str]:
    """从 netstat 读取该端口 LISTENING 的 PID，确认服务是本次启动的进程。"""
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=6,
        )
    except Exception:  # noqa: BLE001 - 非 Windows / 命令不可用时标记 unverified
        return []
    pids = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or "LISTENING" not in parts:
            continue
        local = parts[1]
        if ":" in local and local.rsplit(":", 1)[1] == str(port):
            pids.add(parts[-1])
    return sorted(pids)


def wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"服务在 {timeout} 秒内未监听 {port} 端口")


def wait_for_port_release(port: int, timeout: float = 6.0) -> bool:
    """终止本次启动的进程后端口不再可连，据此确认监听归属本次启动链。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                time.sleep(0.2)
        except OSError:
            return True
    return False


async def probe(url: str, transport: str) -> dict:
    from mcp import ClientSession

    if transport == "sse":
        from mcp.client.sse import sse_client
        context = sse_client(url, timeout=10.0)
    else:
        from mcp.client.streamable_http import streamable_http_client
        context = streamable_http_client(url)

    async with context as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [
                {
                    "name": tool.name,
                    "annotations": tool.annotations.model_dump(exclude_none=True)
                    if tool.annotations else None,
                }
                for tool in tools.tools
            ]

            async def call(price: str):
                arguments = {
                    "portfolio": PORTFOLIO,
                    "snapshot": {
                        "timestamp": "2026-11-09T02:00:00Z",
                        "prices": {"SIM-A": price},
                    },
                }
                response = await session.call_tool(
                    "evaluate_portfolio_snapshot", arguments
                )
                payload = response.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                return {
                    "arguments": arguments,
                    "is_error": bool(payload.get("isError", False)),
                    "signals": payload.get("structuredContent", {}).get("result", []),
                    "content": payload.get("content", []),
                }

            return {
                "tools": names,
                "hit": await call("95.00"),
                "zero_hit": await call("100.00"),
            }


def run_one(transport: str, port: int, path: str,
            startup_wait: float = 1.0) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    free_before = port_is_free(port)
    command = [
        sys.executable, "-m", "mcps.portfolio_rules.server",
        "--transport", transport, "--port", str(port),
    ]
    entry = {
        "transport": transport,
        "url": url,
        "server_command": " ".join(command[1:]),
        "platform_evidence": False,
        "port_free_before_start": free_before,
        "port_owner_verified": None,
    }
    if not free_before:
        entry["pass"] = False
        entry["error"] = (
            f"端口 {port} 在本次启动前已被占用，无法确认监听者属于本次进程；"
            "请用 --base-port 换一组端口重试")
        return entry

    proc = subprocess.Popen(
        command, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    entry["started_pid"] = proc.pid
    try:
        wait_for_port(port)
        pids = listener_pids(port)
        entry["listener_pids"] = pids
        entry["port_pid_matches_started_pid"] = bool(pids) and str(proc.pid) in pids
        # uvicorn 的 TCP listen 早于 ASGI application startup。
        time.sleep(startup_wait)
        entry.update(probe_result := asyncio.run(probe(url, transport)))
        entry["pass"] = (
            any(t["name"] == "evaluate_portfolio_snapshot" for t in probe_result["tools"])
            and not probe_result["hit"]["is_error"]
            and len(probe_result["hit"]["signals"]) == 1
            and probe_result["hit"]["signals"][0]["rule"] == "stop_loss"
            and not probe_result["zero_hit"]["is_error"]
            and probe_result["zero_hit"]["signals"] == []
        )
    except Exception as exc:
        entry["pass"] = False
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["innermost_errors"] = innermost_errors(exc)
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate(timeout=5)
        if not entry.get("pass") and err:
            entry["server_stderr_tail"] = err[-800:]
            entry["http_error_evidence"] = [
                line.strip() for line in err.splitlines()
                if "HTTPStatusError" in line or "404 Not Found" in line
            ][:5]
        entry["port_released_after_terminate"] = wait_for_port_release(port)
        entry["port_owner_verified"] = bool(
            free_before and entry["port_released_after_terminate"])
        entry["port_owner_method"] = (
            "启动前端口空闲；启动后 netstat 记录监听 PID；"
            "终止本次启动进程后端口释放，据此确认端口归属本次启动链")
        if not entry.get("port_pid_matches_started_pid"):
            entry["port_owner_note"] = (
                "venv python.exe 是启动器，实际监听在子进程；"
                "不按 PID 相等硬判，改用终止后端口释放作为归属判据")
        if entry.get("pass") and not entry["port_owner_verified"]:
            entry["pass"] = False
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-port", type=int, default=8768)
    parser.add_argument("--startup-wait", type=float, default=1.0,
                        help="TCP listen 后等待 ASGI startup 的秒数")
    parser.add_argument("--output", type=Path, default=RESULTS / "mcp_smoke.json")
    args = parser.parse_args()

    report = {
        "scope": "local-mcp-smoke",
        "platform_evidence": False,
        "note": "本地 MCP 客户端直连服务的自检；不是 Nexent Agent 调试/发布入口的调用记录。",
        "sdk": "mcp==2.3.0",
        "transports": [],
    }
    report["startup_wait_seconds"] = args.startup_wait
    for item in TRANSPORTS:
        entry = run_one(item["name"], args.base_port + item["offset"],
                        item["path"], args.startup_wait)
        report["transports"].append(entry)
        print(f"[{entry['transport']}] {entry['url']}  PASS={entry['pass']}")
        if entry.get("tools"):
            print(f"  tools: {[t['name'] for t in entry['tools']]}")
            print(f"  命中 -> {len(entry['hit']['signals'])} 条；零命中 -> {len(entry['zero_hit']['signals'])} 条")
        if entry.get("error"):
            print(f"  error: {entry['error'][:180]}")
        for leaf in entry.get("innermost_errors", [])[:5]:
            print(f"    innermost: {leaf[:200]}")
        print(f"  端口归属: started_pid={entry.get('started_pid')} "
              f"listener_pids={entry.get('listener_pids')} "
              f"verified={entry.get('port_owner_verified')}")

    report["pass"] = any(entry["pass"] for entry in report["transports"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"整体：{'至少一条 HTTP transport 完成发现+命中+零命中' if report['pass'] else '全部失败'}")
    print(f"证据：{args.output}")
    print("注意：这是本地 MCP 自检，不是 Nexent 平台记录。")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

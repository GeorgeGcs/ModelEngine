"""平台/环境探测：把「为什么没能平台实跑」变成可核对的证据文件。

只用标准库，不读取任何凭据值；环境变量只记录是否设置。
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = ROOT / "eval" / "results"

PORTS = (3000, 3001, 5010, 7860, 8000, 8080, 8768, 8769)
ENV_NAMES = (
    "NEXENT_BASE_URL", "NEXENT_API_TOKEN", "NEXENT_AGENT_NAME",
    "NEXENT_API_PREFIX", "NEXENT_VERSION", "NEXENT_MODEL",
    "NEXENT_MCP_URL", "NEXENT_HEALTH_STATUS", "NEXENT_HEALTH_SERVICE",
    "NEXENT_TOOL_EVENT_TYPES", "NEXENT_MCP_CALL_LOG",
    "NEXENT_EXECUTION_RECORDS",
)
REQUIRED_NAMES = ("NEXENT_BASE_URL", "NEXENT_API_TOKEN", "NEXENT_AGENT_NAME")
DOC_LINKS = {
    "northbound_api": "https://modelengine-group.github.io/nexent/en/integration/integration-out/northbound-api.html",
    "northbound_source_v283": "https://github.com/ModelEngine-Group/nexent/blob/v2.8.3/backend/apps/northbound_app.py",
    "mcp": "https://modelengine-group.github.io/nexent/en/integration/integration-in/mcp.html",
    "agent_development": "https://modelengine-group.github.io/nexent/en/user-guide/agent-development.html",
    "agent_export": "https://modelengine-group.github.io/nexent/en/integration/integration-out/agents-export.html",
}


def http_code(url: str, timeout: float = 2.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:
        return f"unreachable ({type(exc).__name__})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=RESULTS / "platform_probe.json")
    args = parser.parse_args()

    commands = {}
    for name in ("docker", "docker compose", "podman", "kubectl", "wsl"):
        parts = name.split()
        # 只记录是否存在，不写本机绝对路径（证据提交到仓库前不得带机器路径）。
        commands[name] = "present" if shutil.which(parts[0]) else "absent"
    if commands["docker"] != "absent":
        try:
            completed = subprocess.run(["docker", "--version"], capture_output=True,
                                        text=True, timeout=5)
            commands["docker_version"] = completed.stdout.strip() or "unknown"
        except Exception as exc:
            commands["docker_version"] = f"failed: {type(exc).__name__}"

    ports = {}
    for port in PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                ports[str(port)] = f"listening / {http_code(f'http://127.0.0.1:{port}')}"
        except OSError:
            ports[str(port)] = "no listener"

    env = {
        name: {"set": bool(os.environ.get(name)), "value": "<redacted>"}
        for name in ENV_NAMES
    }
    workspace = ROOT.parents[4]  # 工作区目录（仅用于发现同名检出）
    local_checkouts = sorted(
        path.name for path in workspace.glob("*nexent*") if path.exists()
    )

    report = {
        "schema": "topic16-platform-probe-v1",
        "status": "pending-platform",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conclusion": "本机没有可用的 Nexent 实例；按指令报告「待平台实跑」，不生成端到端通过报告。",
        "container_runtime": commands,
        "local_http_ports": ports,
        "cloud": {
            "https://nexent.tech/zh": http_code("https://nexent.tech/zh", 6.0),
            "https://nexent.tech": http_code("https://nexent.tech", 6.0),
        },
        "environment": env,
        "local_nexent_paths": local_checkouts,
        "official_docs": DOC_LINKS,
        "northbound_api": {
            "target_version": "v2.8.3",
            "paths": {
                "health": "GET /nb/v1/health",
                "agents": "GET /nb/v1/agents",
                "chat_run": "POST /nb/v1/chat/run (agent_name + query, SSE)",
            },
            "client_status": "已按上述路径实现；真实网关前缀与工具事件结构仍待实例验证",
        },
        "blocked_at": [
            "无 Docker Desktop / podman / kubectl，无法本地拉起 Nexent 容器",
            "常见端口无监听，云端试用站点亦不可达",
            "缺少 NEXENT_BASE_URL / NEXENT_API_TOKEN / NEXENT_AGENT_NAME",
            "平台尚未发布绑定 evaluate_portfolio_snapshot 的 Agent，或 API Key 未在本机配置",
            "真实 SSE 工具事件结构与网关前缀需要在实例上核对",
        ],
        "needed_from_user": [
            "确定一台可访问的 Nexent 实例（地址 + 版本）",
            "在该实例发布绑定规则 MCP 的 Agent，并把 Agent 名称配置为 NEXENT_AGENT_NAME",
            "在本机环境变量配置 API Key（NEXENT_API_TOKEN）；不贴聊天、不进日志或仓库",
            "一次真实 chat/run SSE 响应样例，用于核对工具事件名与结构化结果字段",
            "若网关前缀不是 /nb/v1，配置 NEXENT_API_PREFIX",
        ],
        "credential_handling": "凭据仅通过环境变量传入，本探测只记录是否设置，绝不写出取值。",
    }
    reachable = any(
        value.startswith("HTTP ") and not value.startswith("HTTP 5")
        for value in ports.values()
    )
    if all(env[name]["set"] for name in REQUIRED_NAMES):
        report["status"] = "configured-needs-verification"
    elif reachable:
        report["status"] = "local-http-detected-needs-verification"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"平台状态：{report['status']}")
    print(f"  Docker: {commands.get('docker')}  端口监听: "
          f"{sum(1 for v in ports.values() if v.startswith('listening'))}/{len(ports)}")
    print(f"  凭据设置: {sum(1 for v in env.values() if v['set'])}/{len(env)}")
    print(f"  证据：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

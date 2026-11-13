"""可替换的推送适配器（普通 Python 契约，非 MCP server）。

- PushGateway 是唯一契约：send(recipient_id, message) -> (status, failure_reason)。
- LocalMockGateway 明确标 simulated=True、gateway_id="local-mock"，
  只写本地回执，**不发起任何真实外部请求**，用于本地完整链路演示。
- WebhookGateway 是真实接入模板：URL / Token 只从运行机器的环境变量读取，
  不写进仓库、不进日志、不进回执。配置与平台接入边界见
  docs/本地演示与验收对照.md。

替换方式：实现同一个 PushGateway 契约，或用 build_gateway("webhook") 切换；
Service 只依赖契约，不依赖具体实现。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

SIMULATED_NOTICE = "本地模拟网关回执（SIMULATED，未真实发送）"


class PushGateway(Protocol):
    gateway_id: str
    simulated: bool

    def describe(self) -> dict: ...

    def send(self, recipient_id: str, message: str) -> tuple[str, str | None]: ...


class LocalMockGateway:
    """标识清楚的本地模拟网关：故障可注入，用于验证恢复后不重复送达。"""

    gateway_id = "local-mock"
    simulated = True

    def __init__(self, *, failure_mode: bool = False):
        self.failure_mode = failure_mode

    def set_failure_mode(self, enabled: bool) -> None:
        self.failure_mode = enabled

    def describe(self) -> dict:
        return {
            "gateway_id": self.gateway_id,
            "simulated": True,
            "failure_mode": self.failure_mode,
            "notice": SIMULATED_NOTICE,
            "real_delivery": False,
        }

    def send(self, recipient_id: str, message: str) -> tuple[str, str | None]:
        if self.failure_mode:
            return "failed", "模拟网关故障（注入）；未产生任何真实送达"
        return "delivered", None


class WebhookGateway:
    """真实接入模板：企业微信/自建网关 Webhook。

    环境变量（只在运行机器配置）：
      TOPIC16_WECHAT_WEBHOOK_URL   必填，HTTPS 地址
      TOPIC16_WECHAT_WEBHOOK_TOKEN 可选，Bearer Token
      TOPIC16_WECHAT_TIMEOUT       可选，秒，默认 8
    """

    gateway_id = "webhook"
    simulated = False

    def __init__(self, *, url: str | None = None, token: str | None = None,
                 timeout: float | None = None):
        self.url = url or os.environ.get("TOPIC16_WECHAT_WEBHOOK_URL", "")
        self.token = token or os.environ.get("TOPIC16_WECHAT_WEBHOOK_TOKEN", "")
        self.timeout = float(timeout or os.environ.get("TOPIC16_WECHAT_TIMEOUT", "8"))

    def describe(self) -> dict:
        # 只报是否配置，绝不回显 URL / Token。
        return {
            "gateway_id": self.gateway_id,
            "simulated": False,
            "url_configured": bool(self.url),
            "token_configured": bool(self.token),
            "timeout_seconds": self.timeout,
            "real_delivery": False,
            "delivery_confirmation_supported": False,
            "notice": "Webhook 仅核对业务受理，不提供终端送达确认",
        }

    def send(self, recipient_id: str, message: str) -> tuple[str, str | None]:
        if not self.url:
            return "failed", "未配置 TOPIC16_WECHAT_WEBHOOK_URL；请先在部署机设置环境变量"
        if not self.url.startswith("https://"):
            return "failed", "Webhook 必须使用 HTTPS"
        payload = json.dumps({"recipient": recipient_id, "message": message},
                             ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=payload, headers=headers,
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if 200 <= response.status < 300:
                    try:
                        body = json.loads(response.read(65537))
                    except (ValueError, UnicodeError):
                        return "unknown", "网关响应不是有效 JSON；不得自动重发"
                    if not isinstance(body, dict):
                        return "unknown", "网关响应缺少业务对象；不得自动重发"
                    if (body.get("errcode") not in (None, 0)
                            or body.get("success") is False
                            or body.get("status") in ("failed", "error", "rejected")):
                        return "failed", "网关明确拒绝业务请求"
                    if (response.status == 202 or body.get("errcode") == 0
                            or body.get("status") in ("accepted", "queued", "delivered")):
                        return "accepted", "网关已受理；未取得终端送达确认"
                    return "unknown", "HTTP 成功但缺少已识别业务受理标记"
                return "failed", f"网关返回 HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            return ("failed" if 400 <= exc.code < 500 else "unknown"), f"网关 HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # 不在日志里回显带凭据的 URL 或完整异常对象。
            return "unknown", f"发送结果不确定：{type(exc).__name__}；不得自动重发"


def build_gateway(name: str = "local-mock", **kwargs) -> PushGateway:
    if name in ("local-mock", "mock", "local"):
        return LocalMockGateway(**kwargs)
    if name in ("webhook", "real"):
        return WebhookGateway(**kwargs)
    raise ValueError(f"未知网关：{name}；可选 local-mock / webhook")


def mcp_tool_send_risk_notification(*, recipient_id: str, message: str,
                                    gateway: str = "local-mock") -> dict:
    """普通 Python 兼容入口（名称保留）；不是已注册或可启动的 MCP server。

    返回体显式带 simulated / real_delivery，避免把模拟回执当成真实送达。
    """
    adapter = build_gateway(gateway)
    status, reason = adapter.send(recipient_id, message)
    return {
        "recipient_masked": None,  # 由 Service 负责脱敏后落库，工具不回显原文
        "status": status,
        "failure_reason": reason,
        "gateway": adapter.describe(),
        "disclaimer": "风险提示仅供参考，不构成投资建议。",
    }

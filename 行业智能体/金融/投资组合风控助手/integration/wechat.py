"""Authorize dispatch of persisted risk events through the published Nexent Agent."""

import hashlib
import hmac
import json
import os
import time

from .dispatch import read_key


TOOL = "send_wechat_risk_notification"


def signature(key, event_id, run_id, expires, delivery):
    data = json.dumps([TOOL, event_id, run_id, expires, delivery], separators=(",", ":")).encode()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def authorize(event_id, run_id, delivery):
    expires = int(time.time()) + 60
    key = read_key(os.environ["TOPIC16_WECHAT_MCP_KEY_FILE"])
    return f"{expires}.{signature(key, event_id, run_id, expires, delivery)}"


def verify_authorization(key, event_id, run_id, authorization, delivery):
    expires, _, mac = authorization.partition(".")
    if not (expires.isascii() and expires.isdecimal() and len(expires) <= 12):
        return False
    expires = int(expires)
    now = int(time.time())
    return (now <= expires <= now + 60
            and hmac.compare_digest(signature(key, event_id, run_id, expires, delivery), mac))


class NexentWeChatGateway:
    """Scheduler-side descriptor. Outbound calls are made via the native adapter."""

    gateway_id = "nexent-wechat"
    simulated = False

    def describe(self):
        return {"gateway_id": self.gateway_id, "simulated": False, "real_delivery": False}

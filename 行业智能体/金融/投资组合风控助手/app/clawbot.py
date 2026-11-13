"""ClawBot outbound transport and the operator's phone confirmation command."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from .store import Store


CHANNEL = "openclaw-weixin"


class ClawBotGateway:
    gateway_id = "clawbot"
    simulated = False

    def __init__(self):
        self.command = json.loads(os.getenv("TOPIC16_CLAWBOT_COMMAND_JSON", '["openclaw"]'))
        self.account = os.getenv("TOPIC16_CLAWBOT_ACCOUNT", "")
        self.target = os.getenv("TOPIC16_CLAWBOT_TARGET", "")
        self.recipient = os.getenv("TOPIC16_RECIPIENT_ID", "")
        if (not isinstance(self.command, list) or not self.command
                or not all(isinstance(item, str) and item for item in self.command)
                or Path(self.command[0]).suffix.lower() in (".cmd", ".bat")
                or not all(value.strip() for value in (self.account, self.target, self.recipient))):
            raise ValueError("ClawBot 缺少私有命令、账号、会话或接收人配置")

    def describe(self):
        return {"gateway_id": self.gateway_id, "simulated": False, "real_delivery": False}

    def send(self, recipient_id, message):
        status, reason, _ = self.send_with_receipt(recipient_id, message, "")
        return status, reason

    def send_with_receipt(self, recipient_id, message, attempt_id):
        if recipient_id != self.recipient:
            return "failed", "接收人不在 ClawBot 配置中", None
        message = f"{message}\n送达核对码：{attempt_id}" if attempt_id else message
        if len(message.encode("utf-16-le")) > 8000:
            return "failed", "消息超过单条发送长度", None
        args = [*self.command, "message", "send", "--channel", CHANNEL,
                "--account", self.account, "--target", self.target,
                "--message", message, "--json"]
        try:
            process = subprocess.run(args, capture_output=True, timeout=25, check=False)
        except FileNotFoundError:
            return "failed", "ClawBot 命令不可用", None
        except (OSError, subprocess.TimeoutExpired):
            return "unknown", "ClawBot 命令结果不确定", None
        message_id = accepted_message_id(process)
        if message_id is None:
            return "unknown", "ClawBot 缺少可核对的发送回执", None
        return "accepted", None, message_id


def accepted_message_id(process):
    try:
        data = json.loads(process.stdout)
    except (ValueError, UnicodeError):
        return None
    if (process.returncode or not isinstance(data, dict) or data.get("ok") is False
            or data.get("dryRun") is not False or data.get("action") != "send"
            or data.get("channel") != CHANNEL):
        return None
    payload = data.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    for part in (data, payload, result):
        if (part.get("ok") is False
                or part.get("outcome") in ("not_sent", "suppressed", "failed", "partial")
                or part.get("deliveryStatus") in ("not_sent", "suppressed", "failed", "partial")):
            return None
    value = data.get("messageId") or payload.get("messageId") or result.get("messageId")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else None


def main():
    parser = argparse.ArgumentParser(description="Record a code read from the recipient's phone")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--phone-code", required=True)
    parser.add_argument("--observer", required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("existing delivery database required")
    confirmed = Store(args.db).confirm_clawbot_phone(args.phone_code, args.observer)
    if not confirmed:
        parser.error("no unconfirmed eligible ClawBot attempt matches that code")
    print(json.dumps(confirmed, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Signed, single-use tool requests shared by the application and Nexent adapter."""
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from uuid import uuid4


PREFIX = 'TOPIC16_WORKFLOW_V1 '
LIFETIME = 60
MAX_PAYLOAD = 128 * 1024
TOOL_ARGUMENTS = {
    'evaluate_portfolio_snapshot': {'portfolio': dict, 'snapshot': dict},
    'assess_news_risk_v2': {'news': dict, 'entities': dict},
    'search_portfolio_knowledge': {'query': str, 'run_id': str, 'task_key': str},
    'send_wechat_risk_notification': {'event_id': str, 'run_id': str, 'authorization': str},
}
CORE_TOOLS = TOOL_ARGUMENTS.keys() - {'send_wechat_risk_notification'}


def read_key(path):
    key = Path(path).read_bytes()
    if len(key) != 32:
        raise ValueError('workflow key must contain 32 bytes')
    return key


def validate_arguments(tool, arguments):
    expected = TOOL_ARGUMENTS.get(tool)
    if expected is None or not isinstance(arguments, dict) or arguments.keys() != expected.keys():
        raise ValueError('invalid workflow tool or arguments')
    if any(type(arguments[name]) is not kind or not arguments[name] for name, kind in expected.items()):
        raise ValueError('invalid workflow argument value')


def encode_request(key, agent, tool, arguments, *, now=None):
    validate_arguments(tool, arguments)
    nonce = uuid4().hex
    data = {'agent': agent, 'tool': tool, 'arguments': arguments, 'nonce': nonce,
            'expires': int(time.time() if now is None else now) + LIFETIME}
    body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(body) > MAX_PAYLOAD:
        raise ValueError('workflow request exceeds size limit')
    encoded = base64.urlsafe_b64encode(body).decode('ascii')
    return PREFIX + encoded + '.' + hmac.digest(key, body, 'sha256').hex(), nonce


def decode_request(key, agent, text, *, now=None):
    # Nexent adds a time banner ahead of the original task.
    matches = [line for line in text.splitlines() if line.startswith(PREFIX)]
    if not matches:
        return None
    if len(matches) != 1 or len(matches[0]) > 2 * MAX_PAYLOAD:
        raise ValueError('invalid workflow request')
    token = matches[0][len(PREFIX):]
    encoded, separator, signature = token.partition('.')
    if not separator or not re.fullmatch(r'[0-9a-f]{64}', signature):
        raise ValueError('invalid workflow signature')
    try:
        body = base64.b64decode(encoded, altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('invalid workflow encoding') from None
    if len(body) > MAX_PAYLOAD or not hmac.compare_digest(hmac.digest(key, body, 'sha256').hex(), signature):
        raise ValueError('invalid workflow signature')
    data = json.loads(body)
    if not isinstance(data, dict) or data.keys() != {'agent', 'tool', 'arguments', 'nonce', 'expires'}:
        raise ValueError('invalid workflow request')
    clock = int(time.time() if now is None else now)
    if (data['agent'] != agent or type(data['expires']) is not int
            or not clock <= data['expires'] <= clock + LIFETIME
            or not isinstance(data['nonce'], str) or not re.fullmatch(r'[0-9a-f]{32}', data['nonce'])
            or not isinstance(data['tool'], str)):
        raise ValueError('invalid workflow identity or expiry')
    validate_arguments(data['tool'], data['arguments'])
    return data


def action_digest(nonce):
    return hashlib.sha256(nonce.encode('ascii')).hexdigest()

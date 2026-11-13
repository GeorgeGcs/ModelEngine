"""Evaluate price levels independently of model output and delivery state."""

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} 须为非空字符串，首尾不可有空白")
    return value


def _price(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{field} 须为精确的十进制字符串或整数")
    if isinstance(value, str) and not re.fullmatch(
        r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value
    ):
        raise ValueError(f"{field} 不是有效的十进制价格")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} 不是有效价格") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{field} 须为有限正数")
    return amount


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp 须为带时区的 ISO 时间")
    # Retain fractional seconds beyond datetime's microsecond precision.
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(?:[.,](\d+))?"
        r"(Z|[+-]\d{2}:?\d{2})", value
    )
    if not match:
        raise ValueError("timestamp 须含日期、时间及 UTC 偏移，例如 2026-11-07T09:30:00+08:00")
    clock, fraction, offset = match.groups()
    if fraction and clock.count(":") != 2:
        raise ValueError("timestamp 的小数部分须跟随秒数")
    if offset != "Z" and int(offset[-2:]) >= 60:
        raise ValueError("timestamp 偏移分钟数无效")
    try:
        utc = datetime.fromisoformat(clock + offset).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError("timestamp 不是有效的带时区时间") from exc
    fraction = (fraction or "").rstrip("0")
    seconds = utc.replace(tzinfo=None).isoformat(timespec="seconds")
    return seconds + ("." + fraction if fraction else "") + "Z"


def evaluate_snapshot(portfolio: dict, snapshot: dict) -> list[dict]:
    """Return signals in holding order. The same instant and rule yield the same ID."""
    if not isinstance(portfolio, dict) or not isinstance(snapshot, dict):
        raise ValueError("portfolio 和 snapshot 须为对象")
    portfolio_id = _identifier(portfolio.get("id"), "portfolio.id")
    timestamp = _utc_timestamp(snapshot.get("timestamp"))
    holdings = portfolio.get("holdings")
    prices = snapshot.get("prices")
    if not isinstance(holdings, list) or not isinstance(prices, dict):
        raise ValueError("holdings 须为数组，prices 须为按标的索引的对象")
    amounts = {
        _identifier(symbol, "prices 标的"): _price(value, f"prices[{symbol}]")
        for symbol, value in prices.items()
    }

    seen = set()
    signals = []
    for holding in holdings:
        if not isinstance(holding, dict):
            raise ValueError("每一项持仓须为对象")
        symbol = _identifier(holding.get("symbol"), "holding.symbol")
        if symbol in seen:
            raise ValueError(f"持仓标的重复：{symbol}")
        seen.add(symbol)
        if symbol not in amounts:
            raise ValueError(f"缺少标的行情：{symbol}")
        lower = _price(holding.get("stop_loss"), f"{symbol}.stop_loss")
        upper = _price(holding.get("take_profit"), f"{symbol}.take_profit")
        if lower >= upper:
            raise ValueError(f"{symbol} 的止损阈值须低于止盈阈值")
        price = amounts[symbol]
        if price <= lower:
            rule, threshold = "stop_loss", lower
        elif price >= upper:
            rule, threshold = "take_profit", upper
        else:
            continue
        identity = json.dumps([portfolio_id, timestamp, symbol, rule], separators=(",", ":"))
        signals.append({
            "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "portfolio_id": portfolio_id,
            "timestamp": timestamp,
            "symbol": symbol,
            "rule": rule,
            "price": str(price),
            "threshold": str(threshold),
        })
    return signals

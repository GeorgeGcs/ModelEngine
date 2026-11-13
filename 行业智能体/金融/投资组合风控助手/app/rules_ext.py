"""组合风险判定：复用价格规则与舆情 v2 规则，补齐单日异动/集中度/流动性。

判定全部由确定性代码完成，模型不参与阈值比较。所有输出均为辅助建议。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from news_risk.rules_v2 import assess_news_v2
from portfolio_risk.rules import evaluate_snapshot

DISCLAIMER = "风险提示仅供参考，不构成投资建议；系统输出为辅助建议，重大风险须人工核实。"

DAILY_MOVE_THRESHOLD = Decimal("0.05")     # 单日涨跌幅绝对值 ≥5%
CONCENTRATION_THRESHOLD = Decimal("0.60")  # 单一标的市值占比 ≥60%

RISK_CATALOG = [
    {"id": "stop_loss", "name": "止损阈值触发", "implemented": True,
     "source": "portfolio_risk.rules.evaluate_snapshot（冻结评测集同一实现）",
     "verification": "离线回放 20 个触发点 TP=20 FP=0 FN=0；本地演示三组合各触发一条",
     "level": "高", "review_action": "核对行情与持仓成本，人工确认是否需调整仓位"},
    {"id": "take_profit", "name": "止盈阈值触发", "implemented": True,
     "source": "portfolio_risk.rules.evaluate_snapshot（冻结评测集同一实现）",
     "verification": "离线回放 20 个触发点覆盖；本地演示与单测覆盖",
     "level": "中", "review_action": "核对止盈线与当日成交，人工确认是否分批了结"},
    {"id": "daily_move", "name": "单日异动", "implemented": True,
     "source": "app/rules_ext.py：上一快照收盘价与当前价的确定性百分比比较",
     "verification": "app/tests/test_app.py 覆盖触发、不触发与缺上一快照三种情形；"
                     "演示中给出前后两次快照",
     "level": "中", "review_action": "核对涨跌幅与公告，排除数据异常后人工复核"},
    {"id": "concentration", "name": "集中度", "implemented": True,
     "source": "app/rules_ext.py：按当前价×数量计算单一标的市值占组合比例",
     "verification": "app/tests/test_app.py 覆盖超阈值与分散组合；演示中混合组合可复现",
     "level": "中", "review_action": "核对组合市值分布，人工评估分散度"},
    {"id": "liquidity", "name": "流动性", "implemented": True,
     "source": "app/rules_ext.py：模拟成交量与该标的持仓数量比较（演示口径，非真实盘口）",
     "verification": "app/tests/test_app.py 覆盖成交量不足与充足；演示给出模拟成交量",
     "level": "中", "review_action": "核对模拟成交量与持仓规模，人工评估变现能力"},
    {"id": "news_risk", "name": "舆情风险", "implemented": True,
     "source": "news_risk.rules_v2.assess_news_v2（同一 v2 研判规则）",
     "verification": "舆情开发评测 50/50 仅记与 AI 参考答案一致率；正式人工一致率仍待盲评；"
                     "本演示只证明链路可调用、原文证据可回查",
     "level": "按研判结果", "review_action": "按研判结果给出的人工复核动作"},
]


def _decimal(value: object, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是有效数值") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} 须为有限数值")
    return amount


def _event_id(portfolio_id: str, timestamp: str, symbol: str,
              risk_type: str, evidence: str) -> str:
    """不含 run_id：同一快照、同一事件重复监控得到同一 ID，推送天然幂等。"""
    identity = json.dumps([portfolio_id, timestamp, symbol, risk_type, evidence],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _signal(portfolio: dict, timestamp: str, symbol: str, risk_type: str,
            name: str, evidence: str, level: str, review_action: str,
            detail: dict) -> dict:
    return {
        "event_id": _event_id(portfolio["id"], timestamp, symbol, risk_type, evidence),
        "portfolio_id": portfolio["id"],
        "portfolio_name": portfolio["name"],
        "portfolio_type": portfolio["type"],
        "symbol": symbol,
        "risk_type": risk_type,
        "risk_type_name": name,
        "trigger_time": timestamp,
        "evidence": evidence,
        "level": level,
        "review_action": review_action,
        "detail": detail,
        "disclaimer": DISCLAIMER,
    }


def assess_news_items(portfolio,news_items):
    records=[];symbols={h['symbol'] for h in portfolio['holdings']}
    for item in news_items or []:
        if item.get('symbol') not in symbols:continue
        symbol=item['symbol'];profiles=item.get('entity_profiles') or {
            symbol:{'canonical':item.get('name',f'虚构标的{symbol}'),'aliases':[symbol]}}
        news={k:item[k] for k in ('symbol','text','sample_id','published_at','source') if k in item}
        record={'scope':'local-rules','news':news,'entities':profiles}
        try:record.update(status='success',result=assess_news_v2(news,profiles))
        except ValueError:record.update(status='failed',error_type='invalid-news-input',result=None)
        records.append(record)
    return records


def news_signals(portfolio,timestamp,records):
    signals=[]
    for record in records:
        assessment=record.get('result');news=record['news']
        if record['status']!='success' or assessment['risk_verdict']!='risk':continue
        evidence=f"舆情原文：{news['text']}（证据片段：{assessment['evidence']}）"
        signals.append(_signal(portfolio,timestamp,news['symbol'],'news_risk','舆情风险',evidence,
            assessment['level'],assessment['review_action'],{'evidence_status':assessment['evidence_status'],
            'profile':assessment['profile'],'reason_codes':assessment['reason_codes'],
            'source':record['scope'],'news_input':news,'assessment':assessment,
            'tool_evidence':record.get('tool_evidence')}))
    return signals


def build_signals(portfolio: dict, snapshot: dict, *,
                  previous_prices: dict | None = None,
                  market: dict | None = None,
                  news_items: list[dict] | None = None, news_records=None) -> list[dict]:
    """返回本组合在该快照下的全部风险信号（按持仓顺序 + 组合级 + 舆情）。"""
    if not isinstance(snapshot, dict) or "timestamp" not in snapshot \
            or "prices" not in snapshot:
        raise ValueError("快照须含 timestamp 与 prices")
    timestamp = snapshot["timestamp"]
    prices = snapshot["prices"]
    if not isinstance(prices, dict) or not prices:
        raise ValueError("prices 须为按标的索引的非空对象")
    holdings = portfolio["holdings"]
    market = market or {}
    previous_prices = previous_prices or {}
    signals: list[dict] = []

    # ① 止盈止损：直接复用冻结评测集使用的同一规则实现。
    rule_input = {
        "id": portfolio["id"],
        "holdings": [{"symbol": h["symbol"], "stop_loss": h["stop_loss"],
                      "take_profit": h["take_profit"]} for h in holdings],
    }
    for raw in evaluate_snapshot(rule_input, {"timestamp": timestamp, "prices": prices}):
        rule = raw["rule"]
        catalog = next(c for c in RISK_CATALOG if c["id"] == rule)
        evidence = (f"{raw['symbol']} 快照价 {raw['price']}，"
                    f"{'止损' if rule == 'stop_loss' else '止盈'}线 {raw['threshold']}")
        signals.append(_signal(
            portfolio, timestamp, raw["symbol"], rule, catalog["name"], evidence,
            catalog["level"], catalog["review_action"],
            {"price": raw["price"], "threshold": raw["threshold"],
             "source_event_id": raw["event_id"], "source": "evaluate_snapshot"}))

    price_map = {h["symbol"]: _decimal(prices[h["symbol"]],
                                       f"prices[{h['symbol']}]") for h in holdings}

    # ② 单日异动：与上一快照比较，或由调用方直接给 daily_change_pct（演示/补数据用）。
    declared_change = market.get("daily_change_pct", {}) or {}
    for holding in holdings:
        symbol = holding["symbol"]
        current = price_map[symbol]
        change: Decimal | None = None
        if symbol in declared_change:
            change = _decimal(declared_change[symbol],
                              f"daily_change_pct[{symbol}]") / Decimal("100")
        elif symbol in previous_prices:
            previous = _decimal(previous_prices[symbol], f"previous[{symbol}]")
            if previous > 0:
                change = (current - previous) / previous
        if change is not None and abs(change) >= DAILY_MOVE_THRESHOLD:
            pct = (change * Decimal("100")).quantize(Decimal("0.01"))
            evidence = f"{symbol} 单日涨跌幅 {pct}%（前值 {previous_prices.get(symbol, '—')} → 现价 {current}）"
            signals.append(_signal(
                portfolio, timestamp, symbol, "daily_move", "单日异动", evidence,
                "中", "核对涨跌幅与公告，排除数据异常后人工复核",
                {"change_pct": str(pct), "threshold_pct": "5.00",
                 "previous": previous_prices.get(symbol), "current": str(current)}))

    # ③ 集中度：组合级，按市值占比。
    values = {h["symbol"]: price_map[h["symbol"]] * Decimal(str(h["quantity"]))
              for h in holdings}
    total = sum(values.values(), Decimal("0"))
    if total > 0:
        top_symbol = max(values, key=lambda key: values[key])
        ratio = values[top_symbol] / total
        if ratio >= CONCENTRATION_THRESHOLD:
            pct = (ratio * Decimal("100")).quantize(Decimal("0.01"))
            evidence = (f"组合市值 {total}，其中 {top_symbol} 占 {pct}%，"
                        f"达到集中度阈值 60%")
            signals.append(_signal(
                portfolio, timestamp, "(组合)", "concentration", "集中度", evidence,
                "中", "核对组合市值分布，人工评估分散度",
                {"top_symbol": top_symbol, "ratio_pct": str(pct),
                 "total_value": str(total), "threshold_pct": "60.00"}))

    # ④ 流动性：模拟成交量小于该标的持仓数量即视为变现能力不足（演示口径）。
    volumes = market.get("volumes", {}) or {}
    for holding in holdings:
        symbol = holding["symbol"]
        if symbol not in volumes:
            continue
        volume = _decimal(volumes[symbol], f"volumes[{symbol}]")
        quantity = Decimal(str(holding["quantity"]))
        if volume < quantity:
            evidence = (f"{symbol} 模拟成交量 {volume} 手 < 持仓 {quantity}，"
                        f"按演示口径判为流动性不足")
            signals.append(_signal(
                portfolio, timestamp, symbol, "liquidity", "流动性", evidence,
                "中", "核对模拟成交量与持仓规模，人工评估变现能力",
                {"simulated_volume": str(volume), "quantity": str(quantity),
                 "metric": "模拟成交量 < 持仓数量（演示口径，非真实盘口）"}))

    signals.extend(news_signals(portfolio,timestamp,
        assess_news_items(portfolio,news_items) if news_records is None else news_records))
    return signals

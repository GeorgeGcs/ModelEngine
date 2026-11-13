"""本地演示业务编排：录入/导入 → 立即监控 → 信号落库 → 推送 → 查询。

不手工插库、不预置成功状态：所有记录都由本模块的正常入口产生。
推送幂等、限频与故障恢复都在 Store 层以事务实现，Service 只调用契约。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .gateway import LocalMockGateway, PushGateway, build_gateway
from .rules_ext import DISCLAIMER, RISK_CATALOG, build_signals, assess_news_items
from .store import Store

DEMO_DATA = Path(__file__).resolve().parents[1] / "data" / "demo.json"
DEFAULT_RECIPIENT = "SIM-REVIEWER-01"  # 虚构接收人，仅用于本地演示


def beijing_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class Service:
    def __init__(self, store: Store | None = None,
                 gateway: PushGateway | None = None, *,
                 recipient_id: str = DEFAULT_RECIPIENT,
                 window_seconds: int = 60, max_per_window: int = 3, knowledge_client=None):
        self.store = store or Store()
        self.gateway = gateway or LocalMockGateway()
        self.recipient_id = recipient_id
        self.window_seconds = window_seconds
        self.max_per_window = max_per_window
        self.knowledge_client = knowledge_client

    # ---------- 录入 / 导入 / 修改 / 查询 ----------
    def portfolios(self) -> list[dict]:
        return self.store.list_portfolios()

    def upsert_portfolio(self, portfolio: dict) -> dict:
        return self.store.upsert_portfolio(portfolio)

    def set_holding(self, portfolio_id: str, holding: dict) -> dict:
        return self.store.set_holding(portfolio_id, holding)

    def import_portfolios(self, payload: dict | list) -> list[dict]:
        """批量导入：接受 {portfolios:[...]} 或数组；逐条走同一录入校验。"""
        items = payload.get("portfolios") if isinstance(payload, dict) else payload
        if not isinstance(items, list) or not items:
            raise ValueError("批量导入须提供非空 portfolios 数组")
        return [self.upsert_portfolio(item) for item in items]

    def import_demo_file(self, path: Path | str | None = None) -> list[dict]:
        """从 data/demo.json 读取三组合，只做录入，不写任何信号/推送状态。"""
        import json
        source = Path(path) if path else DEMO_DATA
        document = json.loads(source.read_text(encoding="utf-8-sig"))
        return self.import_portfolios(document)

    # ---------- 立即监控 ----------
    def monitor(self, *, prices: dict, timestamp: str | None = None,
                market: dict | None = None, news_items: list[dict] | None = None,
                portfolio_ids: list[str] | None = None,
                run_id: str | None = None, frozen_portfolios: list[dict] | None = None,
                knowledge_queries: list[dict] | None = None) -> dict:
        if not isinstance(prices, dict) or not prices:
            raise ValueError("监控须提供非空 prices")
        run_id = run_id or f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        timestamp = timestamp or beijing_now()
        portfolios = self.portfolios() if frozen_portfolios is None else frozen_portfolios
        if portfolio_ids:
            wanted = set(portfolio_ids)
            portfolios = [p for p in portfolios if p["id"] in wanted]
            missing = wanted - {p["id"] for p in portfolios}
            if missing:
                raise ValueError(f"组合不存在：{sorted(missing)}")
        if not portfolios:
            raise ValueError("尚无可监控组合；请先录入或导入持仓")
        queries = knowledge_queries or []
        if not isinstance(queries,list) or any(not isinstance(q,dict) or
                q.get('portfolio_id') not in {p['id'] for p in portfolios} or
                not isinstance(q.get('query'),str) or not q['query'].strip() for q in queries):
            raise ValueError('knowledge_queries 须含已选 portfolio_id 和非空 query')
        self.store.open_run(run_id, {"timestamp": timestamp,
                                     "execution_mode": "local-rules",
                                     "portfolio_ids": [p["id"] for p in portfolios],
                                     "gateway": self.gateway.describe()})
        self.store.save_component(run_id,'inputs','recorded',{'portfolios':portfolios,'prices':prices,
            'timestamp':timestamp,'market':market,'news_items':news_items,'knowledge_queries':queries})
        previous = self.store.previous_prices(timestamp)
        signal_rows: list[dict] = []
        push_rows: list[dict] = []
        knowledge_rows: list[dict] = []
        news_rows=[]
        for portfolio in portfolios:
            news=assess_news_items(portfolio,news_items);news_rows.extend(news)
            self.store.save_component(run_id,'news:'+portfolio['id'],
                'failed' if any(n['status']=='failed' for n in news) else 'success',news)
            references=[]
            for q in queries:
                if q['portfolio_id'] != portfolio['id']:continue
                from knowledge.client import KnowledgeClient
                client=self.knowledge_client or KnowledgeClient.from_env(self.store)
                references.append(client.retrieve(run_id=run_id,
                    task_key=portfolio['id']+'|'+timestamp,query=q['query']))
            knowledge_rows.extend(references)
            signals = build_signals(portfolio, {"timestamp": timestamp, "prices": prices},
                                    previous_prices=previous, market=market,
                                    news_items=news_items,news_records=news)
            for signal in signals:
                signal["run_id"] = run_id
                # 中文类别名随 detail 落库，查询接口与页面都能直接显示。
                signal.setdefault("detail", {})["risk_type_name"] = \
                    signal["risk_type_name"]
                signal['detail']['knowledge_retrieval_ids']=[r['retrieval_id'] for r in references]
                signal['detail']['knowledge_statuses']=[r['status'] for r in references]
                signal['detail']['knowledge_role']='supporting-reference; does not override input rules'
                created = self.store.insert_signal(signal)
                signal_rows.append({"event_id": signal["event_id"],
                                    "portfolio_id": portfolio["id"],
                                    "risk_type": signal["risk_type"],
                                    "risk_type_name": signal["risk_type_name"],
                                    "symbol": signal["symbol"],
                                    "signal_created": created})
                push_rows.append(self._push(signal))
        self.store.save_snapshot(run_id, timestamp, prices)
        review_required=any(r['status']!='hit' for r in knowledge_rows) or any(r['status']!='success' for r in news_rows)
        self.store.finish_run(run_id, "review_required" if review_required else "completed", len(signal_rows),
                              sum(1 for p in push_rows if p.get("attempted")))
        return {
            "run_id": run_id,
            "execution_mode": "local-rules",
            "timestamp": timestamp,
            "gateway": self.gateway.describe(),
            "signal_count": len(signal_rows),
            "signals": signal_rows,
            "push_count": len(push_rows),
            "pushes": push_rows,
            "knowledge": knowledge_rows,
            "news": news_rows,
            "news_review_required": any(r['status']!='success' for r in news_rows),
            "evidence_complete": all(r['status']=='success' for r in news_rows) and all(
                r['status'] in ('hit','no_match') for r in knowledge_rows),
            "knowledge_review_required": any(r['status']!='hit' for r in knowledge_rows),
            "counts": self.store.counts(),
            "disclaimer": DISCLAIMER,
        }

    def _push(self, signal: dict) -> dict:
        message = (
            f"[风险提示·{'模拟' if self.gateway.simulated else '网关转发'}] 组合 {signal['portfolio_name']}（{signal['portfolio_type']}）"
            f" | {signal['risk_type_name']} | 触发时间 {signal['trigger_time']}"
            f" | 标的 {signal['symbol']} | 等级 {signal['level']}"
            f" | 依据：{signal['evidence']} | 建议：{signal['review_action']}。"
            f" {DISCLAIMER}")
        record = self.store.push_begin(
            event_id=signal["event_id"], recipient_id=self.recipient_id,
            message=message, gateway_id=self.gateway.gateway_id,
            simulated=self.gateway.simulated,
            window_seconds=self.window_seconds,
            max_per_window=self.max_per_window)
        result = {
            "event_id": signal["event_id"],
            "push_id": record["id"],
            "status": record["status"],
            "duplicate": record["duplicate"],
            "suppressed": record["status"] == "suppressed",
            "recipient_masked": record["recipient_masked"],
            "attempted": False,
        }
        if record["created"] and record["status"] == "pending":
            claimed = self.store.push_retry(signal["event_id"], self.recipient_id,
                source="initial", window_seconds=self.window_seconds,
                max_per_window=self.max_per_window, gateway_id=self.gateway.gateway_id,
                simulated=self.gateway.simulated)
            if claimed:
                status, reason = self._send_claimed(claimed, self.recipient_id)
                result.update(status=status, failure_reason=reason, attempted=True,
                              attempt_id=claimed["attempt_id"])
            else:
                current = next(p for p in self.store.list_pushes(event_id=signal["event_id"])
                               if p["id"] == record["id"])
                result.update(status=current["status"], failure_reason=current["failure_reason"],
                              suppressed=current["status"] == "suppressed")
        elif record["created"]:
            result["failure_reason"] = record.get("failure_reason")
        return result

    def retry_push(self, event_id: str, *, recipient_id: str | None = None,
                   source: str = "manual") -> dict:
        """故障恢复：只重试 failed/pending 行；delivered / suppressed 不重发。"""
        recipient = recipient_id or self.recipient_id
        row = self.store.push_retry(event_id, recipient, window_seconds=self.window_seconds,
            max_per_window=self.max_per_window, gateway_id=self.gateway.gateway_id,
            simulated=self.gateway.simulated, source=source)
        if row is None:
            return {"event_id": event_id, "retried": False,
                    "reason": "无待重试记录，或该事件已送达/已抑制；不重复送达"}
        status, reason = self._send_claimed(row, recipient)
        return {"event_id": event_id, "push_id": row["id"], "retried": True,
                "previous_status": row["status"], "status": status, "attempt_id": row["attempt_id"],
                "failure_reason": reason}

    def _send_claimed(self, row, recipient):
        provider_id = None
        try:
            send_with_receipt = getattr(self.gateway, "send_with_receipt", None)
            if send_with_receipt:
                status, reason, provider_id = send_with_receipt(recipient, row["message"], row["attempt_id"])
            else:
                status, reason = self.gateway.send(recipient, row["message"])
        except Exception as exc:
            status, reason = "unknown", f"发送结果不确定：{type(exc).__name__}"
        stored = self.store.push_finish(row["id"], status, reason,
                                       lease_token=row["lease_token"], provider_message_id=provider_id)
        return stored or "unknown", reason if stored else "发送所有权已失效；请核对原尝试"

    # ---------- 查询 ----------
    def signals(self, *, portfolio_id: str | None = None,
                run_id: str | None = None) -> list[dict]:
        return self.store.list_signals(portfolio_id=portfolio_id, run_id=run_id)

    def pushes(self, *, event_id: str | None = None) -> list[dict]:
        return self.store.list_pushes(event_id=event_id)

    def runs(self) -> list[dict]:
        return self.store.list_runs()

    def counts(self) -> dict:
        return self.store.counts()

    def risk_catalog(self) -> list[dict]:
        return RISK_CATALOG

    def switch_gateway(self, name: str, **kwargs) -> dict:
        self.gateway = build_gateway(name, **kwargs)
        return self.gateway.describe()

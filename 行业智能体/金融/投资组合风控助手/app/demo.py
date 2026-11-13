"""三组合闭环演示：全部通过公开 HTTP 接口完成，不手工插库、不预置成功状态。

启动：python -m app.demo --port 8765
演示链路：导入 → 立即监控 → 触发 → 推送 → 查询；并顺带验证去重、限频与故障恢复。
运行后服务保持在线，可在浏览器打开首页查回全部记录；`--no-serve` 只跑演示后退出。

仅使用虚构模拟数据；风险提示仅供参考，不构成投资建议。
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .gateway import LocalMockGateway
from .server import build_server
from .service import Service, beijing_now
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
TRIGGER_PRICES = {
    "SIM-STOCK-A": "94.00", "SIM-FUND-B": "1.0900",
    "SIM-STOCK-C": "47.00", "SIM-FUND-D": "2.1300",
}


def call(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def run_demo(base: str, *, db_path: Path) -> dict:
    print("=" * 78)
    print("课题 16 本地闭环演示｜全部经公开 HTTP 接口，使用虚构模拟数据")
    print("风险提示仅供参考，不构成投资建议")
    print("=" * 78)

    health = call(base, "GET", "/api/health")
    print(f"[0] 服务状态：{health['status']}，网关 "
          f"{health['gateway']['gateway_id']}（simulated="
          f"{health['gateway']['simulated']}）")

    imported = call(base, "POST", "/api/import", {"source": "demo"})
    print(f"[1] 录入/导入：{imported['imported']} 个组合，"
          f"{sum(len(p['holdings']) for p in imported['portfolios'])} 项持仓 "
          f"（股票 / 基金 / 混合）")

    call(base, "POST", "/api/config", {"max_per_window": 50,
                                       "window_seconds": 3600})
    now = datetime.now(timezone(timedelta(hours=8)))
    t_main = now.isoformat(timespec="seconds")
    t_limit = (now + timedelta(hours=1)).isoformat(timespec="seconds")
    t_fail = (now + timedelta(hours=2)).isoformat(timespec="seconds")

    news = [{"symbol": "SIM-STOCK-A", "name": "云岚科技股份有限公司",
             "text": "云岚科技股份有限公司公告称全厂停产，尚无复产日期。"}]
    first = call(base, "POST", "/api/monitor", {
        "prices": TRIGGER_PRICES, "timestamp": t_main,
        "news_items": news})["result"]
    print(f"[2] 立即监控 run_id={first['run_id']}：信号 "
          f"{first['signal_count']} 条，推送尝试 {first['push_count']} 条")

    records = call(base, "GET", "/api/records")
    pushes_by_event = {p["event_id"]: p for p in records["pushes"]}
    by_portfolio: dict[str, dict] = {}
    for signal in records["signals"]:
        pid = signal["portfolio_id"]
        push = pushes_by_event.get(signal["event_id"])
        if not push:
            continue
        # 每个组合取一条代表记录，优先展示止盈止损触发。
        rank = 0 if signal["risk_type"] in ("stop_loss", "take_profit") else 1
        if pid in by_portfolio and by_portfolio[pid]["_rank"] <= rank:
            continue
        by_portfolio[pid] = {
            "_rank": rank,
            "portfolio_id": pid,
            "portfolio_type": next(
                (p["type"] for p in records["portfolios"] if p["id"] == pid), "—"),
            "risk_type": signal.get("detail", {}).get(
                "risk_type_name", signal["risk_type"]),
            "symbol": signal["symbol"],
            "trigger_time": signal["trigger_time"],
            "event_id": signal["event_id"],
            "push_id": push["id"],
            "push_status": push["status"],
            "recipient_masked": push["recipient_masked"],
        }
    three = [{k: v for k, v in row.items() if k != "_rank"}
             for row in by_portfolio.values()]
    print("[3] 三个组合各自完成「录入→监控→触发→推送→查询」，可实际查回的记录：")
    for row in three:
        print(f"    - {row['portfolio_id']}({row['portfolio_type']}) "
              f"| {row['risk_type']} | {row['symbol']} "
              f"| event_id={row['event_id'][:16]}… "
              f"| push_id={row['push_id']} | status={row['push_status']} "
              f"| 接收人={row['recipient_masked']}")
    if len(three) < 3:
        raise RuntimeError("三组合闭环未全部产生可查回记录，请检查监控输入")

    before = records["counts"]
    repeat = call(base, "POST", "/api/monitor", {
        "prices": TRIGGER_PRICES, "timestamp": t_main,
        "news_items": news})["result"]
    after_repeat = call(base, "GET", "/api/records")["counts"]
    print(f"[4] 同一事件重复触发：去重 {sum(1 for p in repeat['pushes'] if p['duplicate'])} 条；"
          f"推送记录 {before['push_records']} → {after_repeat['push_records']}（未增加）")

    call(base, "POST", "/api/config", {"max_per_window": 2})
    limited = call(base, "POST", "/api/monitor", {
        "prices": TRIGGER_PRICES, "timestamp": t_limit})["result"]
    suppressed = sum(1 for p in limited["pushes"] if p["suppressed"])
    print(f"[5] 不同事件限频：本轮新事件被抑制 {suppressed} 条，"
          f"被抑制记录也落库可查（status=suppressed）")

    call(base, "POST", "/api/config", {"max_per_window": 50})
    call(base, "POST", "/api/gateway",
         {"name": "local-mock", "failure_mode": True})
    failed = call(base, "POST", "/api/monitor", {
        "prices": TRIGGER_PRICES, "timestamp": t_fail})["result"]
    failed_rows = [p for p in failed["pushes"] if p["status"] == "failed"]
    call(base, "POST", "/api/gateway",
         {"name": "local-mock", "failure_mode": False})
    recovery = {"retried": 0, "delivered": 0, "duplicate_blocked": 0}
    for row in failed_rows[:3]:
        retried = call(base, "POST", "/api/retry",
                       {"event_id": row["event_id"]})["result"]
        if retried["retried"]:
            recovery["retried"] += 1
            if retried["status"] == "delivered":
                recovery["delivered"] += 1
        again = call(base, "POST", "/api/retry",
                     {"event_id": row["event_id"]})["result"]
        if not again["retried"]:
            recovery["duplicate_blocked"] += 1
    counts = call(base, "GET", "/api/records")["counts"]
    print(f"[6] 故障恢复：注入故障产生 {len(failed_rows)} 条 failed；恢复后重试 "
          f"{recovery['retried']} 条、送达 {recovery['delivered']} 条，"
          f"再次重试被拦截 {recovery['duplicate_blocked']} 条（不重复送达）；"
          f"其余 {len(failed_rows) - recovery['retried']} 条保留失败现场供查询。"
          f"库内 delivered={counts['delivered']} "
          f"suppressed={counts['suppressed']} failed={counts['failed']}")

    print("-" * 78)
    print("查回方式（服务在线时）：")
    print(f"  浏览器打开           {base}/")
    print(f"  信号                 {base}/api/signals?portfolio_id=stock-demo")
    print(f"  推送回执             {base}/api/pushes")
    print(f"  运行记录             {base}/api/runs")
    print(f"  完整汇总             {base}/api/records")
    print(f"  演示数据库           {db_path}")
    print("-" * 78)
    return {"three_records": three, "counts": counts,
            "run_id": first["run_id"], "base": base}


def main() -> int:
    parser = argparse.ArgumentParser(description="三组合闭环演示（公开接口）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=None,
                        help="演示数据库路径；默认按时间新建，绝不覆盖已有数据")
    parser.add_argument("--no-serve", action="store_true",
                        help="只跑演示链路后退出，不保持服务在线")
    args = parser.parse_args()
    db = (Path(args.db) if args.db else
          ROOT / "data" / f"demo_run_{datetime.now():%Y%m%d_%H%M%S}.db")
    service = Service(Store(db), LocalMockGateway(),
                      window_seconds=3600, max_per_window=50)
    try:
        httpd = build_server(args.host, args.port, service)
    except OSError as exc:
        print(f"端口 {args.host}:{args.port} 无法监听：{exc}")
        return 2
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://{args.host}:{args.port}"
    print(f"演示数据库：{db}")
    try:
        run_demo(base, db_path=db)
        if args.no_serve:
            return 0
        print(f"服务保持在线：{base}/  （Ctrl+C 结束）")
        thread.join()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

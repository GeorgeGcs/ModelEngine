"""本地演示的公开 HTTP 接口与可点击页面（Python 标准库，无 Web 框架依赖）。

启动：python -m app.server --host 127.0.0.1 --port 8765
所有接口只接受虚构模拟数据；风险提示仅供参考，不构成投资建议。
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .gateway import LocalMockGateway
from .service import Service
from .store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Topic16LocalDemo/1.0"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("[demo] " + (fmt % args) + "\n")

        # ---------- 基础响应 ----------
        def _json(self, payload: dict, code: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _html(self, raw: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                document = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(document, dict):
                raise ValueError("请求体须为 JSON 对象")
            return document

        def _error(self, exc: Exception, code: int = 400) -> None:
            if code >= 500:
                traceback.print_exc(file=sys.stderr)
            self._json({"ok": False, "error": str(exc), "error_type":
                        type(exc).__name__}, code)

        # ---------- GET ----------
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    self._html((WEB_DIR / "index.html").read_bytes())
                    return
                if path == "/api/health":
                    self._json({"ok": True, "status": "running",
                                "gateway": service.gateway.describe(),
                                "counts": service.counts(),
                                "config": {
                                    "window_seconds": service.window_seconds,
                                    "max_per_window": service.max_per_window,
                                },
                                "disclaimer":
                                    "风险提示仅供参考，不构成投资建议"})
                    return
                if path == "/api/portfolios":
                    self._json({"ok": True, "portfolios": service.portfolios()})
                    return
                if path == "/api/signals":
                    self._json({"ok": True, "signals": service.signals(
                        portfolio_id=first(query, "portfolio_id"),
                        run_id=first(query, "run_id"))})
                    return
                if path == "/api/business-run":
                    self._json({"ok":True,"record":service.store.business_record(first(query,'run_id'))})
                    return
                if path == "/api/knowledge":
                    self._json({"ok": True, "records": service.store.knowledge_records(first(query,"run_id"))})
                    return
                if path == "/api/pushes":
                    self._json({"ok": True, "pushes": service.pushes(
                        event_id=first(query, "event_id"))})
                    return
                if path == "/api/push-attempts":
                    self._json({"ok": True, "attempts": service.store.list_push_attempts()})
                    return
                if path == "/api/runs":
                    self._json({"ok": True, "runs": service.runs()})
                    return
                if path == "/api/risk-catalog":
                    self._json({"ok": True, "risk_catalog": service.risk_catalog()})
                    return
                if path == "/api/records":
                    self._json({
                        "ok": True,
                        "portfolios": service.portfolios(),
                        "signals": service.signals(
                            portfolio_id=first(query, "portfolio_id")),
                        "pushes": service.pushes(),
                        "runs": service.runs(),
                        "counts": service.counts(),
                        "gateway": service.gateway.describe(),
                        "disclaimer": "风险提示仅供参考，不构成投资建议",
                    })
                    return
                self._error(ValueError(f"未知路径：{path}"), 404)
            except Exception as exc:  # noqa: BLE001
                self._error(exc, 500)

        # ---------- 写接口 ----------
        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            path = urlparse(self.path).path.rstrip("/")
            try:
                body = self._body()
                if method == "POST" and path == "/api/portfolios":
                    self._json({"ok": True,
                                "portfolio": service.upsert_portfolio(body)})
                    return
                if method == "POST" and path == "/api/import":
                    if body.get("source") == "demo":
                        rows = service.import_demo_file()
                    else:
                        rows = service.import_portfolios(body)
                    self._json({"ok": True, "portfolios": rows,
                                "imported": len(rows)})
                    return
                if method == "PUT" and path == "/api/holdings":
                    self._json({"ok": True, "portfolio": service.set_holding(
                        body["portfolio_id"], body["holding"])})
                    return
                if method == "DELETE" and path == "/api/holdings":
                    service.store.remove_holding(body["portfolio_id"],
                                                 body["symbol"])
                    self._json({"ok": True,
                                "portfolio": service.store.get_portfolio(
                                    body["portfolio_id"])})
                    return
                if method == 'POST' and path == '/api/integrated-monitor':
                    from integration.runner import execute
                    root=os.environ.get('TOPIC16_INTEGRATION_EVIDENCE_DIR')
                    if not root:raise ValueError('TOPIC16_INTEGRATION_EVIDENCE_DIR required')
                    self._json({'ok':True,'result':execute(service.store,body['case'],body['run_id'],
                        body['mode'],Path(root)/uuid.uuid4().hex)})
                    return
                if method == 'POST' and path == '/api/integrated-monitor/stream':
                    from integration.rehearsal import CASES
                    from integration.runner import execute
                    if (self.headers.get_content_type() != 'application/json'
                            or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                        self._json({'ok':False,'error':'仅接受同源 JSON 请求'},400)
                        return
                    root = os.environ.get('TOPIC16_INTEGRATION_EVIDENCE_DIR')
                    if not root or not os.environ.get('TOPIC16_DISPATCH_KEY_FILE'):
                        self._json({'ok':False,'error':'Nexent 工作流未配置'},503)
                        return
                    matches = [c for c in json.loads(CASES.read_text(encoding='utf-8'))['cases']
                               if c['case_id'] == body.get('case_id')]
                    if len(matches) != 1:
                        raise ValueError('未知集成用例')
                    self.send_response(200)
                    self.send_header('Content-Type','application/x-ndjson; charset=utf-8')
                    self.send_header('Cache-Control','no-store')
                    self.send_header('Connection','close')
                    self.end_headers()
                    self.close_connection = True
                    connected = True

                    def emit(event):
                        nonlocal connected
                        if connected:
                            try:
                                self.wfile.write((json.dumps(event,ensure_ascii=False)+'\n').encode())
                                self.wfile.flush()
                            except OSError:
                                connected = False

                    run_id = 'browser-' + uuid.uuid4().hex
                    try:
                        result = execute(service.store,matches[0],run_id,'nexent-integrated',
                                         Path(root)/run_id,on_verified=emit)
                        emit({'type':'finished','run_id':run_id,'evidence_complete':result['evidence_complete'],
                              'signal_count':result['signal_count'],'failures':result['failures']})
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                        emit({'type':'failed','run_id':run_id})
                    return
                if method == "POST" and path == "/api/monitor":
                    self._json({"ok": True, "result": service.monitor(
                        prices=body["prices"], timestamp=body.get("timestamp"),
                        market=body.get("market"),
                        news_items=body.get("news_items"),
                        portfolio_ids=body.get("portfolio_ids"),
                        run_id=body.get("run_id"), knowledge_queries=body.get("knowledge_queries"))})
                    return
                if method == "POST" and path == "/api/retry":
                    self._json({"ok": True,
                                "result": service.retry_push(body["event_id"])})
                    return
                if method == "POST" and path == "/api/config":
                    if "max_per_window" in body:
                        value = int(body["max_per_window"])
                        if value < 1:
                            raise ValueError("max_per_window 须为正整数")
                        service.max_per_window = value
                    if "window_seconds" in body:
                        value = int(body["window_seconds"])
                        if value < 1:
                            raise ValueError("window_seconds 须为正整数")
                        service.window_seconds = value
                    self._json({"ok": True, "config": {
                        "window_seconds": service.window_seconds,
                        "max_per_window": service.max_per_window}})
                    return
                if method == "POST" and path == "/api/gateway":
                    name = body.get("name", "local-mock")
                    if name == "local-mock":
                        gateway = LocalMockGateway(
                            failure_mode=bool(body.get("failure_mode", False)))
                        service.gateway = gateway
                        describe = gateway.describe()
                    else:
                        describe = service.switch_gateway(name)
                    self._json({"ok": True, "gateway": describe})
                    return
                self._error(ValueError(f"未知路径或方法：{method} {path}"), 404)
            except (KeyError, ValueError, TypeError) as exc:
                self._error(exc, 400)
            except Exception as exc:  # noqa: BLE001
                self._error(exc, 500)

    return Handler


def first(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def build_server(host: str, port: int, service: Service) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="课题 16 本地可操作演示服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=None, help="SQLite 路径，默认 data/local_app.db")
    parser.add_argument("--gateway", default="local-mock",
                        choices=("local-mock", "webhook"))
    parser.add_argument("--max-per-window", type=int, default=3)
    args = parser.parse_args()
    service = Service(Store(args.db), max_per_window=args.max_per_window)
    if args.gateway != "local-mock":
        service.switch_gateway(args.gateway)
    try:
        httpd = build_server(args.host, args.port, service)
    except OSError as exc:
        print(f"端口 {args.host}:{args.port} 无法监听：{exc}", file=sys.stderr)
        return 2
    print(f"本地演示已启动：http://{args.host}:{args.port}/")
    print(f"数据库：{service.store.path}")
    print(f"网关：{service.gateway.describe()}")
    print("风险提示仅供参考，不构成投资建议。Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

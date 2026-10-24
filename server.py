"""零依赖 HTTP 服务（1 号负责）：把 Agent 暴露成网页 + API。

启动：python server.py [端口]
浏览器打开 http://127.0.0.1:8848
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.agent import Session

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")

SESSIONS: dict[str, Session] = {}
LOCK = threading.Lock()

TOOL_CATALOG = [
    {"name": "account.balance", "level": "query", "desc": "查询借记卡余额与信用卡可用额度"},
    {"name": "bill.summary", "level": "query", "desc": "按月汇总消费，含分类占比与环比"},
    {"name": "bill.detail", "level": "query", "desc": "按分类看金额最高的若干笔"},
    {"name": "bill.anomalies", "level": "query", "desc": "异常交易识别，给出判定依据"},
    {"name": "bill.annual", "level": "query", "desc": "年度消费汇总"},
    {"name": "subscription.list", "level": "query", "desc": "订阅清单、月固定支出、下次扣费日"},
    {"name": "subscription.cancel", "level": "change", "desc": "取消订阅"},
    {"name": "transfer.create", "level": "change/high", "desc": "转账、定时转账"},
    {"name": "wealth.list", "level": "query", "desc": "理财产品、年化、期限、风险"},
    {"name": "wealth.purchase", "level": "change", "desc": "申购理财"},
    {"name": "wealth.redeem", "level": "change", "desc": "赎回理财"},
    {"name": "card.list", "level": "query", "desc": "卡片与额度"},
    {"name": "card.lock", "level": "change", "desc": "锁卡/解锁"},
    {"name": "card.report_loss", "level": "high", "desc": "挂失"},
    {"name": "card.limit_adjust", "level": "high", "desc": "额度调整"},
]


def get_session(sid: str | None) -> tuple[str, Session]:
    sid = sid or ("s_" + uuid.uuid4().hex[:8])
    with LOCK:
        if sid not in SESSIONS:
            SESSIONS[sid] = Session(sid)
        return sid, SESSIONS[sid]


class Handler(BaseHTTPRequestHandler):
    server_version = "AIBankAgent/1.0"

    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # ---------- 工具方法 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str):
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._file(os.path.join(WEB, "index.html"))
        elif u.path == "/api/health":
            self._json({"ok": True, "sessions": len(SESSIONS)})
        elif u.path == "/api/tools":
            self._json({"tools": TOOL_CATALOG})
        elif u.path == "/api/bootstrap":
            q = parse_qs(u.query)
            sid, sess = get_session((q.get("session_id") or [None])[0])
            db = sess.db
            debit = next((a for a in db["accounts"] if a["type"] == "借记卡"), {})
            self._json({
                "session_id": sid,
                "user": db["user"],
                "contacts": db["contacts"],
                "debit": {"no": debit.get("no", ""), "balance": debit.get("balance", 0)},
                "subscriptions": [{"id": x["id"], "name": x["name"]} for x in db["subscriptions"]],
                "wealth": [{"id": x["id"], "name": x["name"]} for x in db["wealth"]],
            })
        elif u.path == "/api/timeline":
            q = parse_qs(u.query)
            sid, sess = get_session((q.get("session_id") or [None])[0])
            self._json({"session_id": sid, "audit": sess.audit, "history": sess.history})
        elif u.path.startswith("/"):
            self._file(os.path.join(WEB, u.path.lstrip("/")))
        else:
            self._json({"error": "bad path"}, 400)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        if u.path == "/api/chat":
            sid, sess = get_session(payload.get("session_id"))
            text = (payload.get("text") or "").strip()
            if not text:
                self._json({"error": "empty text"}, 400)
                return
            with LOCK:
                out = sess.handle(text)
            out["session_id"] = sid
            self._json(out)
        elif u.path == "/api/reset":
            sid, _ = get_session(payload.get("session_id"))
            with LOCK:
                SESSIONS[sid] = Session(sid)
            self._json({"ok": True, "session_id": sid})
        else:
            self._json({"error": "bad path"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 56)
    print("  AI Banking Agent 已启动")
    print(f"  打开浏览器访问： http://127.0.0.1:{port}")
    print("  Ctrl+C 停止")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

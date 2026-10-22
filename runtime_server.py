"""Serve the dashboard and refresh its single WeCom source on the server."""
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from scripts.refresh_dashboard_from_wecom import SHANGHAI, build_payload, fetch_records

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DASHBOARD_DATA_DIR", "/data"))
PATTERN = r"(?<=const REQUIREMENT_DATA = )(.*?)(?=;\s*// REQUIREMENT_DATA_END)"


def now_text():
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def next_hour(now=None):
    return ((now or datetime.now(SHANGHAI)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))


class Dashboard:
    def __init__(self, database=None):
        self.db = str(database or os.environ.get("TECHHUB_SQLITE_PATH", DATA / "app.sqlite3"))
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.config = json.loads((ROOT / "config/wecom_yiduoyun_config.json").read_text())
        self.template = (ROOT / "index.html").read_text()
        bundled = json.loads(re.search(PATTERN, self.template, re.S)[1])
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, state TEXT NOT NULL)")
            row = db.execute("SELECT payload,state FROM snapshot WHERE id=1").fetchone()
        self.payload = json.loads(row[0]) if row else bundled
        self.state = json.loads(row[1]) if row else {"status": "idle", "message": "等待刷新"}
        if self.state["status"] == "running":
            self.state.update(status="error", message="上次刷新被中断，请重新刷新", completedAt=now_text())
        self.persist()

    def persist(self):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT OR REPLACE INTO snapshot VALUES(1,?,?)", (json.dumps(self.payload, ensure_ascii=False), json.dumps(self.state, ensure_ascii=False)))

    def status(self):
        with self.lock:
            return {"ok": True, **self.state, "updatedAt": self.payload["meta"]["snapshotAt"],
                    "sourceRecordCount": self.payload["meta"].get("sourceRecordCount"),
                    "requirementCount": sum(t["total"] for t in self.payload["topics"].values()),
                    "schedule": "每小时整点自动刷新（北京时间）", "nextRefreshAt": next_hour().strftime("%Y-%m-%d %H:%M:%S")}

    def start(self, trigger="manual"):
        with self.lock:
            if self.state["status"] == "running":
                return False
            # Coalesce repeated clicks across browsers immediately after completion.
            if trigger == "manual" and self.state.get("status") == "success":
                completed = datetime.strptime(self.state["completedAt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
                if (datetime.now(SHANGHAI) - completed).total_seconds() < 30:
                    return False
            self.state = {"status": "running", "runId": uuid.uuid4().hex, "trigger": trigger,
                          "startedAt": now_text(), "message": "正在读取业务需求收集表…"}
            self.persist()
            self.worker = threading.Thread(target=self.refresh, daemon=True)
            self.worker.start()
            return True

    def refresh(self):
        try:
            records = fetch_records(self.config)
            if not records:
                raise ValueError("业务需求收集表返回 0 条记录")
            # Use the latest snapshot for existing user-name and row mappings.
            legacy_page = DATA / "refresh-input.html"
            legacy_page.parent.mkdir(parents=True, exist_ok=True)
            legacy_page.write_text(self.html())
            payload = build_payload(records, self.config, legacy_page)
            legacy_page.unlink(missing_ok=True)
            if not sum(t["total"] for t in payload["topics"].values()):
                raise ValueError("未读取到有效需求，请检查来源表字段")
            with self.lock:
                old_payload, old_state = self.payload, self.state.copy()
                self.payload = payload
                self.state.update(status="success", completedAt=now_text(), message="数据刷新完成")
                try:
                    self.persist()
                except Exception:
                    self.payload, self.state = old_payload, old_state
                    raise
        except Exception as error:
            # Keep operational details and CLI output out of the public API.
            print(f"Refresh failed: {type(error).__name__}", flush=True)
            with self.lock:
                self.state.update(status="error", completedAt=now_text(), message="数据刷新失败，已保留上次成功数据。请稍后重试或联系管理员检查数据源授权。")
                self.persist()

    def html(self):
        with self.lock:
            serialized = json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
            return re.sub(PATTERN, lambda _: serialized, self.template, count=1, flags=re.S)

    def schedule(self):
        def tick():
            try:
                self.start("hourly")
            finally:
                self.schedule()
        timer = threading.Timer((next_hour() - datetime.now(SHANGHAI)).total_seconds(), tick)
        timer.daemon = True
        timer.start()


def make_handler(dashboard):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, content, mime="application/json; charset=utf-8"):
            raw = (json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/health":
                self.send(200, {"ok": True, "service": "一朵云项目进度看板"})
            elif path == "/api/refresh/status":
                self.send(200, dashboard.status())
            elif path == "/api/dashboard":
                with dashboard.lock:
                    self.send(200, {"ok": True, "data": dashboard.payload})
            elif path in ("/", "/index.html"):
                self.send(200, dashboard.html(), "text/html; charset=utf-8")
            elif path == "/iteration-plan.html":
                self.send(200, (ROOT / "iteration-plan.html").read_text(), "text/html; charset=utf-8")
            elif path == "/data-refresh.js":
                self.send(200, (ROOT / "data-refresh.js").read_text(), "text/javascript; charset=utf-8")
            else:
                # Never expose source files, configuration, credentials or SQLite.
                self.send(404, {"ok": False, "message": "页面不存在"})

        def do_POST(self):
            if urlsplit(self.path).path != "/api/refresh":
                self.send(404, {"ok": False})
                return
            origin = self.headers.get("Origin")
            public_origin = os.environ.get("DASHBOARD_PUBLIC_ORIGIN", "https://techhub.zhaogangrentest.com")
            if origin and origin != public_origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.send(403, {"ok": False, "message": "仅支持从看板页面发起刷新"})
                return
            started = dashboard.start()
            self.send(202 if started else 200, dashboard.status())

    return Handler


if __name__ == "__main__":
    dashboard = Dashboard()
    dashboard.schedule()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), make_handler(dashboard)).serve_forever()

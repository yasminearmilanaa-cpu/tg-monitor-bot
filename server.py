import os
import json
import time
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Konfigurasi Environment ---
PORT = int(os.getenv("PORT", "8080"))
BOT_TOKEN = os.getenv("9267118980:AAHAynWeSABP2EiEijQZsCwKsffvgqr2Dzc", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SYNC_TOKEN = os.getenv("SYNC_TOKEN", "").strip()
MONITOR_MODE = os.getenv("MONITOR_MODE", "monitor").strip().lower()
NOTIFY_MODE = os.getenv("NOTIFY_MODE", "both").strip().lower()
SUMMARY_INTERVAL = int(os.getenv("SUMMARY_INTERVAL_SEC", "900"))
STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/monitor_state.json"))

state_lock = threading.Lock()
state = {
    "last_update": None,
    "vps_host": "unknown",
    "counts": {},
    "recent_events": [],
}


def load_state() -> None:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state.update(data)
        except Exception:
            pass


def save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] Gagal simpan state: {exc}")


def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diset.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as exc:
        print(f"[ERROR] Gagal kirim Telegram: {exc}")


def format_summary() -> str:
    with state_lock:
        last_upd = state.get("last_update") or "Belum ada data dari VPS"
        host = state.get("vps_host") or "unknown"
        counts = state.get("counts") or {}
        recent = state.get("recent_events") or []

    lines = [
        "<b>📊 Rekap Evaluasi Monitoring (Railway)</b>",
        f"• Mode: <code>{MONITOR_MODE}</code> | Notif: <code>{NOTIFY_MODE}</code>",
        f"• Sumber VPS: <code>{host}</code>",
        f"• Update Terakhir: <code>{last_upd}</code>",
        "",
        "<b>Statistik File:</b>",
    ]
    if counts:
        for fname, cnt in counts.items():
            lines.append(f"• <code>{fname}</code>: <b>{cnt}</b> baris")
    else:
        lines.append("• <i>Menunggu sinkronisasi pertama dari VPS...</i>")

    if recent:
        lines.append("\n<b>Aktivitas Terakhir:</b>")
        for ev in recent[-5:]:
            lines.append(f"• [{ev.get('time')}] <code>{ev.get('file')}</code> (+{ev.get('new_count')})")

    return "\n".join(lines)


class IngestHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "mode": MONITOR_MODE, "notify": NOTIFY_MODE})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/ingest":
            self._send_json(404, {"error": "not_found"})
            return

        token = self.headers.get("X-Sync-Token", "")
        if SYNC_TOKEN and token != SYNC_TOKEN:
            self._send_json(401, {"error": "unauthorized"})
            return

        content_len = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_len).decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        fname = str(payload.get("file", "unknown"))
        new_count = int(payload.get("new_count", 0))
        sample = str(payload.get("sample", "")).strip()
        counts = payload.get("counts", {})
        vps_host = str(payload.get("host", "vps-primary"))

        with state_lock:
            state["last_update"] = now_str
            state["vps_host"] = vps_host
            if isinstance(counts, dict):
                state["counts"] = counts
            state["recent_events"].append({
                "time": now_str,
                "file": fname,
                "new_count": new_count,
            })
            state["recent_events"] = state["recent_events"][-30:]
            save_state()

        # Notifikasi real-time jika NOTIFY_MODE = instant atau both
        if NOTIFY_MODE in ("instant", "both") and new_count > 0:
            msg = (
                f"<b>🔔 Update VPS: <code>{fname}</code></b>\n"
                f"• Host: <code>{vps_host}</code>\n"
                f"• Tambahan: <b>+{new_count}</b> baris\n"
            )
            if sample:
                safe_sample = (
                    sample[-900:]
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                msg += f"<pre>{safe_sample}</pre>"
            send_telegram(msg)

        self._send_json(200, {"ok": True})

    def log_message(self, format: str, *args) -> None:
        return


def summary_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        stop_event.wait(SUMMARY_INTERVAL)
        if stop_event.is_set():
            break
        if NOTIFY_MODE in ("summary", "both"):
            send_telegram(format_summary())


def telegram_command_loop(stop_event: threading.Event) -> None:
    if not BOT_TOKEN:
        return
    offset = None
    while not stop_event.is_set():
        try:
            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?{urllib.parse.urlencode(params)}"
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                offset = item["update_id"] + 1
                msg = item.get("message", {})
                text = (msg.get("text") or "").strip().lower()
                chat = str(msg.get("chat", {}).get("id", ""))
                if CHAT_ID and chat != CHAT_ID:
                    continue
                if text in ("/start", "/status", "/summary", "/eval"):
                    send_telegram(format_summary())
        except Exception:
            time.sleep(3)


def main() -> None:
    load_state()
    stop_event = threading.Event()

    threading.Thread(target=summary_scheduler, args=(stop_event,), daemon=True).start()
    threading.Thread(target=telegram_command_loop, args=(stop_event,), daemon=True).start()

    send_telegram(
        f"<b>✅ Railway Monitor Server Aktif</b>\n"
        f"Mode: <code>{MONITOR_MODE}</code> | Notifikasi: <code>{NOTIFY_MODE}</code>\n"
        f"Gunakan <code>/summary</code> atau <code>/status</code> untuk evaluasi kapan saja."
    )

    server = ThreadingHTTPServer(("0.0.0.0", PORT), IngestHandler)
    print(f"[INFO] Listening on 0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()

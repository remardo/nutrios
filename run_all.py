import os
import sys
import time
import socket
import signal
import atexit
import subprocess
from threading import Event

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(__file__)
load_dotenv(os.path.join(ROOT, ".env"))

API_PORT = int(os.getenv("API_PORT", "8000"))
DASH_PORT = int(os.getenv("DASH_PORT", "8501"))
API_BASE = os.getenv("ADMIN_API_BASE", f"http://localhost:{API_PORT}")
API_KEY = os.getenv("ADMIN_API_KEY", "supersecret")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

PY = sys.executable or "python"

procs: list[subprocess.Popen] = []
stop_event = Event()


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def wait_http(url: str, headers: dict | None = None, timeout_s: float = 20.0, interval_s: float = 0.5) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline and not stop_event.is_set():
        try:
            r = requests.get(url, headers=headers or {}, timeout=5)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def tg_getme_ok(token: str | None) -> bool:
    if not token:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        return r.ok and (r.json().get("ok") is True)
    except Exception:
        return False


def start_api() -> str:
    if is_port_in_use(API_PORT):
        return f"API already running on :{API_PORT}"
    p = subprocess.Popen([PY, os.path.join(ROOT, "run_api.py")], cwd=ROOT)
    procs.append(p)
    ok = wait_http(f"{API_BASE}/clients", headers={"x-api-key": API_KEY}, timeout_s=25)
    return "API started" if ok else "API start timed out"


def start_dashboard() -> str:
    if is_port_in_use(DASH_PORT):
        return f"Dashboard already running on :{DASH_PORT}"
    p = subprocess.Popen([PY, "-m", "streamlit", "run", os.path.join(ROOT, "dashboard", "app.py"),
                          "--server.headless", "true", "--server.port", str(DASH_PORT)], cwd=ROOT)
    procs.append(p)
    ok = wait_http(f"http://localhost:{DASH_PORT}/", timeout_s=30)
    return "Dashboard started" if ok else "Dashboard start timed out"


def start_bot() -> str:
    # We can't probe a local port; validate token and then just spawn
    token_ok = tg_getme_ok(TG_TOKEN)
    p = subprocess.Popen([PY, os.path.join(ROOT, "bot", "main.py")], cwd=ROOT)
    procs.append(p)
    return "Bot started (token ok)" if token_ok else "Bot started (token check failed or missing)"


def shutdown():
    # Terminate child processes gracefully
    for p in procs:
        if p.poll() is None:
            try:
                if os.name == "nt":
                    p.send_signal(signal.CTRL_BREAK_EVENT) if hasattr(signal, "CTRL_BREAK_EVENT") else p.terminate()
                else:
                    p.terminate()
            except Exception:
                pass
    # Give them a moment, then kill if alive
    time.sleep(1.0)
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


atexit.register(shutdown)


def health_only() -> int:
    api_ok = wait_http(f"{API_BASE}/clients", headers={"x-api-key": API_KEY}, timeout_s=3)
    dash_ok = wait_http(f"http://localhost:{DASH_PORT}/", timeout_s=3)
    bot_ok = tg_getme_ok(TG_TOKEN)
    print(f"HEALTH: API={'OK' if api_ok else 'FAIL'} | Dashboard={'OK' if dash_ok else 'FAIL'} | BotToken={'OK' if bot_ok else 'FAIL'}")
    return 0 if (api_ok and dash_ok and bot_ok) else 1


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run Nutrios services")
    parser.add_argument("--no-api", action="store_true")
    parser.add_argument("--no-dash", action="store_true")
    parser.add_argument("--no-bot", action="store_true")
    parser.add_argument("--health", action="store_true", help="Only run health checks and exit")
    args = parser.parse_args()

    if args.health:
        sys.exit(health_only())

    msgs = []
    try:
        if not args.no_api:
            msgs.append(start_api())
        if not args.no_dash:
            msgs.append(start_dashboard())
        if not args.no_bot:
            msgs.append(start_bot())
        print(" | ".join(msgs))
        print(f"Open dashboard: http://localhost:{DASH_PORT}")
        print(f"API base: {API_BASE}")
        # Keep parent alive until Ctrl+C
        while True:
            # If any critical process exits early, break
            alive = [p.poll() is None for p in procs]
            if not any(alive):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        shutdown()


if __name__ == "__main__":
    main()

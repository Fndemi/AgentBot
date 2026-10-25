"""Starts LeadBot and the website together: `python start.py`.

The bot (`python main.py auto`) runs as a child process, restarted 30 seconds after it
ever exits. The website runs here; when it stops, the bot is stopped too.
"""
import os
import subprocess
import sys
import threading
from datetime import datetime

import uvicorn

RESTART_SECONDS = 30
BOT = [sys.executable, "main.py", "auto"]
HERE = os.path.dirname(os.path.abspath(__file__))

stopping = threading.Event()
lock = threading.Lock()
bot = None


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} start: {msg}", flush=True)


def start_bot():
    """Start the bot unless we are stopping. Its output goes to ours, i.e. the normal logs."""
    global bot
    with lock:
        if stopping.is_set():
            return None
        bot = subprocess.Popen(BOT, cwd=HERE)
        log(f"bot started (pid {bot.pid})")
        return bot


def watch_bot():
    proc = start_bot()
    while proc:
        code = proc.wait()
        if stopping.is_set():
            return
        log(f"bot exited with code {code}; restarting in {RESTART_SECONDS} seconds")
        if stopping.wait(RESTART_SECONDS):
            return
        proc = start_bot()
        if proc:
            log("bot restarted")


def stop_bot():
    with lock:
        stopping.set()
        proc = bot
    if proc and proc.poll() is None:
        log("stopping bot")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log("bot stopped")


def main():
    threading.Thread(target=watch_bot, daemon=True).start()
    try:
        uvicorn.run("web:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
    finally:
        stop_bot()


if __name__ == "__main__":
    main()

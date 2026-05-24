"""
line-sync: forward LINE messages to Telegram.

Usage:
    uv run main.py

Requires:
    1. Fill in config.toml (bot_token, supergroup_chat_id).
    2. Screen Recording permission granted to Terminal in System Settings.
    3. LINE must be running (background is fine; not minimised to Dock).
"""

import logging
import sys
import threading
import time
import tomllib

import accessibility_reader
import fs_watcher
import state
from telegram_sender import TelegramSender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("line-sync")


def load_config() -> dict:
    try:
        with open("config.toml", "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        sys.exit("config.toml not found.")


def forward_messages(telegram: TelegramSender, chat_name: str, messages: list[dict]):
    for msg in messages:
        sender = msg.get("sender", "")
        text = msg.get("text", "").strip()
        if not text:
            continue
        if state.is_seen(chat_name, sender, text):
            continue

        topic_id = state.get_topic(chat_name)
        if topic_id is None:
            log.info("Creating Telegram topic for '%s'", chat_name)
            topic_id = telegram.create_topic(chat_name)
            state.set_topic(chat_name, topic_id)

        label = f"[{sender}]: {text}" if sender else text
        log.info("-> Telegram [%s] %s", chat_name, label[:80])
        telegram.send_message(topic_id, label)


def make_on_db_change(telegram: TelegramSender):
    # Debounce: ignore rapid successive writes (WAL checkpoints)
    _last_fire = [0.0]
    _lock = threading.Lock()

    def handler():
        with _lock:
            now = time.time()
            if now - _last_fire[0] < 4.0:
                return
            _last_fire[0] = now

        log.info("DB change detected — capturing LINE")
        chat_name, messages = accessibility_reader.get_messages_current_chat()
        if not chat_name:
            log.warning("Could not determine current chat name")
            return
        if not messages:
            log.debug("No messages parsed from OCR")
            return
        forward_messages(telegram, chat_name, messages)

    return handler


def main():
    cfg = load_config()
    if cfg.get("bot_token", "").startswith("YOUR_"):
        sys.exit("Please fill in bot_token in config.toml")

    telegram = TelegramSender(cfg["bot_token"], cfg["supergroup_chat_id"])
    on_change = make_on_db_change(telegram)

    # FSEvents watches WAL file; fires for both sent and received messages
    fs_watcher.start(on_change, [0.0])

    log.info("line-sync running — waiting for LINE DB changes...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down.")
        telegram.close()


if __name__ == "__main__":
    main()

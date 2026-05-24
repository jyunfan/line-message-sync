import hashlib
import json
import os
import tempfile

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"topics": {}, "seen": {}}


def _save(data: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def get_topic(chat_name: str) -> int | None:
    return _load()["topics"].get(chat_name)


def set_topic(chat_name: str, topic_id: int):
    data = _load()
    data["topics"][chat_name] = topic_id
    _save(data)


def is_seen(chat_name: str, sender: str, text: str) -> bool:
    # Hash on text only (sender parsing is unreliable across OCR runs).
    # Normalize before hashing so minor OCR variations don't break dedup.
    from accessibility_reader import normalize_for_dedup
    key = hashlib.sha1(f"{chat_name}\x00{normalize_for_dedup(text)}".encode()).hexdigest()
    data = _load()
    seen_set = set(data["seen"].get(chat_name, []))
    if key in seen_set:
        return True
    # Keep last 500 hashes per chat to bound file size
    seen_list = data["seen"].get(chat_name, [])
    seen_list.append(key)
    if len(seen_list) > 500:
        seen_list = seen_list[-500:]
    data["seen"][chat_name] = seen_list
    _save(data)
    return False

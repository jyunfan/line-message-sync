import httpx
import logging

log = logging.getLogger(__name__)

BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramSender:
    def __init__(self, bot_token: str, supergroup_chat_id: int):
        self._token = bot_token
        self._chat_id = supergroup_chat_id
        self._client = httpx.Client(timeout=15)

    def _call(self, method: str, **params):
        url = BASE.format(token=self._token, method=method)
        resp = self._client.post(url, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data}")
        return data["result"]

    def create_topic(self, name: str) -> int:
        result = self._call("createForumTopic", chat_id=self._chat_id, name=name[:128])
        return result["message_thread_id"]

    def send_message(self, topic_id: int, text: str):
        self._call(
            "sendMessage",
            chat_id=self._chat_id,
            message_thread_id=topic_id,
            text=text[:4096],
        )

    def close(self):
        self._client.close()

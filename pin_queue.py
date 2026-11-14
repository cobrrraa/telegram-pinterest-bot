"""Очередь фото на публикацию. Хранится в JSON-файле, чтобы переживать перезапуск бота.

Пачка (batch) — фото, отправленные одним выбором доски; у каждой пачки есть сообщение
в Telegram, которое бот обновляет по мере публикации.
"""

import json
import secrets
from pathlib import Path


class PinQueue:
    def __init__(self, path: Path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        self.items: list[dict] = data.get("items", [])  # {"batch", "file_id", "n"} в порядке публикации
        self.batches: dict[str, dict] = data.get("batches", {})
        self.next_attempt_at: float = data.get("next_attempt_at", 0)  # unix time
        self.backoff: float = data.get("backoff", 0)  # текущая пауза после антиспама, секунды; 0 — блокировки нет
        self.paused: bool = data.get("paused", False)  # Make перестал запускать сценарий — ждём /resume

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "items": self.items,
            "batches": self.batches,
            "next_attempt_at": self.next_attempt_at,
            "backoff": self.backoff,
            "paused": self.paused,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add_batch(self, chat_id: int, message_id: int, board_id: str, board_name: str, file_ids: list[str]) -> str:
        batch_id = secrets.token_hex(4)
        self.batches[batch_id] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "board_id": board_id,
            "board_name": board_name,
            "total": len(file_ids),
            "links": [],
            "errors": [],
        }
        self.items += [{"batch": batch_id, "file_id": f, "n": n} for n, f in enumerate(file_ids, start=1)]
        self.save()
        return batch_id

    def remaining(self, batch_id: str) -> int:
        return sum(1 for item in self.items if item["batch"] == batch_id)

    def pop_first(self) -> None:
        self.items.pop(0)

    def clear(self) -> list[str]:
        """Убирает все фото из очереди, возвращает id затронутых пачек."""
        batch_ids = list(dict.fromkeys(item["batch"] for item in self.items))
        self.items = []
        self.save()
        return batch_ids

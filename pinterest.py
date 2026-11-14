"""Клиент Pinterest API v5, который ходит в Pinterest через webhook сценария Make.

Свой API-доступ Pinterest не выдал, поэтому запросы проксирует Make: сценарий
принимает {secret, method, path, body}, вызывает модуль Pinterest "Make an API Call"
и возвращает статус и JSON-ответ Pinterest как есть.
"""

import base64
import json
from urllib.parse import urlencode

import httpx


class PinterestError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        # True — ошибка временная (антиспам, лимиты, сбой Make): остальные фото стоит отложить и повторить позже.
        self.retryable = retryable


class MakeStoppedError(PinterestError):
    """Make не запустил сценарий: кончились кредиты, сценарий выключен или удалён.

    Повторять раз в несколько часов бессмысленно — нужно, чтобы человек разобрался в Make.
    """

    def __init__(self, message: str, queued_in_make: bool):
        super().__init__(message)
        # True — Make ответил "Accepted" и, возможно, сохранил запрос у себя: фото может выложиться позже само.
        self.queued_in_make = queued_in_make


# Ответы самого Make (не Pinterest), когда сценарий не запустился.
MAKE_STOPPED = ["no scenario listening", "queue is full"]


# Частые ответы Pinterest/Make -> (понятное объяснение, временная ли ошибка). Ищем подстроку без учёта регистра.
KNOWN_ERRORS = [
    ("image is too small", "картинка слишком маленькая, Pinterest такие не принимает", False),
    ("missing: [", "у подключения Pinterest в Make не хватает прав", False),
    ("hit a block", "антиспам Pinterest: слишком много пинов подряд, нужно подождать", True),
    ("[429]", "Pinterest просит подождать: слишком много запросов", True),
    ("rate limit", "Pinterest просит подождать: слишком много запросов", True),
    # Без обработчика ошибок в сценарии Make причину не сообщает — считаем ошибку временной.
    ("scenario failed to complete", "сценарий Make упал, причина в истории запусков Make", True),
]


def make_error(text: str) -> PinterestError:
    lowered = text.lower()
    for needle, explanation, retryable in KNOWN_ERRORS:
        if needle in lowered:
            return PinterestError(f"{explanation} ({text.strip()[:200]})", retryable)
    return PinterestError(text.strip()[:300])


class PinterestClient:
    def __init__(self, webhook_url: str, secret: str, path_prefix: str = "/v5"):
        self.webhook_url = webhook_url
        self.secret = secret
        self.path_prefix = path_prefix.rstrip("/")
        self._http = httpx.AsyncClient(timeout=120)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        full_path = self.path_prefix + path
        if query:
            full_path += "?" + urlencode(query)
        payload = {
            "secret": self.secret,
            "method": method,
            "path": full_path,
            # Тело передаём готовой JSON-строкой, чтобы в Make его можно было вставить как есть.
            "body": json.dumps(body) if body is not None else "",
        }
        try:
            resp = await self._http.post(self.webhook_url, json=payload)
        except httpx.HTTPError as e:
            raise PinterestError(f"Make недоступен: {e}", retryable=True) from e
        try:
            data = resp.json()
        except ValueError:
            text = resp.text.strip()
            # Make отвечает "Accepted", если сценарий не запустился (кончились кредиты, выключен)
            # или не дошёл до Webhook response.
            if resp.status_code < 400 or any(s in text.lower() for s in MAKE_STOPPED):
                raise MakeStoppedError(
                    f"Make не запустил сценарий — скорее всего, кончились кредиты или сценарий выключен "
                    f"({resp.status_code}: {text[:200]})",
                    queued_in_make=resp.status_code < 400,
                ) from None
            # Обработчик ошибок в сценарии возвращает текст ошибки Pinterest как есть.
            raise make_error(text) from None
        if resp.status_code >= 400:
            message = data.get("message", data) if isinstance(data, dict) else data
            raise make_error(f"[{resp.status_code}] {message}")
        return data

    async def list_boards(self) -> list[dict]:
        boards, bookmark = [], None
        while True:
            query = {"page_size": 100}
            if bookmark:
                query["bookmark"] = bookmark
            data = await self._request("GET", "/boards", query=query)
            boards.extend({"id": b["id"], "name": b["name"]} for b in data.get("items", []))
            bookmark = data.get("bookmark")
            if not bookmark:
                return boards

    async def create_pin(self, board_id: str, image: bytes, content_type: str) -> dict:
        body = {
            "board_id": board_id,
            "media_source": {
                "source_type": "image_base64",
                "content_type": content_type,
                "data": base64.b64encode(image).decode("ascii"),
            },
        }
        return await self._request("POST", "/pins", body=body)

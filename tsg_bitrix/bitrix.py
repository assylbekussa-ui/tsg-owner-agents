"""Клиент REST Битрикс24 только на чтение.

Любой метод вне белого списка отклоняется до отправки запроса,
поэтому даже вебхук с правами администратора через этот клиент
ничего не может изменить на портале.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

READ_ONLY_METHODS = frozenset(
    {
        "crm.item.list",
        "crm.item.get",
        "crm.item.fields",
        "crm.category.list",
        "crm.status.list",
        "crm.deal.list",
        "crm.lead.list",
        "tasks.task.list",
        "tasks.task.get",
        "user.get",
        "department.get",
        "disk.storage.getlist",
        "disk.storage.getchildren",
        "disk.folder.getchildren",
        "disk.folder.get",
        "disk.file.get",
        "im.recent.list",
    }
)
# Запись: только эти два действия и только через write() — его вызывают инструменты Тимлида,
# каждый вызов которых владелец подтверждает кнопкой в Telegram.
WRITE_METHODS = frozenset({"im.message.add", "tasks.task.add", "task.commentitem.add"})

PAGE = 50  # Битрикс отдаёт по 50 записей за запрос


class BitrixError(RuntimeError):
    pass


class MethodNotAllowed(BitrixError):
    pass


class BitrixClient:
    def __init__(self, webhook_url: str, transport: httpx.AsyncBaseTransport | None = None):
        if not webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL не задан")
        self.base = webhook_url.rstrip("/") + "/"
        self._http = httpx.AsyncClient(timeout=30, transport=transport)
        self._lock = asyncio.Semaphore(2)  # лимит Битрикса ~2 запроса в секунду

    async def aclose(self) -> None:
        await self._http.aclose()

    async def write(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in WRITE_METHODS:
            raise MethodNotAllowed(f"Метод {method} запрещён для записи")
        return await self._send(method, params)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method not in READ_ONLY_METHODS:
            raise MethodNotAllowed(f"Метод {method} запрещён: сервер работает только на чтение")
        return await self._send(method, params)

    async def _send(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        for attempt in range(4):
            async with self._lock:
                resp = await self._http.post(f"{self.base}{method}.json", json=params or {})
            data = resp.json() if resp.content else {}
            err = data.get("error")
            if err == "QUERY_LIMIT_EXCEEDED":
                await asyncio.sleep(1 + attempt)
                continue
            if err:
                raise BitrixError(f"{method}: {err} {data.get('error_description', '')}".strip())
            resp.raise_for_status()
            return data
        raise BitrixError(f"{method}: превышен лимит запросов Битрикса")

    async def list_all(
        self, method: str, params: dict[str, Any], key: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Постраничная выборка. Возвращает записи и общее число записей по фильтру."""
        out: list[dict[str, Any]] = []
        start = 0
        total = None
        while len(out) < limit:
            data = await self.call(method, {**params, "start": start})
            result = data.get("result", [])
            items = result.get(key, []) if key else result
            total = data.get("total", total)
            out.extend(items)
            nxt = data.get("next")
            if nxt is None or not items:
                break
            start = nxt
        return out[:limit], total

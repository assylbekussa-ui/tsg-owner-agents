"""MCP-сервер «tsg-bitrix»: отдельный адрес и токен для каждого агента.

Адрес агента:  https://<хост>/<роль>/mcp   (роль: teamlead, production, account, accountant …)
Токен агента:  MCP_TOKEN_<РОЛЬ> или выводится из ANTHROPIC_API_KEY (см. provision.role_token), передаётся как Bearer.
Агент с одной ролью не может обратиться к адресу другой роли: токен не подойдёт.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from .bitrix import BitrixClient
from .data import TZ, AccessDenied, DataService
from .policy import ENTITY_TITLES, ROLES, STAGE_2_ROLES
from .provision import role_token

log = logging.getLogger("tsg-bitrix")
JOURNAL_PATH = os.getenv("JOURNAL_PATH")


def journal(role: str, tool: str, args: dict[str, Any], result: Any, error: str | None = None) -> None:
    """Журнал запросов: кто (роль), какой инструмент, с какими параметрами, сколько данных отдано."""
    size = len(result) if isinstance(result, list) else result.get("выдано", 1) if isinstance(result, dict) else 0
    line = json.dumps(
        {"ts": datetime.now(TZ).isoformat(timespec="seconds"), "role": role, "tool": tool,
         "args": args, "records": size if not error else 0, "error": error},
        ensure_ascii=False,
    )
    print(line, file=sys.stdout, flush=True)
    if JOURNAL_PATH:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def build_role_server(role_key: str, svc: DataService) -> MCPServer:
    role = ROLES[role_key]
    sections = ", ".join(f"{k} — {ENTITY_TITLES[k]}" for k in role.entities)
    server = MCPServer(
        f"tsg-bitrix-{role_key}",
        instructions=(
            f"Данные портала Битрикс24 Top Service Group для роли «{role.title}». Только чтение. "
            f"Доступные разделы: {sections}. Скрытые поля роль не получает вообще."
        ),
    )

    async def run(tool: str, args: dict[str, Any], coro):
        try:
            result = await coro
        except AccessDenied as exc:
            journal(role_key, tool, args, None, error=f"отказ: {exc}")
            return {"ошибка": str(exc)}
        except Exception as exc:  # ошибка Битрикса не должна ронять агента
            journal(role_key, tool, args, None, error=str(exc))
            return {"ошибка": f"Битрикс вернул ошибку: {exc}"}
        journal(role_key, tool, args, result)
        return result

    if role.entities:
        @server.tool(description=(
            "Список записей раздела Битрикса. "
            f"entity — один из: {sections}. "
            "filters — фильтр Битрикса по видимым полям, например {\"=stageId\": \"DT1068_5:NEW\"} "
            "или {\">=createdTime\": \"2026-10-15T00:00:00+05:00\"}. Имена полей — через describe_section. "
            "limit — максимум записей (по умолчанию 200)."
        ))
        async def list_section(entity: str, filters: dict[str, Any] | None = None, limit: int = 200) -> dict:
            args = {"entity": entity, "filters": filters, "limit": limit}
            return await run("list_section", args, svc.list_items(role_key, entity, filters, limit))

        @server.tool(description="Одна запись раздела по ID со всеми видимыми для роли полями.")
        async def get_record(entity: str, record_id: int) -> dict:
            args = {"entity": entity, "record_id": record_id}
            return await run("get_record", args, svc.get_item(role_key, entity, record_id))

        @server.tool(description="Какие поля раздела видит эта роль: системное имя, название, тип. "
                                 "Нужен, чтобы составить фильтр для list_section.")
        async def describe_section(entity: str) -> list:
            return await run("describe_section", {"entity": entity}, svc.describe(role_key, entity))

    if role.tasks:
        @server.tool(description=(
            "Задачи Битрикса. responsible_id — ID исполнителя (из list_employees). "
            "overdue_only — только просроченные и не закрытые. "
            "closed_since — закрытые с даты ISO, например 2026-10-15T00:00:00+05:00."
        ))
        async def list_tasks(responsible_id: int | None = None, overdue_only: bool = False,
                             closed_since: str | None = None, limit: int = 200) -> list:
            args = {"responsible_id": responsible_id, "overdue_only": overdue_only,
                    "closed_since": closed_since, "limit": limit}
            return await run("list_tasks", args, svc.tasks(role_key, responsible_id, overdue_only, closed_since, limit))

    if role.directory:
        @server.tool(description="Активные сотрудники портала: ID, имя, должность, подразделения.")
        async def list_employees() -> list:
            return await run("list_employees", {}, svc.users())

        @server.tool(description="Подразделения портала: ID, название, родитель, руководитель.")
        async def list_departments() -> list:
            return await run("list_departments", {}, svc.departments())

    if role.disk:
        folders = "все папки" if "*" in role.disk else ", ".join(role.disk)
        @server.tool(description=(
            f"Битрикс-Диск, общий диск компании. Доступные папки: {folders}. "
            "Без folder_id — список доступных папок верхнего уровня; с folder_id — содержимое папки. "
            "Возвращает названия, даты и ссылки на файлы в Битриксе (ссылку можно дать человеку)."
        ))
        async def disk_list(folder_id: int | None = None) -> dict:
            return await run("disk_list", {"folder_id": folder_id}, svc.disk_list(role_key, folder_id))

        @server.tool(description="Поиск файлов и папок по части названия в доступных роли папках общего диска.")
        async def disk_search(query: str, limit: int = 30) -> list:
            return await run("disk_search", {"query": query}, svc.disk_search(role_key, query, limit))

    if role.writes:
        @server.tool(description="Групповые чаты Битрикса, в которых состоит владелец кабинета: dialog_id и название.")
        async def list_chats() -> list:
            return await run("list_chats", {}, svc.list_chats(role_key))

        @server.tool(description=(
            "Отправить сообщение в Битрикс-чат от имени владельца кабинета. dialog_id — ID сотрудника "
            "из list_employees (личное сообщение) или ID группового чата вида chat123 из list_chats. "
            "Перед отправкой владелец видит текст и подтверждает его кнопкой; если отказал — не повторяй."
        ))
        async def send_message(dialog_id: str, text: str) -> dict:
            return await run("send_message", {"dialog_id": dialog_id, "text": text[:300]},
                             svc.send_message(role_key, dialog_id, text))

        @server.tool(description=(
            "Написать комментарий в задачу Битрикса (его видят исполнитель, постановщик и наблюдатели). "
            "task_id — номер задачи. Владелец подтверждает кнопкой."
        ))
        async def comment_task(task_id: int, text: str) -> dict:
            return await run("comment_task", {"task_id": task_id, "text": text[:300]},
                             svc.comment_task(role_key, task_id, text))

        @server.tool(description=(
            "Поставить задачу в Битриксе от имени владельца кабинета. responsible_id — ID исполнителя "
            "из list_employees; deadline — ISO-дата и время по Алматы, например 2026-10-17T18:00:00+05:00. "
            "Владелец подтверждает каждую задачу кнопкой; при отказе задача не создаётся."
        ))
        async def create_task(responsible_id: int, title: str, description: str, deadline: str | None = None) -> dict:
            args = {"responsible_id": responsible_id, "title": title, "deadline": deadline}
            return await run("create_task", args, svc.create_task(role_key, responsible_id, title, description, deadline))

    @server.tool(description="Текущие дата и время в Алматы.")
    async def now_almaty() -> str:
        return datetime.now(TZ).strftime("%Y-%m-%d %H:%M, %A")

    return server


class BearerPerRole:
    """Проверяет, что токен в запросе выдан именно той роли, чей адрес вызывается."""

    def __init__(self, app, tokens: dict[str, str]):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            role = path.strip("/").split("/", 1)[0]
            if role in self.tokens:
                header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
                given = header[7:] if header.lower().startswith("bearer ") else ""
                if not given or not hmac.compare_digest(given, self.tokens[role]):
                    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                    return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(client: BitrixClient | None = None, roles: tuple[str, ...] | None = None,
               tokens: dict[str, str] | None = None) -> Any:
    roles = roles or tuple(os.getenv("ENABLED_ROLES", ",".join(STAGE_2_ROLES)).split(","))
    tokens = tokens if tokens is not None else {r: role_token(r) for r in roles}
    missing = [r for r in roles if not tokens.get(r)]
    if missing:
        raise RuntimeError("Нет ANTHROPIC_API_KEY (или MCP_TOKEN_*): токены ролей не из чего получить")

    client = client or BitrixClient(os.getenv("BITRIX_WEBHOOK_URL", ""))
    svc = DataService(client)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    servers = {r: build_role_server(r, svc) for r in roles}
    mounts = [
        Mount(f"/{r}", app=s.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=security))
        for r, s in servers.items()
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for s in servers.values():
                await stack.enter_async_context(s.session_manager.run())
            yield
        await client.aclose()

    async def health(request: Request):
        return PlainTextResponse(f"ok {time.strftime('%H:%M:%S')} roles={','.join(roles)}")

    app = Starlette(routes=[Route("/health", health), *mounts], lifespan=lifespan)
    return BearerPerRole(app, tokens)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), lifespan="on")


if __name__ == "__main__":
    main()

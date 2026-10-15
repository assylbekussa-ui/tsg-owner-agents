"""Чтение данных Битрикса с фильтрацией полей по роли агента."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .bitrix import BitrixClient
from .policy import ENTITY_IDS, ROLES, Role, classify_field

TZ = ZoneInfo("Asia/Almaty")
SERVICE_FIELDS = {
    "id": "ID",
    "title": "Название",
    "stageId": "Стадия",
    "createdTime": "Создано",
    "updatedTime": "Изменено",
    "assignedById": "Ответственный (ID)",
}
SKIP_FIELDS = {"xmlId", "opened", "createdBy", "updatedBy", "movedBy", "movedTime", "webformId",
               "lastActivityBy", "lastActivityTime", "observers", "utmSource", "utmMedium",
               "utmCampaign", "utmContent", "utmTerm", "entityTypeId", "categoryId", "previousStageId"}
TASK_STATUS = {"1": "новая", "2": "ждёт выполнения", "3": "выполняется", "4": "ждёт контроля",
               "5": "завершена", "6": "отложена", "7": "отклонена"}


class AccessDenied(PermissionError):
    pass


class DataService:
    def __init__(self, client: BitrixClient, cache_ttl: int = 600):
        self.bx = client
        self.ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _cached(self, key: str, loader):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        value = await loader()
        self._cache[key] = (time.time(), value)
        return value

    # ---------- метаданные ----------
    async def fields(self, entity: str) -> dict[str, dict[str, Any]]:
        etid = ENTITY_IDS[entity]

        async def load():
            data = await self.bx.call("crm.item.fields", {"entityTypeId": etid})
            return data.get("result", {}).get("fields", {})

        return await self._cached(f"fields:{etid}", load)

    async def stages(self, entity: str) -> dict[str, str]:
        etid = ENTITY_IDS[entity]

        async def load():
            names: dict[str, str] = {}
            cats = await self.bx.call("crm.category.list", {"entityTypeId": etid})
            for cat in cats.get("result", {}).get("categories", []):
                st = await self.bx.call(
                    "crm.status.list", {"filter": {"ENTITY_ID": f"DYNAMIC_{etid}_STAGE_{cat['id']}"}}
                )
                for s in st.get("result", []):
                    names[s["STATUS_ID"]] = s["NAME"]
            return names

        return await self._cached(f"stages:{etid}", load)

    @staticmethod
    def field_title(name: str, meta: dict[str, Any]) -> str:
        return (meta.get("title") or meta.get("listLabel") or meta.get("formLabel") or name).strip()

    def _check(self, role: Role, entity: str) -> bool:
        if entity not in role.entities:
            raise AccessDenied(f"У роли «{role.title}» нет доступа к разделу «{entity}».")
        return role.entities[entity]

    async def allowed_fields(self, role: Role, entity: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """Поля сущности, которые видит роль: имя -> (заголовок, метаданные)."""
        money_ok = self._check(role, entity)
        out = {}
        for name, meta in (await self.fields(entity)).items():
            if name in SKIP_FIELDS:
                continue
            title = SERVICE_FIELDS.get(name) or self.field_title(name, meta)
            kind = classify_field(name, title)
            if kind == "pii" or (kind == "money" and not money_ok):
                continue
            out[name] = (title, meta)
        return out

    # ---------- элементы ----------
    async def _format(self, role: Role, entity: str, item: dict[str, Any]) -> dict[str, Any]:
        allowed = await self.allowed_fields(role, entity)
        stages = await self.stages(entity)
        row: dict[str, Any] = {}
        for name, (title, meta) in allowed.items():
            if name not in item:
                continue
            value = item[name]
            if value in (None, "", [], {}):
                continue
            if name == "stageId":
                value = stages.get(value, value)
            elif meta.get("items"):  # списки: id -> текст
                labels = {str(i.get("ID")): i.get("VALUE") for i in meta["items"]}
                value = [labels.get(str(v), v) for v in value] if isinstance(value, list) else labels.get(str(value), value)
            row[title] = value
        return row

    async def list_items(self, role_key: str, entity: str, filters: dict[str, Any] | None,
                         limit: int = 200) -> dict[str, Any]:
        role = ROLES[role_key]
        allowed = await self.allowed_fields(role, entity)
        # Фильтр разрешён только по видимым полям, чтобы роль не «угадывала» скрытые значения.
        safe_filter = {}
        for key, value in (filters or {}).items():
            bare = key.lstrip("=<>!%@")
            if bare not in allowed:
                raise AccessDenied(f"Фильтр по полю {bare} недоступен этой роли.")
            safe_filter[key] = value
        items, total = await self.bx.list_all(
            "crm.item.list",
            {"entityTypeId": ENTITY_IDS[entity], "select": ["*", "uf*"], "filter": safe_filter},
            "items",
            min(max(limit, 1), 1000),
        )
        rows = [await self._format(role, entity, it) for it in items]
        return {"раздел": entity, "всего_по_фильтру": total, "выдано": len(rows), "записи": rows}

    async def get_item(self, role_key: str, entity: str, item_id: int) -> dict[str, Any]:
        role = ROLES[role_key]
        self._check(role, entity)
        data = await self.bx.call("crm.item.get", {"entityTypeId": ENTITY_IDS[entity], "id": item_id})
        return await self._format(role, entity, data.get("result", {}).get("item", {}))

    async def describe(self, role_key: str, entity: str) -> list[dict[str, str]]:
        role = ROLES[role_key]
        allowed = await self.allowed_fields(role, entity)
        return [{"поле": n, "название": t, "тип": m.get("type", "")} for n, (t, m) in allowed.items()]

    # ---------- задачи и справочник ----------
    async def tasks(self, role_key: str, responsible_id: int | None, overdue_only: bool,
                    closed_since: str | None, limit: int) -> list[dict[str, Any]]:
        role = ROLES[role_key]
        if not role.tasks:
            raise AccessDenied(f"У роли «{role.title}» нет доступа к задачам.")
        flt: dict[str, Any] = {}
        if responsible_id:
            flt["RESPONSIBLE_ID"] = responsible_id
        now = datetime.now(TZ)
        if overdue_only:
            flt["<DEADLINE"] = now.isoformat()
        if closed_since:
            flt[">=CLOSED_DATE"] = closed_since
        tasks, _ = await self.bx.list_all(
            "tasks.task.list",
            {"filter": flt, "select": ["ID", "TITLE", "STATUS", "RESPONSIBLE_ID", "CREATED_BY",
                                       "DEADLINE", "CLOSED_DATE", "CREATED_DATE"]},
            "tasks",
            min(max(limit, 1), 500),
        )
        out = []
        for t in tasks:
            status = str(t.get("status", ""))
            if overdue_only and status in ("4", "5", "7"):
                continue
            out.append({
                "id": t.get("id"), "название": t.get("title"),
                "статус": TASK_STATUS.get(status, status),
                "исполнитель_id": t.get("responsibleId"), "постановщик_id": t.get("createdBy"),
                "срок": t.get("deadline"), "закрыта": t.get("closedDate"), "создана": t.get("createdDate"),
            })
        return out

    async def users(self) -> list[dict[str, Any]]:
        async def load():
            users, _ = await self.bx.list_all("user.get", {"FILTER": {"ACTIVE": True}}, None, 1000)
            return [
                {"id": u.get("ID"), "имя": " ".join(x for x in [u.get("NAME"), u.get("LAST_NAME")] if x),
                 "должность": u.get("WORK_POSITION"), "подразделения": u.get("UF_DEPARTMENT")}
                for u in users
            ]
        return await self._cached("users", load)

    async def departments(self) -> list[dict[str, Any]]:
        async def load():
            deps, _ = await self.bx.list_all("department.get", {}, None, 1000)
            return [{"id": d.get("ID"), "название": d.get("NAME"), "родитель": d.get("PARENT"),
                     "руководитель_id": d.get("UF_HEAD")} for d in deps]
        return await self._cached("departments", load)

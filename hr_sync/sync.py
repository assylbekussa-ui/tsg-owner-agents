"""Синхронизация «Кандидаты» → «Сотрудники» и переход по стажу в Битриксе.

Работает опросом (без входящих вебхуков): раз в POLL_SECONDS секунд
  1. берёт кандидатов на стадии «Устроился в штат и работает» и создаёт для каждого
     карточку в смарт-процессе «Сотрудники» (колонка «До 6 месяцев», дата приёма = сегодня);
  2. раз в сутки двигает сотрудников вперёд по стажу: 6 месяцев -> «До 1 года»,
     12 месяцев -> «Более 1 года». Назад карточки не двигаются.

Повторная карточка не создаётся: у сотрудника во «Внешнем коде» (xmlId) хранится cand-<id кандидата>,
а при его отсутствии совпадение ищется по телефону.
Нужен вебхук Битрикса с правом на CRM (запись): переменная BITRIX_HRSYNC_WEBHOOK.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger("hr_sync")

CANDIDATES = 1066
EMPLOYEES = 1060
EMP_CATEGORY = 25

TRIGGER_STAGE = os.getenv("HRSYNC_TRIGGER_STAGE", "DT1066_29:UC_BBNZ2K")
STAGE_6M = "DT1060_25:NEW"          # До 6 месяцев
STAGE_1Y = "DT1060_25:UC_9059MU"    # До 1 года
STAGE_OVER = "DT1060_25:PREPARATION"  # Более 1 года

F_PHONE = "ufCrm17_1791291278638"
F_STATUS = "ufCrm17_1791463276337"
STATUS_WORKS = 685
SOURCE_CANDIDATES = 819
F_HIRE = "ufCrm17HireDate"
F_POSITION = "ufCrm17PositionTxt"
F_SOURCE = "ufCrm17Source"
C_PHONE = "ufCrm21Phone"
C_CITY = "ufCrm21TsgCity"

ALMATY = timezone(timedelta(hours=5))


def today() -> date:
    return datetime.now(ALMATY).date()


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def norm_phone(s: str | None) -> str:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return digits


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def target_stage(hire: date, now: date, current: str) -> str | None:
    """Стадия по стажу, если она дальше текущей; иначе None (назад не двигаем)."""
    if now >= add_months(hire, 12):
        wanted = STAGE_OVER
    elif now >= add_months(hire, 6):
        wanted = STAGE_1Y
    else:
        wanted = STAGE_6M
    order = [STAGE_6M, STAGE_1Y, STAGE_OVER]
    if current not in order or order.index(wanted) <= order.index(current):
        return None
    return wanted


class Bitrix:
    def __init__(self, webhook: str, client: httpx.AsyncClient | None = None) -> None:
        self.base = webhook.rstrip("/") + "/"
        self.http = client or httpx.AsyncClient(timeout=30)

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self.http.post(self.base + method + ".json", json=params)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method}: {data.get('error')} {data.get('error_description', '')}")
        return data

    async def items(self, entity: int, filter_: dict[str, Any], select: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            data = await self.call("crm.item.list", {"entityTypeId": entity, "filter": filter_,
                                                     "select": select, "start": start})
            out += data["result"]["items"]
            if "next" not in data:
                return out
            start = data["next"]


async def sync_candidates(bx: Bitrix, now: date | None = None) -> int:
    """Создаёт сотрудников для кандидатов на стадии «Устроился в штат». Возвращает число созданных."""
    now = now or today()
    cands = await bx.items(CANDIDATES, {"stageId": TRIGGER_STAGE},
                           ["id", "title", "assignedById", C_PHONE, C_CITY])
    if not cands:
        return 0
    emps = await bx.items(EMPLOYEES, {}, ["id", "xmlId", F_PHONE])
    known_ids = {e.get("xmlId") for e in emps if e.get("xmlId")}
    known_phones = {norm_phone(e.get(F_PHONE)) for e in emps if e.get(F_PHONE)}
    created = 0
    for c in cands:
        xml_id = f"cand-{c['id']}"
        phone = norm_phone(c.get(C_PHONE))
        if xml_id in known_ids or (phone and phone in known_phones):
            continue
        fields: dict[str, Any] = {
            "title": c.get("title") or f"Кандидат {c['id']}",
            "categoryId": EMP_CATEGORY, "stageId": STAGE_6M, "opened": "Y",
            "xmlId": xml_id, F_HIRE: now.isoformat(), F_SOURCE: SOURCE_CANDIDATES, F_STATUS: STATUS_WORKS,
        }
        if c.get(C_PHONE):
            fields[F_PHONE] = c[C_PHONE]
        if c.get("assignedById"):
            fields["assignedById"] = c["assignedById"]
        await bx.call("crm.item.add", {"entityTypeId": EMPLOYEES, "fields": fields})
        created += 1
        known_ids.add(xml_id)
        if phone:
            known_phones.add(phone)
        log.info("создан сотрудник из кандидата %s", c["id"])
    return created


async def advance_by_tenure(bx: Bitrix, now: date | None = None) -> int:
    """Двигает сотрудников вперёд по стажу. Возвращает число перенесённых карточек."""
    now = now or today()
    emps = await bx.items(EMPLOYEES, {"stageId": [STAGE_6M, STAGE_1Y]}, ["id", "stageId", F_HIRE])
    moved = 0
    for e in emps:
        hire = parse_date(e.get(F_HIRE))
        if not hire:
            continue
        stage = target_stage(hire, now, e["stageId"])
        if stage:
            await bx.call("crm.item.update", {"entityTypeId": EMPLOYEES, "id": e["id"],
                                              "fields": {"stageId": stage}})
            moved += 1
            log.info("сотрудник %s -> %s", e["id"], stage)
    return moved


async def run() -> None:
    webhook = os.getenv("BITRIX_HRSYNC_WEBHOOK")
    if not webhook:
        raise SystemExit("BITRIX_HRSYNC_WEBHOOK не задан")
    poll = int(os.getenv("POLL_SECONDS", "300"))
    bx = Bitrix(webhook)
    last_aging: date | None = None
    while True:
        try:
            await sync_candidates(bx)
            if last_aging != today():
                await advance_by_tenure(bx)
                last_aging = today()
        except Exception as exc:  # цикл не должен падать из-за разовой ошибки Битрикса
            log.error("синхронизация не удалась: %s", str(exc)[:200])
        await asyncio.sleep(poll)

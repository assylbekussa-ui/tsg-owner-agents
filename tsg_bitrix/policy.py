"""Матрица доступа агентов к данным Битрикса.

Соответствует разделу «Инструменты и права доступа» документа
«Команда ИИ-агентов TSG». Меняется здесь, а не в промптах агентов:
агент физически не получает поля, которых нет в его наборе.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

# entityTypeId смарт-процессов портала top-cleaning (разведка 14.10.2026).
# Можно переопределить переменной окружения ENTITY_IDS='{"objects": 1054, ...}'.
ENTITY_IDS: dict[str, int] = {
    "objects": 1054,      # Объекты
    "shifts": 1076,       # Смены (строки штатных расписаний)
    "complaints": 1068,   # Обращения клиентов
    "nps": 1070,          # Опросы NPS
    "audits": 1072,       # Аудиты объектов
    "contracts": 1074,    # Действующие договоры
    "accounting": 1050,   # Бухгалтерия
    "employees": 1060,    # Сотрудники
    "vacancies": 1064,    # Вакансии
    "candidates": 1066,   # Кандидаты
}
ENTITY_IDS.update({k: int(v) for k, v in json.loads(os.getenv("ENTITY_IDS", "{}")).items()})

ENTITY_TITLES = {
    "objects": "Объекты",
    "shifts": "Смены (штатные расписания)",
    "complaints": "Обращения клиентов",
    "nps": "Опросы NPS",
    "audits": "Аудиты объектов",
    "contracts": "Действующие договоры",
    "accounting": "Бухгалтерия",
    "employees": "Сотрудники",
    "vacancies": "Вакансии",
    "candidates": "Кандидаты",
}

# Категории полей. money — деньги (ЗП, ФОТ, сметы, ОФ/НЕ ОФ, суммы договоров).
# pii — документы и реквизиты людей: не отдаются никому из агентов.
MONEY_RE = re.compile(
    r"(зп|зарплат|заработн|оклад|фот|смет|бюджет|ставк|сумм|оф\b|не\s*оф|официал|"
    r"на\s*руки|оплат|стоимост|цен[аы]|выручк|реализац|ндс|тенге|₸|доплат|преми)",
    re.IGNORECASE,
)
PII_RE = re.compile(
    r"(иин|паспорт|удостоверен|банковск|карт[аы]\s|номер\s*карт|счет|счёт|iban|арест)",
    re.IGNORECASE,
)
FIELD_OVERRIDES: dict[str, str] = json.loads(os.getenv("FIELD_OVERRIDES", "{}"))


def classify_field(name: str, title: str) -> str:
    """Возвращает 'pii', 'money' или 'plain'."""
    if name in FIELD_OVERRIDES:
        return FIELD_OVERRIDES[name]
    text = f"{title} {name}"
    if PII_RE.search(text):
        return "pii"
    if MONEY_RE.search(text):
        return "money"
    return "plain"


@dataclass(frozen=True)
class Role:
    key: str
    title: str
    # сущность -> True, если роль видит денежные поля этой сущности
    entities: dict[str, bool] = field(default_factory=dict)
    tasks: bool = False
    directory: bool = True  # сотрудники и отделы портала


ROLES: dict[str, Role] = {
    r.key: r
    for r in [
        Role("teamlead", "Тимлид", {"objects": False}, tasks=True),
        Role(
            "production",
            "Агент производства",
            {"objects": False, "shifts": False, "complaints": False, "nps": False, "audits": False},
            tasks=True,
        ),
        Role(
            "account",
            "Аккаунт-агент",
            # Решение 14.10.2026: аккаунт видит сметы полностью, включая ОФ / НЕ ОФ.
            {"objects": True, "shifts": True, "complaints": True, "nps": True, "audits": True, "contracts": True},
            tasks=True,
        ),
        Role(
            "accountant",
            "Бухгалтер",
            {"objects": True, "shifts": True, "contracts": True, "accounting": True},
        ),
        # Этап 4: HR, продажи, аналитик, юрист. Наборы уже заданы, агенты подключаются позже.
        Role(
            "hr",
            "HR-агент",
            {"vacancies": False, "candidates": False, "employees": False, "objects": False, "shifts": False},
            tasks=True,
        ),
        Role("lawyer", "Юрист", {"contracts": True, "complaints": False, "objects": False}),
    ]
}

STAGE_2_ROLES = ("teamlead", "production", "account", "accountant")

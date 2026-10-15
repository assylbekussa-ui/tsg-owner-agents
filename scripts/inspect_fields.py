"""Показывает, какие поля каждого смарт-процесса увидит каждая роль.

    python scripts/inspect_fields.py            # нужен BITRIX_WEBHOOK_URL

Запустите один раз после подключения вебхука: проверьте, что ЗП, ФОТ, ОФ / НЕ ОФ и ИИН
попали в скрытые. Если какое-то поле классифицировано неверно, задайте FIELD_OVERRIDES,
например FIELD_OVERRIDES='{"ufCrm20_1712345": "money"}'.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tsg_bitrix.bitrix import BitrixClient  # noqa: E402
from tsg_bitrix.data import DataService  # noqa: E402
from tsg_bitrix.policy import ENTITY_IDS, ENTITY_TITLES, ROLES, STAGE_2_ROLES, classify_field  # noqa: E402


async def main() -> None:
    svc = DataService(BitrixClient(os.environ["BITRIX_WEBHOOK_URL"]))
    for entity in ENTITY_IDS:
        try:
            fields = await svc.fields(entity)
        except Exception as exc:
            print(f"\n## {ENTITY_TITLES[entity]}: ошибка {exc}")
            continue
        print(f"\n## {ENTITY_TITLES[entity]} ({ENTITY_IDS[entity]}): {len(fields)} полей")
        for name, meta in fields.items():
            title = svc.field_title(name, meta)
            kind = classify_field(name, title)
            seen = [r for r in STAGE_2_ROLES if entity in ROLES[r].entities
                    and (kind == "plain" or (kind == "money" and ROLES[r].entities[entity]))]
            print(f"  {kind:5}  {name:32} {title[:40]:40} видят: {', '.join(seen) or '—'}")
    await svc.bx.aclose()


asyncio.run(main())

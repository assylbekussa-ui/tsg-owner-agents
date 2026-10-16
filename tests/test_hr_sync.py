import asyncio
from datetime import date

from hr_sync import sync
from hr_sync.sync import STAGE_1Y, STAGE_6M, STAGE_OVER, add_months, norm_phone, target_stage


def test_add_months_clamps_day():
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert add_months(date(2026, 10, 16), 12) == date(2027, 10, 16)


def test_norm_phone():
    assert norm_phone("+7 (771) 071-93-75") == "7710719375"
    assert norm_phone("87710719375") == "7710719375"


def test_target_stage_only_forward():
    hire = date(2026, 10, 16)
    assert target_stage(hire, date(2027, 3, 1), STAGE_6M) is None
    assert target_stage(hire, date(2027, 4, 16), STAGE_6M) == STAGE_1Y
    assert target_stage(hire, date(2027, 10, 16), STAGE_6M) == STAGE_OVER
    assert target_stage(hire, date(2027, 10, 16), STAGE_1Y) == STAGE_OVER
    # назад не двигаем: сегодняшняя дата приёма у карточки «Более 1 года» ничего не меняет
    assert target_stage(hire, date(2026, 10, 17), STAGE_OVER) is None


class FakeBitrix:
    def __init__(self, cands, emps):
        self.cands, self.emps, self.calls = cands, emps, []

    async def items(self, entity, filter_, select):
        if entity == sync.CANDIDATES:
            return self.cands
        return [e for e in self.emps if "stageId" not in filter_ or e["stageId"] in filter_["stageId"]]

    async def call(self, method, params):
        self.calls.append((method, params))
        return {"result": {}}


def test_sync_creates_and_dedupes():
    cands = [{"id": 5, "title": "Иван", sync.C_PHONE: "+7 701 000 00 01", "assignedById": 25},
             {"id": 6, "title": "Дубль по телефону", sync.C_PHONE: "87010000002"},
             {"id": 7, "title": "Уже создан"}]
    emps = [{"id": 1, "xmlId": "", sync.F_PHONE: "+77010000002", "stageId": STAGE_6M},
            {"id": 2, "xmlId": "cand-7", "stageId": STAGE_6M}]
    bx = FakeBitrix(cands, emps)
    assert asyncio.run(sync.sync_candidates(bx, date(2026, 10, 16))) == 1
    method, params = bx.calls[0]
    assert method == "crm.item.add"
    f = params["fields"]
    assert f["stageId"] == STAGE_6M and f["xmlId"] == "cand-5" and f[sync.F_HIRE] == "2026-10-16"


def test_advance_skips_missing_hire_date():
    emps = [{"id": 1, "stageId": STAGE_6M, sync.F_HIRE: "2026-01-01T00:00:00+05:00"},
            {"id": 2, "stageId": STAGE_6M, sync.F_HIRE: None},
            {"id": 3, "stageId": STAGE_6M, sync.F_HIRE: "2026-10-16T00:00:00+05:00"}]
    bx = FakeBitrix([], emps)
    assert asyncio.run(sync.advance_by_tenure(bx, date(2026, 10, 16))) == 1
    assert bx.calls[0][1]["id"] == 1 and bx.calls[0][1]["fields"]["stageId"] == STAGE_1Y

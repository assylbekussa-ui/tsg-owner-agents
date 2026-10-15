"""Проверка прав без доступа к реальному Битриксу: Битрикс подменён фиктивными ответами."""
import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from tsg_bitrix.app import build_role_server, create_app
from tsg_bitrix.bitrix import BitrixClient, MethodNotAllowed
from tsg_bitrix.data import AccessDenied, DataService
from tsg_bitrix.policy import classify_field

SHIFT_FIELDS = {
    "id": {"type": "integer", "title": "ID"},
    "title": {"type": "string", "title": "Название"},
    "stageId": {"type": "crm_status", "title": "Стадия"},
    "parentId1054": {"type": "crm_entity", "title": "Объекты"},
    "ufCrm20Position": {"type": "string", "title": "Должность"},
    "ufCrm20Schedule": {"type": "string", "title": "График"},
    "ufCrm20Salary": {"type": "double", "title": "ЗП на руки за 1 чел."},
    "ufCrm20Fot": {"type": "double", "title": "ФОТ по строке"},
    "ufCrm20Official": {"type": "enumeration", "title": "Оформление (ОФ/НЕ ОФ)",
                        "items": [{"ID": "1", "VALUE": "ОФ"}, {"ID": "2", "VALUE": "НЕ ОФ"}]},
    "ufCrm20Iin": {"type": "string", "title": "ИИН сотрудника"},
}
SHIFT_ITEM = {
    "id": 7, "title": "OP-14 ОПУ ночь", "stageId": "DT1076_3:NEW", "parentId1054": 14,
    "ufCrm20Position": "ОПУ (ночь)", "ufCrm20Schedule": "2/2 20:00–08:00",
    "ufCrm20Salary": 135000, "ufCrm20Fot": 1350000, "ufCrm20Official": "2", "ufCrm20Iin": "900101300000",
}
CALLS: list[tuple[str, dict]] = []


def fake_bitrix(request: httpx.Request) -> httpx.Response:
    method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
    body = json.loads(request.content or b"{}")
    CALLS.append((method, body))
    if method == "crm.item.fields":
        return httpx.Response(200, json={"result": {"fields": SHIFT_FIELDS}})
    if method == "crm.category.list":
        return httpx.Response(200, json={"result": {"categories": [{"id": 3}]}})
    if method == "crm.status.list":
        return httpx.Response(200, json={"result": [{"STATUS_ID": "DT1076_3:NEW", "NAME": "Действует"}]})
    if method == "crm.item.list":
        return httpx.Response(200, json={"result": {"items": [SHIFT_ITEM]}, "total": 1})
    if method == "user.get":
        return httpx.Response(200, json={"result": [{"ID": "239", "NAME": "Асылбек", "LAST_NAME": "Кожахметов"},
                                                    {"ID": "45", "NAME": "Раушан", "LAST_NAME": "Баясилова"}]})
    if method == "im.recent.list":
        return httpx.Response(200, json={"result": {"items": [{"id": "chat27", "type": "chat", "title": "Общий чат TSG"},
                                                             {"id": "45", "type": "user", "title": "Раушан"}]}})
    if method == "task.commentitem.add":
        return httpx.Response(200, json={"result": 555})
    if method == "im.message.add":
        return httpx.Response(200, json={"result": 777})
    if method == "tasks.task.add":
        return httpx.Response(200, json={"result": {"task": {"id": "901"}}})
    if method == "disk.storage.getlist":
        return httpx.Response(200, json={"result": [{"ID": "1", "ROOT_OBJECT_ID": "10"}]})
    if method == "disk.storage.getchildren":
        return httpx.Response(200, json={"result": [
            {"ID": "11", "NAME": "02 Объекты", "TYPE": "folder"},
            {"ID": "12", "NAME": "06 Финансы", "TYPE": "folder"},
            {"ID": "13", "NAME": "01 Директор", "TYPE": "folder"}]})
    if method == "disk.folder.get":
        fid = str(body.get("id"))
        names = {"11": "02 Объекты", "12": "06 Финансы", "21": "OP-14"}
        parent = "11" if fid == "21" else "10"
        return httpx.Response(200, json={"result": {"ID": fid, "NAME": names.get(fid, "?"), "STORAGE_ID": "1", "PARENT_ID": parent}})
    if method == "disk.folder.getchildren":
        fid = str(body.get("id"))
        kids = {"11": [{"ID": "21", "NAME": "OP-14", "TYPE": "folder"}],
                "21": [{"ID": "31", "NAME": "График OP-14.xlsx", "TYPE": "file", "DETAIL_URL": "https://x/disk/31"}],
                "12": [{"ID": "41", "NAME": "ФОТ октябрь.xlsx", "TYPE": "file"}]}
        return httpx.Response(200, json={"result": kids.get(fid, [])})
    return httpx.Response(200, json={"error": "UNKNOWN", "error_description": method})


def service() -> DataService:
    return DataService(BitrixClient("https://example.bitrix24.kz/rest/1/x/", transport=httpx.MockTransport(fake_bitrix)))


def test_classification():
    assert classify_field("ufCrm20Salary", "ЗП на руки за 1 чел.") == "money"
    assert classify_field("ufCrm20Official", "Оформление (ОФ/НЕ ОФ)") == "money"
    assert classify_field("ufCrm20Iin", "ИИН сотрудника") == "pii"
    assert classify_field("ufCrm20Position", "Должность") == "plain"
    assert classify_field("x", "Профессиональная уборка") == "plain"


def test_production_does_not_see_money():
    rows = asyncio.run(service().list_items("production", "shifts", None))["записи"]
    assert rows[0]["Должность"] == "ОПУ (ночь)"
    assert rows[0]["Стадия"] == "Действует"
    assert not any(k in rows[0] for k in ("ЗП на руки за 1 чел.", "ФОТ по строке", "Оформление (ОФ/НЕ ОФ)"))
    assert "ИИН сотрудника" not in rows[0]


def test_accountant_sees_money_but_not_iin():
    row = asyncio.run(service().list_items("accountant", "shifts", None))["записи"][0]
    assert row["ЗП на руки за 1 чел."] == 135000
    assert row["Оформление (ОФ/НЕ ОФ)"] == "НЕ ОФ"
    assert "ИИН сотрудника" not in row


def test_filter_on_hidden_field_is_refused():
    with pytest.raises(AccessDenied):
        asyncio.run(service().list_items("production", "shifts", {">ufCrm20Salary": 100000}))


def test_section_outside_role_is_refused():
    with pytest.raises(AccessDenied):
        asyncio.run(service().list_items("production", "contracts", None))
    with pytest.raises(AccessDenied):
        asyncio.run(service().tasks("accountant", None, False, None, 10))


def test_write_methods_never_leave_the_client():
    CALLS.clear()
    client = BitrixClient("https://example.bitrix24.kz/rest/1/x/", transport=httpx.MockTransport(fake_bitrix))
    for method in ("tasks.task.add", "crm.item.update", "im.message.add", "crm.item.delete"):
        with pytest.raises(MethodNotAllowed):
            asyncio.run(client.call(method, {}))
    assert CALLS == []


def test_tool_sets_per_role():
    svc = service()
    names = {r: {t.name for t in asyncio.run(build_role_server(r, svc).list_tools())}
             for r in ("teamlead", "production", "accountant")}
    assert "list_tasks" in names["production"]
    assert "list_tasks" not in names["accountant"]
    assert "list_section" in names["teamlead"]


TOKENS = {"teamlead": "t-lead", "production": "t-prod", "account": "t-acc", "accountant": "t-buh"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_http_auth_per_role():
    client = BitrixClient("https://example.bitrix24.kz/rest/1/x/", transport=httpx.MockTransport(fake_bitrix))
    app = create_app(client=client, roles=tuple(TOKENS), tokens=TOKENS)
    with TestClient(app) as http:
        assert http.get("/health").status_code == 200
        assert http.post("/production/mcp", json=INIT, headers=HEADERS).status_code == 401
        # токен бухгалтера не открывает адрес производства
        assert http.post("/production/mcp", json=INIT,
                         headers={**HEADERS, "authorization": "Bearer t-buh"}).status_code == 401
        ok = http.post("/production/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer t-prod"})
        assert ok.status_code == 200, ok.text
        assert "tsg-bitrix-production" in ok.text


def test_tool_call_over_http_hides_money():
    client = BitrixClient("https://example.bitrix24.kz/rest/1/x/", transport=httpx.MockTransport(fake_bitrix))
    app = create_app(client=client, roles=tuple(TOKENS), tokens=TOKENS)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_section", "arguments": {"entity": "shifts"}}}
    with TestClient(app) as http:
        prod = http.post("/production/mcp", json=call, headers={**HEADERS, "authorization": "Bearer t-prod"})
        buh = http.post("/accountant/mcp", json=call, headers={**HEADERS, "authorization": "Bearer t-buh"})
    assert prod.status_code == 200, prod.text
    assert "ОПУ (ночь)" in prod.text and "135000" not in prod.text
    assert "135000" in buh.text



def test_only_teamlead_writes_and_only_to_real_people():
    svc = service()
    CALLS.clear()
    res = asyncio.run(svc.send_message("teamlead", "45", "Раушан, проверьте OP-14"))
    assert res["куда"] == "Раушан Баясилова"
    assert ("im.message.add", {"DIALOG_ID": "45", "MESSAGE": "Раушан, проверьте OP-14"}) in CALLS
    with pytest.raises(AccessDenied):
        asyncio.run(svc.send_message("production", "45", "привет"))
    with pytest.raises(AccessDenied):
        asyncio.run(svc.send_message("teamlead", "99999", "кому-то"))
    task = asyncio.run(svc.create_task("teamlead", 45, "Проверить OP-14", "Невыходы", "2026-10-17T18:00:00+05:00"))
    assert task["id_задачи"] == "901" and task["ссылка"].endswith("/tasks/task/view/901/")


def test_disk_by_role():
    svc = service()
    top_prod = [r["название"] for r in asyncio.run(svc.disk_list("production", None))["записи"]]
    assert top_prod == ["02 Объекты"]  # ни «06 Финансы», ни «01 Директор»
    top_buh = [r["название"] for r in asyncio.run(svc.disk_list("accountant", None))["записи"]]
    assert top_buh == ["06 Финансы"]
    with pytest.raises(AccessDenied):
        asyncio.run(svc.disk_list("production", 12))  # папка финансов по прямому ID
    found = asyncio.run(svc.disk_search("production", "op-14"))
    assert {f["название"] for f in found} == {"OP-14", "График OP-14.xlsx"}


def test_write_tools_exist_only_for_teamlead():
    svc = service()
    lead = {t.name for t in asyncio.run(build_role_server("teamlead", svc).list_tools())}
    prod = {t.name for t in asyncio.run(build_role_server("production", svc).list_tools())}
    assert {"send_message", "create_task", "disk_list"} <= lead
    assert "send_message" not in prod and "disk_search" in prod



def test_group_chat_and_task_comment():
    svc = service()
    CALLS.clear()
    res = asyncio.run(svc.send_message("teamlead", "chat27", "Коллеги, сводка готова"))
    assert res["куда"] == "чат «Общий чат TSG»"
    with pytest.raises(AccessDenied):
        asyncio.run(svc.send_message("teamlead", "chat999", "в чужой чат"))
    out = asyncio.run(svc.comment_task("teamlead", 4765, "Прошу принять в работу"))
    assert out["задача"] == 4765
    assert ("task.commentitem.add", {"TASKID": 4765, "FIELDS": {"POST_MESSAGE": "Прошу принять в работу"}}) in CALLS

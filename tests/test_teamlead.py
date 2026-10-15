"""Разбор потока событий сессии на подставном клиенте Anthropic."""
from types import SimpleNamespace as NS

from owner_bot.teamlead import Config, Teamlead


class FakeStream:
    def __init__(self, events): self.events = events
    def __enter__(self): return iter(self.events)
    def __exit__(self, *a): return False


def fake_client(events, created):
    def create(**kw):
        created.append(kw)
        return NS(id=f"sesn_{len(created)}")
    sent = []
    events_api = NS(stream=lambda sid: FakeStream(events), send=lambda sid, events: sent.append((sid, events)))
    return NS(beta=NS(sessions=NS(create=create, events=events_api))), sent


def make(events, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "s.json"))
    created = []
    client, sent = fake_client(events, created)
    return Teamlead(Config("agent_1", "env_1", "vlt_1", "150"), client), created, sent


def test_answer_progress_and_session_reuse(tmp_path, monkeypatch):
    events = [
        NS(type="session.thread_created", agent_name="TSG · Производство"),
        NS(type="agent.message", content=[NS(type="text", text="Невыходов: 2 (Алматы)")]),
        NS(type="session.status_idle", stop_reason=NS(type="end_turn")),
        NS(type="agent.message", content=[NS(type="text", text="не должно попасть")]),
    ]
    lead, created, sent = make(events, tmp_path, monkeypatch)
    steps = []
    assert lead.ask("chat1", "невыходы?", steps.append) == "Невыходов: 2 (Алматы)"
    assert steps == ["подключён агент: Производство"]
    lead.ask("chat1", "а по Астане?")
    assert len(created) == 1  # та же сессия в течение дня
    assert created[0]["budget"]["max_list_cost"]["amount"] == "150"
    assert "Сейчас " in sent[0][1][0]["content"][0]["text"]
    lead.reset("chat1")
    lead.ask("chat1", "новая тема")
    assert len(created) == 2


def test_budget_reached(tmp_path, monkeypatch):
    lead, created, _ = make([NS(type="session.status_idle", stop_reason=NS(type="budget_reached"))], tmp_path, monkeypatch)
    assert "лимит расходов" in lead.ask("c", "сводка")


def test_confirmation_flow(tmp_path, monkeypatch):
    events = [
        NS(type="agent.mcp_tool_use", id="e1", name="send_message", input={"user_id": 45, "text": "Проверьте OP-14"}),
        NS(type="session.status_idle", stop_reason=NS(type="requires_action", event_ids=["e1"])),
        NS(type="agent.message", content=[NS(type="text", text="Сообщение отправлено")]),
        NS(type="session.status_idle", stop_reason=NS(type="end_turn")),
    ]
    lead, _, sent = make(events, tmp_path, monkeypatch)
    seen = []
    out = lead.ask("c", "напиши Раушан", confirm=lambda a: seen.append(a) or True)
    assert seen == [{"tool": "send_message", "input": {"user_id": 45, "text": "Проверьте OP-14"}}]
    assert sent[-1][1] == [{"type": "user.tool_confirmation", "tool_use_id": "e1", "result": "allow"}]
    assert out == "Сообщение отправлено"
    lead2, _, sent2 = make(events, tmp_path, monkeypatch)
    lead2.ask("c2", "напиши", confirm=None)
    assert sent2[-1][1][0]["result"] == "deny"  # без кнопки — отказ

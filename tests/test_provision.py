"""Автонастройка на подставном API Anthropic: второй запуск ничего не плодит, токены сходятся с сервером."""
import itertools
import json

import anthropic
import httpx2

from tsg_bitrix import provision as pv

BASE = "https://tsg-bitrix-production.up.railway.app"


class FakeAPI:
    def __init__(self):
        self.ids = itertools.count(1)
        self.store = {"environments": {}, "vaults": {}, "agents": {}, "credentials": {}}
        self.calls: list[tuple[str, str]] = []

    def obj(self, kind, body, oid=None):
        oid = oid or f"{kind[:3]}_{next(self.ids)}"
        rec = self.store[kind].setdefault(oid, {"id": oid, "archived_at": None, "version": 0,
                                                "created_at": "2026-10-15T00:00:00Z",
                                                "updated_at": "2026-10-15T00:00:00Z"})
        rec.update({k: v for k, v in body.items() if v is not None})
        rec["version"] += 1
        rec.setdefault("metadata", {})
        return rec

    def __call__(self, request):
        path = request.url.path.removeprefix("/v1/")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path))
        parts = path.split("/")
        if parts[0] == "vaults" and len(parts) >= 3 and parts[2] == "credentials":
            kind, oid = "credentials", (parts[3] if len(parts) > 3 else None)
        else:
            kind, oid = parts[0], (parts[1] if len(parts) > 1 else None)
        if request.method == "GET":
            return httpx2.Response(200, json={"data": list(self.store[kind].values()), "next_page": None})
        rec = self.obj(kind, body, oid)
        if kind == "credentials":
            rec.setdefault("type", "vault_credential")
            rec.setdefault("vault_id", parts[1])
        return httpx2.Response(200, json=rec)


def client(api):
    return anthropic.Anthropic(api_key="sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(api)))


def test_provision_is_idempotent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    api = FakeAPI()
    first = pv.provision(client(api), BASE, log=lambda m: None)
    created = [c for c in api.calls if c[0] == "POST" and c[1] in ("environments", "vaults", "agents")]
    assert len(created) == 6  # среда, хранилище, 4 агента
    second = pv.provision(client(api), BASE, log=lambda m: None)
    assert first == second
    assert len(api.store["agents"]) == 4 and len(api.store["credentials"]) == 4
    lead = api.store["agents"][first["agents"]["teamlead"]]
    assert lead["multiagent"]["subagents"]["inline_agents"] == {"type": "disabled"}
    assert {a["id"] for a in lead["multiagent"]["subagents"]["predefined_agents"]} == {
        first["agents"][r] for r in pv.SPECIALISTS}
    assert pv.find_ids(client(api)) == first


def test_tokens_derived_from_key_match_server(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("MCP_TOKEN_PRODUCTION", raising=False)
    api = FakeAPI()
    pv.provision(client(api), BASE, log=lambda m: None)
    cred = next(c for c in api.store["credentials"].values() if c["metadata"]["role"] == "production")
    assert cred["auth"]["token"] == pv.role_token("production")
    assert cred["auth"]["mcp_server_url"] == f"{BASE}/production/mcp"
    assert pv.role_token("production") != pv.role_token("accountant")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-other")
    assert cred["auth"]["token"] != pv.role_token("production")

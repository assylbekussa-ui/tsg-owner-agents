"""Создаёт или обновляет команду агентов TSG в Claude Platform (Managed Agents).

Запуск:  python scripts/setup_agents.py
Нужны переменные окружения: ANTHROPIC_API_KEY, MCP_BASE_URL (https://<сервис>.up.railway.app),
MCP_TOKEN_TEAMLEAD, MCP_TOKEN_PRODUCTION, MCP_TOKEN_ACCOUNT, MCP_TOKEN_ACCOUNTANT.

Создаёт: среду (environment) с доступом только к MCP-серверу, хранилище токенов (vault),
три агента-специалиста и Тимлида, который может делегировать только им.
ID созданного сохраняются в state.json — повторный запуск обновляет агентов, а не плодит копии.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import anthropic

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
STATE = Path(os.getenv("STATE_PATH", ROOT / "state.json"))
SPECIALISTS = ("production", "account", "accountant")


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def read_prompt(key: str) -> tuple[dict, str]:
    raw = (AGENTS_DIR / f"{key}.md").read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    common = (AGENTS_DIR / "_common.md").read_text(encoding="utf-8")
    return meta, body.strip() + "\n\n" + common


def mcp_url(base: str, role: str) -> str:
    return f"{base.rstrip('/')}/{role}/mcp"


def agent_payload(key: str, base: str) -> dict:
    meta, system = read_prompt(key)
    tools: list[dict] = [{
        "type": "mcp_toolset",
        "mcp_server_name": "bitrix",
        # сервер только на чтение, поэтому подтверждение каждого вызова не нужно
        "default_config": {"permission_policy": {"type": "always_allow"}},
    }]
    if meta.get("toolset") == "true":  # бухгалтеру нужен код для расчётов, без интернета
        tools.append({
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False},
            "configs": [
                {"name": "bash", "enabled": True, "permission_policy": {"type": "always_allow"}},
                {"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}},
                {"name": "write", "enabled": True, "permission_policy": {"type": "always_allow"}},
            ],
        })
    return {
        "name": meta["name"],
        "model": meta["model"],
        "description": meta.get("description"),
        "system": system,
        "mcp_servers": [{"type": "url", "name": "bitrix", "url": mcp_url(base, key)}],
        "tools": tools,
        "metadata": {"project": "tsg-owner-agents", "role": key},
    }


def upsert_agent(client: anthropic.Anthropic, state: dict, key: str, payload: dict) -> str:
    agents = state.setdefault("agents", {})
    if key in agents:
        agent = client.beta.agents.update(agents[key], **payload)
        print(f"  обновлён {payload['name']}: версия {agent.version}")
    else:
        agent = client.beta.agents.create(**payload)
        agents[key] = agent.id
        print(f"  создан {payload['name']}: {agent.id}")
    save_state(state)
    return agents[key]


def main() -> None:
    base = os.environ.get("MCP_BASE_URL", "").rstrip("/")
    if not base.startswith("https://"):
        sys.exit("MCP_BASE_URL должен быть https-адресом сервера tsg-bitrix на Railway")
    roles = ("teamlead", *SPECIALISTS)
    tokens = {r: os.environ.get(f"MCP_TOKEN_{r.upper()}", "") for r in roles}
    if not all(tokens.values()):
        sys.exit("Не заданы все MCP_TOKEN_* (teamlead, production, account, accountant)")

    client = anthropic.Anthropic()
    state = load_state()

    print("Среда (environment)…")
    host = urlparse(base).hostname
    if "environment_id" not in state:
        env = client.beta.environments.create(
            name="tsg-owner-agents",
            config={"type": "cloud", "networking": {"type": "limited", "allowed_hosts": [host],
                                                    "allow_mcp_servers": True}},
        )
        state["environment_id"] = env.id
        save_state(state)
        print(f"  создана {env.id} (сеть: только {host})")
    else:
        print(f"  уже есть {state['environment_id']}")

    print("Хранилище токенов (vault)…")
    if "vault_id" not in state:
        vault = client.beta.vaults.create(display_name="TSG Bitrix MCP", metadata={"project": "tsg-owner-agents"})
        state["vault_id"] = vault.id
        state["credentials"] = {}
        save_state(state)
        print(f"  создано {vault.id}")
    creds = state.setdefault("credentials", {})
    for role in roles:
        if role in creds:
            client.beta.vaults.credentials.update(
                creds[role], vault_id=state["vault_id"], auth={"type": "static_bearer", "token": tokens[role]})
            continue
        cred = client.beta.vaults.credentials.create(
            state["vault_id"], display_name=f"tsg-bitrix {role}",
            auth={"type": "static_bearer", "mcp_server_url": mcp_url(base, role), "token": tokens[role]},
        )
        creds[role] = cred.id
        save_state(state)
    print(f"  токенов в хранилище: {len(creds)}")

    print("Агенты-специалисты…")
    ids = [upsert_agent(client, state, k, agent_payload(k, base)) for k in SPECIALISTS]

    print("Тимлид…")
    lead = agent_payload("teamlead", base)
    lead["multiagent"] = {
        "type": "multiagent_20261001",
        "subagents": {
            "type": "enabled",
            "inline_agents": {"type": "disabled"},  # только перечисленные агенты, без самодельных
            "predefined_agents": [{"type": "agent", "id": i} for i in ids],
        },
        "workflows": {"type": "disabled"},
    }
    upsert_agent(client, state, "teamlead", lead)
    print("\nГотово. ID сохранены в", STATE)
    print("\nДля сервиса Telegram-бота на Railway (Variables):")
    print(f"TEAMLEAD_AGENT_ID={state['agents']['teamlead']}")
    print(f"AGENTS_ENVIRONMENT_ID={state['environment_id']}")
    print(f"AGENTS_VAULT_ID={state['vault_id']}")


if __name__ == "__main__":
    main()

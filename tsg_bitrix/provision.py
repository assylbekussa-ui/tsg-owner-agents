"""Автонастройка команды агентов в Claude Platform (Managed Agents).

Идемпотентно: находит уже созданные среду, хранилище токенов и агентов по имени и
метаданным и обновляет их, а не создаёт копии. Поэтому её можно запускать при каждом
старте сервиса — так новые промпты из репозитория сами попадают к агентам.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import anthropic

PROJECT = "tsg-owner-agents"
ENV_NAME = "tsg-owner-agents"
VAULT_NAME = "TSG Bitrix MCP"
SPECIALISTS = ("production", "account", "accountant")
ROLES = ("teamlead", *SPECIALISTS)
AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def role_token(role: str) -> str:
    """Токен MCP для роли: из MCP_TOKEN_<РОЛЬ> или выводится из ключа Anthropic (HMAC).

    Из токена ключ не восстановить. Сменили ключ — при следующем старте сменятся и токены,
    и автонастройка сама обновит их в хранилище.
    """
    explicit = os.getenv(f"MCP_TOKEN_{role.upper()}")
    if explicit:
        return explicit
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    return hmac.new(key.encode(), f"tsg-bitrix-mcp:{role}".encode(), hashlib.sha256).hexdigest()


def base_url() -> str:
    """Адрес MCP-сервера: MCP_BASE_URL или публичный домен Railway этого сервиса."""
    if os.getenv("MCP_BASE_URL"):
        return os.environ["MCP_BASE_URL"].rstrip("/")
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    return f"https://{domain}" if domain else ""


def mcp_url(base: str, role: str) -> str:
    return f"{base.rstrip('/')}/{role}/mcp"


def read_prompt(key: str) -> tuple[dict, str]:
    raw = (AGENTS_DIR / f"{key}.md").read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    common = (AGENTS_DIR / "_common.md").read_text(encoding="utf-8")
    return meta, body.strip() + "\n\n" + common


def agent_payload(key: str, base: str) -> dict:
    meta, system = read_prompt(key)
    mcp_tools: dict = {
        "type": "mcp_toolset",
        "mcp_server_name": "bitrix",
        "default_config": {"permission_policy": {"type": "always_allow"}},  # чтение — без подтверждения
    }
    if key == "teamlead":
        # Запись в Битрикс — только после кнопки владельца в Telegram
        mcp_tools["configs"] = [{"name": n, "permission_policy": {"type": "always_ask"}}
                                for n in ("send_message", "create_task")]
    tools: list[dict] = [mcp_tools]
    if meta.get("toolset") == "true":  # бухгалтеру нужен код для расчётов, без интернета
        tools.append({
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False},
            "configs": [{"name": n, "enabled": True, "permission_policy": {"type": "always_allow"}}
                        for n in ("bash", "read", "write")],
        })
    return {
        "name": meta["name"],
        "model": meta["model"],
        "description": meta.get("description"),
        "system": system,
        "mcp_servers": [{"type": "url", "name": "bitrix", "url": mcp_url(base, key)}],
        "tools": tools,
        "metadata": {"project": PROJECT, "role": key},
    }


def _alive(obj) -> bool:
    return getattr(obj, "archived_at", None) is None


def find_ids(client: anthropic.Anthropic) -> dict:
    """ID среды, хранилища и агентов, если они уже созданы."""
    out: dict = {"agents": {}}
    for env in client.beta.environments.list():
        if env.name == ENV_NAME and _alive(env):
            out["environment_id"] = env.id
            break
    for vault in client.beta.vaults.list():
        if vault.display_name == VAULT_NAME and _alive(vault):
            out["vault_id"] = vault.id
            break
    for agent in client.beta.agents.list():
        meta = agent.metadata or {}
        if meta.get("project") == PROJECT and _alive(agent) and meta.get("role") in ROLES:
            out["agents"].setdefault(meta["role"], agent.id)
    return out


def provision(client: anthropic.Anthropic, base: str, log: Callable[[str], None] = print) -> dict:
    if not base.startswith("https://"):
        raise RuntimeError("Нет публичного адреса MCP-сервера: в Railway → Settings → Networking → Generate Domain")
    tokens = {r: role_token(r) for r in ROLES}
    if not all(tokens.values()):
        raise RuntimeError("Нет ANTHROPIC_API_KEY или MCP_TOKEN_*: токены ролей не из чего получить")
    ids = find_ids(client)
    host = urlparse(base).hostname
    networking = {"type": "limited", "allowed_hosts": [host], "allow_mcp_servers": True}

    if "environment_id" in ids:
        client.beta.environments.update(ids["environment_id"], config={"type": "cloud", "networking": networking})
        log(f"среда: обновлена (сеть только {host})")
    else:
        ids["environment_id"] = client.beta.environments.create(
            name=ENV_NAME, config={"type": "cloud", "networking": networking}).id
        log(f"среда: создана {ids['environment_id']}")

    if "vault_id" not in ids:
        ids["vault_id"] = client.beta.vaults.create(display_name=VAULT_NAME, metadata={"project": PROJECT}).id
        log(f"хранилище токенов: создано {ids['vault_id']}")
    existing = {}
    for cred in client.beta.vaults.credentials.list(ids["vault_id"]):
        role = (cred.metadata or {}).get("role")
        if role and _alive(cred) and (cred.metadata or {}).get("url") == mcp_url(base, role):
            existing[role] = cred.id
    for role in ROLES:
        if role in existing:
            client.beta.vaults.credentials.update(
                existing[role], vault_id=ids["vault_id"], auth={"type": "static_bearer", "token": tokens[role]})
        else:
            client.beta.vaults.credentials.create(
                ids["vault_id"], display_name=f"tsg-bitrix {role}",
                metadata={"project": PROJECT, "role": role, "url": mcp_url(base, role)},
                auth={"type": "static_bearer", "mcp_server_url": mcp_url(base, role), "token": tokens[role]},
            )
    log(f"хранилище токенов: {len(ROLES)} токена ролей актуальны")

    def upsert(role: str, payload: dict) -> str:
        if role in ids["agents"]:
            agent = client.beta.agents.update(ids["agents"][role], **payload)
            log(f"агент {payload['name']}: версия {agent.version}")
        else:
            ids["agents"][role] = client.beta.agents.create(**payload).id
            log(f"агент {payload['name']}: создан {ids['agents'][role]}")
        return ids["agents"][role]

    specialist_ids = [upsert(r, agent_payload(r, base)) for r in SPECIALISTS]
    lead = agent_payload("teamlead", base)
    lead["multiagent"] = {
        "type": "multiagent_20261001",
        "subagents": {
            "type": "enabled",
            "inline_agents": {"type": "disabled"},  # только перечисленные агенты
            "predefined_agents": [{"type": "agent", "id": i} for i in specialist_ids],
        },
        "workflows": {"type": "disabled"},
    }
    upsert("teamlead", lead)
    return ids

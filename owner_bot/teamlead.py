"""Разговор с Тимлидом через Managed Agents.

Одна сессия на чат и на день: Тимлид помнит разговор в течение дня
(«а по Астане?»), а утром начинает с чистого листа — контекст не растёт
бесконечно, и расходы предсказуемы. «Новая тема» сбрасывает сессию раньше.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import anthropic

TZ = ZoneInfo("Asia/Almaty")
ROOT = Path(__file__).resolve().parent.parent

AGENT_NAMES = {"production": "Производство", "account": "Аккаунт", "accountant": "Бухгалтер"}

SUMMARY_PROMPT = (
    "Сделай вечернюю сводку за сегодня для собственника. "
    "Производство: выходы, невыходы без замены, аудиты и жалобы за сегодня, Алматы и Астана отдельно. "
    "Аккаунт: новые и повторные жалобы, NPS ниже 7, договоры с проблемами. "
    "Бухгалтер: ФОТ текущего месяца по компании, ОФ / НЕ ОФ, объекты с расхождением ФОТ и сметы. "
    "Сверь ответы между собой. Формат: цифры, затем «Требует решения» (не больше 5 пунктов), затем «Нет данных»."
)
HOT_PROMPT = (
    "Что сейчас горит? Открытые и повторные жалобы, невыходы без замены, просроченные задачи, "
    "объекты с расхождением ФОТ и сметы. Только то, что требует решения сегодня, не больше 5 пунктов."
)


@dataclass
class Config:
    teamlead_id: str
    environment_id: str
    vault_id: str
    budget_cents: str = "200"

    @classmethod
    def load(cls, client: anthropic.Anthropic | None = None) -> "Config":
        """Сначала переменные окружения (Railway), иначе state.json от setup_agents.py."""
        state_path = Path(os.getenv("STATE_PATH", ROOT / "state.json"))
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        cfg = cls(
            teamlead_id=os.getenv("TEAMLEAD_AGENT_ID") or state.get("agents", {}).get("teamlead", ""),
            environment_id=os.getenv("AGENTS_ENVIRONMENT_ID") or state.get("environment_id", ""),
            vault_id=os.getenv("AGENTS_VAULT_ID") or state.get("vault_id", ""),
            budget_cents=os.getenv("SESSION_BUDGET_CENTS", "200"),
        )
        if not (cfg.teamlead_id and cfg.environment_id and cfg.vault_id):
            # ID не заданы — находим агентов, которых создал сервис tsg-bitrix при старте
            from tsg_bitrix.provision import find_ids

            found = find_ids(client or anthropic.Anthropic())
            cfg.teamlead_id = cfg.teamlead_id or found["agents"].get("teamlead", "")
            cfg.environment_id = cfg.environment_id or found.get("environment_id", "")
            cfg.vault_id = cfg.vault_id or found.get("vault_id", "")
        if not (cfg.teamlead_id and cfg.environment_id and cfg.vault_id):
            raise RuntimeError("Агенты ещё не созданы: дождитесь старта сервиса tsg-bitrix "
                               "(нужны ANTHROPIC_API_KEY и домен) и перезапустите бота")
        return cfg


class Teamlead:
    def __init__(self, cfg: Config | None = None, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()
        self.cfg = cfg or Config.load(self.client)
        # ключ чата -> [дата, id сессии]; хранится в файле, чтобы переживать перезапуск
        self._path = Path(os.getenv("SESSIONS_PATH", ROOT / "sessions.json"))
        try:
            self._sessions: dict[str, list[str]] = json.loads(self._path.read_text())
        except (OSError, ValueError):
            self._sessions = {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._sessions))
        except OSError:
            pass  # без файла просто начнём новую сессию после перезапуска

    def reset(self, chat_key: str) -> None:
        self._sessions.pop(chat_key, None)
        self._save()

    def _session(self, chat_key: str) -> str:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        cached = self._sessions.get(chat_key)
        if cached and cached[0] == today:
            return cached[1]
        session = self.client.beta.sessions.create(
            agent=self.cfg.teamlead_id,
            environment_id=self.cfg.environment_id,
            vault_ids=[self.cfg.vault_id],
            title=f"Собственник · {chat_key} · {datetime.now(TZ):%d.%m %H:%M}",
            budget={"type": "limit", "max_list_cost": {"amount": self.cfg.budget_cents, "currency": "USD"}},
            metadata={"chat": chat_key},
        )
        self._sessions[chat_key] = [today, session.id]
        self._save()
        return session.id

    def ask(self, chat_key: str, text: str, on_progress: Callable[[str], None] | None = None) -> str:
        """Отправляет вопрос Тимлиду и ждёт конца его хода. Блокирующий вызов."""
        progress = on_progress or (lambda _msg: None)
        session_id = self._session(chat_key)
        stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (%A), Алматы")
        answer: list[str] = []
        with self.client.beta.sessions.events.stream(session_id) as stream:
            self.client.beta.sessions.events.send(session_id, events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": f"Сейчас {stamp}.\n\n{text}"}],
            }])
            for event in stream:
                kind = getattr(event, "type", "")
                if kind == "agent.message":
                    answer.extend(b.text for b in event.content if getattr(b, "type", "") == "text")
                elif kind == "session.thread_created":
                    name = str(getattr(event, "agent_name", "") or "")
                    label = next((v for k, v in AGENT_NAMES.items() if k in name.lower() or v in name), name)
                    if label:
                        progress(f"подключён агент: {label}")
                elif kind == "session.error":
                    err = getattr(event, "error", None)
                    progress(f"ошибка: {getattr(err, 'message', err)}")
                elif kind == "session.status_idle":
                    reason = getattr(getattr(event, "stop_reason", None), "type", "")
                    if reason == "end_turn":
                        break
                    if reason == "budget_reached":
                        self.reset(chat_key)
                        answer.append("\n[Исчерпан лимит расходов этой сессии. Следующий вопрос начнёт новую.]")
                        break
                    if reason == "requires_action":
                        answer.append("\n[Агент запросил подтверждение действия — на этом этапе действия выключены.]")
                        break
        return "\n".join(answer).strip() or "Тимлид не вернул текст ответа. Посмотрите сессию в Console → Сессии."

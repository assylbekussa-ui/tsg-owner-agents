"""Telegram-бот собственника: голос или текст → Тимлид → ответ.

Запуск (отдельный сервис Railway):  python -m owner_bot.bot
Переменные: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS (через запятую), ANTHROPIC_API_KEY,
TEAMLEAD_AGENT_ID, AGENTS_ENVIRONMENT_ID, AGENTS_VAULT_ID, OPENAI_API_KEY или YANDEX_API_KEY.
Необязательно: SUMMARY_TIME=19:00 и SUMMARY_CHAT_IDS — вечерняя сводка сама приходит в чат.

Бот отвечает только людям из TELEGRAM_ALLOWED_IDS. Всем остальным он молчит.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx

from .speech import SpeechError, synthesize, transcribe
from .teamlead import HOT_PROMPT, SUMMARY_PROMPT, TZ, Teamlead

log = logging.getLogger("owner-bot")
LIMIT = 4000  # у Telegram предел 4096 символов на сообщение

BTN_SUMMARY = "Сводка за сегодня"
BTN_HOT = "Что горит"
BTN_NEW = "Новая тема"
KEYBOARD = {"keyboard": [[{"text": BTN_SUMMARY}, {"text": BTN_HOT}], [{"text": BTN_NEW}]],
            "resize_keyboard": True, "is_persistent": True}
HELP = (
    "Я Тимлид команды агентов TSG. Задайте вопрос голосом или текстом: "
    "«что горит по Атакенту?», «сколько невыходов сегодня в Астане?».\n\n"
    "Сейчас в команде: Производство, Аккаунт, Бухгалтер. Работаем только на чтение.\n"
    "/voice — включить или выключить ответ голосом."
)


def split_text(text: str, limit: int = LIMIT) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + ([text] if text else [])


def parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


class Telegram:
    def __init__(self, token: str, http: httpx.AsyncClient):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.files = f"https://api.telegram.org/file/bot{token}/"
        self.http = http

    async def call(self, method: str, **params: Any) -> Any:
        resp = await self.http.post(self.api + method, json=params, timeout=70)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    async def send(self, chat_id: int, text: str, **extra: Any) -> dict:
        return await self.call("sendMessage", chat_id=chat_id, text=text, **extra)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
        except RuntimeError:
            pass  # «сообщение не изменилось» и подобное не важно

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        resp = await self.http.get(self.files + info["file_path"], timeout=60)
        resp.raise_for_status()
        return resp.content

    async def send_voice(self, chat_id: int, audio: bytes) -> None:
        resp = await self.http.post(self.api + "sendVoice", data={"chat_id": str(chat_id)},
                                    files={"voice": ("answer.ogg", audio, "audio/ogg")}, timeout=120)
        if not resp.json().get("ok"):
            raise RuntimeError(resp.text[:200])


class OwnerBot:
    def __init__(self, tg: Telegram, lead: Teamlead, http: httpx.AsyncClient, allowed: set[int],
                 ask_fn: Callable[[str, str, Callable[[str], None]], str] | None = None,
                 stt: Callable[[bytes], Awaitable[str]] | None = None):
        self.tg, self.lead, self.http, self.allowed = tg, lead, http, allowed
        self.ask_fn = ask_fn or lead.ask
        self.stt = stt or (lambda audio: transcribe(audio, http))
        self.voice_on: set[int] = set()
        self.locks: dict[int, asyncio.Lock] = {}

    def journal(self, user_id: int, kind: str, text: str) -> None:
        print(json.dumps({"ts": datetime.now(TZ).isoformat(timespec="seconds"), "user": user_id,
                          "kind": kind, "text": text[:500]}, ensure_ascii=False), flush=True)

    async def handle(self, update: dict) -> None:
        msg = update.get("message") or {}
        user_id = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        if not msg or user_id not in self.allowed:
            if user_id:
                self.journal(user_id, "отказ: не в белом списке", msg.get("text", ""))
            return
        text = (msg.get("text") or "").strip()

        if text in ("/start", "/help"):
            await self.tg.send(chat_id, HELP, reply_markup=KEYBOARD)
            return
        if text == "/voice":
            if chat_id in self.voice_on:
                self.voice_on.discard(chat_id)
                await self.tg.send(chat_id, "Ответ голосом выключен.")
            else:
                self.voice_on.add(chat_id)
                await self.tg.send(chat_id, "Ответ голосом включён: после текста пришлю голосовое.")
            return
        if text == BTN_NEW:
            self.lead.reset(str(chat_id))
            await self.tg.send(chat_id, "Начал новую тему: прошлый разговор Тимлид больше не учитывает.")
            return

        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            await self.tg.send(chat_id, "Ещё работаю над прошлым вопросом. Этот возьму сразу после.")
        async with lock:
            if msg.get("voice") or msg.get("audio"):
                status = await self.tg.send(chat_id, "Распознаю голосовое…")
                media = msg.get("voice") or msg.get("audio")
                try:
                    text = await self.stt(await self.tg.download(media["file_id"]))
                except (SpeechError, KeyError, httpx.HTTPError) as exc:
                    log.warning("STT: %s", exc)
                    await self.tg.edit(chat_id, status["message_id"], "Не смог распознать голосовое. Повторите или напишите текстом.")
                    return
                if not text:
                    await self.tg.edit(chat_id, status["message_id"], "В голосовом не нашёл слов. Повторите, пожалуйста.")
                    return
                await self.tg.edit(chat_id, status["message_id"], f"🎙 «{text}»\n\nПринял, собираю…")
            elif text:
                status = await self.tg.send(chat_id, "Принял, собираю…")
            else:
                return

            question = {BTN_SUMMARY: SUMMARY_PROMPT, BTN_HOT: HOT_PROMPT}.get(text, text)
            self.journal(user_id, "вопрос", text)
            await self.answer(chat_id, question, status)

    async def answer(self, chat_id: int, question: str, status: dict | None) -> None:
        loop = asyncio.get_running_loop()
        steps: list[str] = []
        base = status.get("text", "Собираю…") if status else ""

        def progress(line: str) -> None:  # вызывается из потока с запросом к Тимлиду
            steps.append(line)
            if status:
                text = base + "\n" + "\n".join(f"• {s}" for s in steps[-6:])
                asyncio.run_coroutine_threadsafe(self.tg.edit(chat_id, status["message_id"], text), loop)

        typing = asyncio.create_task(self._typing(chat_id))
        try:
            reply = await asyncio.to_thread(self.ask_fn, str(chat_id), question, progress)
        except Exception as exc:  # сбой API не должен ронять бота
            log.exception("Тимлид")
            reply = f"Не получилось получить ответ Тимлида: {exc}"
        finally:
            typing.cancel()
        self.journal(chat_id, "ответ", reply)
        for part in split_text(reply):
            await self.tg.send(chat_id, part)
        if chat_id in self.voice_on:
            try:
                await self.tg.send_voice(chat_id, await synthesize(reply, self.http))
            except Exception as exc:
                log.warning("TTS: %s", exc)
                await self.tg.send(chat_id, "Голосовую версию сделать не получилось.")

    async def _typing(self, chat_id: int) -> None:
        while True:
            try:
                await self.tg.call("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(5)

    async def scheduled_summary(self, at: str, chat_ids: set[int]) -> None:
        hour, minute = (int(x) for x in at.split(":"))
        while True:
            now = datetime.now(TZ)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            for chat_id in chat_ids:
                status = await self.tg.send(chat_id, "Вечерняя сводка: собираю…")
                await self.answer(chat_id, SUMMARY_PROMPT, status)

    async def run(self) -> None:
        await self.tg.call("deleteWebhook")
        offset = 0
        log.info("Бот запущен, разрешённых пользователей: %d", len(self.allowed))
        while True:
            try:
                updates = await self.tg.call("getUpdates", offset=offset, timeout=50,
                                             allowed_updates=["message"])
            except Exception as exc:
                log.warning("getUpdates: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                asyncio.create_task(self._safe(update))

    async def _safe(self, update: dict) -> None:
        try:
            await self.handle(update)
        except Exception:
            log.exception("Ошибка обработки сообщения")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    allowed = parse_ids(os.environ.get("TELEGRAM_ALLOWED_IDS", ""))
    if not allowed:
        raise SystemExit("TELEGRAM_ALLOWED_IDS пуст: бот никому не будет отвечать")
    async with httpx.AsyncClient() as http:
        bot = OwnerBot(Telegram(os.environ["TELEGRAM_BOT_TOKEN"], http), Teamlead(), http, allowed)
        tasks = [bot.run()]
        if os.getenv("SUMMARY_TIME"):
            ids = parse_ids(os.getenv("SUMMARY_CHAT_IDS", "")) or allowed
            tasks.append(bot.scheduled_summary(os.environ["SUMMARY_TIME"], ids))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())

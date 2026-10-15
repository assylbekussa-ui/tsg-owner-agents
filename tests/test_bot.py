"""Бот на подставном Telegram: без сети, без Anthropic и без распознавания речи."""
import asyncio
import json

import httpx

from owner_bot.bot import BTN_HOT, BTN_NEW, OwnerBot, Telegram, split_text
from owner_bot.teamlead import HOT_PROMPT

OWNER = 239001
SENT: list[tuple[str, dict]] = []


def fake_telegram(request: httpx.Request) -> httpx.Response:
    method = request.url.path.rsplit("/", 1)[-1]
    if request.url.path.startswith("/file/"):
        return httpx.Response(200, content=b"OggS-fake-audio")
    body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else {}
    SENT.append((method, body))
    if method == "getFile":
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/1.oga"}})
    return httpx.Response(200, json={"ok": True, "result": {"message_id": len(SENT), "text": body.get("text", "")}})


class FakeLead:
    def __init__(self):
        self.asked: list[tuple[str, str]] = []
        self.resets: list[str] = []

    def ask(self, chat_key, text, progress):
        self.asked.append((chat_key, text))
        progress("подключён агент: Производство")
        return "ФОТ: 5 830 000 ₸\nТребует решения: OP-14"

    def reset(self, chat_key):
        self.resets.append(chat_key)


def make_bot(stt_text="что горит по Атакенту"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_telegram))
    lead = FakeLead()

    async def stt(audio: bytes) -> str:
        assert audio == b"OggS-fake-audio"
        return stt_text

    bot = OwnerBot(Telegram("TOKEN", http), lead, http, {OWNER}, ask_fn=lead.ask, stt=stt)
    return bot, lead


def msg(user, **kw):
    return {"update_id": 1, "message": {"message_id": 1, "from": {"id": user}, "chat": {"id": user}, **kw}}


def texts():
    return [b.get("text", "") for m, b in SENT if m in ("sendMessage", "editMessageText")]


def test_stranger_gets_silence():
    SENT.clear()
    bot, lead = make_bot()
    asyncio.run(bot.handle(msg(555, text="покажи ФОТ")))
    assert lead.asked == [] and SENT == []


def test_text_question_reaches_teamlead():
    SENT.clear()
    bot, lead = make_bot()
    asyncio.run(bot.handle(msg(OWNER, text="сколько невыходов сегодня?")))
    assert lead.asked == [(str(OWNER), "сколько невыходов сегодня?")]
    assert any("5 830 000" in t for t in texts())


def test_voice_is_transcribed_and_shown():
    SENT.clear()
    bot, lead = make_bot()
    asyncio.run(bot.handle(msg(OWNER, voice={"file_id": "abc", "duration": 4})))
    assert lead.asked[0][1] == "что горит по Атакенту"
    assert any("«что горит по Атакенту»" in t for t in texts())


def test_buttons():
    SENT.clear()
    bot, lead = make_bot()
    asyncio.run(bot.handle(msg(OWNER, text=BTN_HOT)))
    asyncio.run(bot.handle(msg(OWNER, text=BTN_NEW)))
    assert lead.asked[0][1] == HOT_PROMPT
    assert lead.resets == [str(OWNER)]


def test_long_answer_is_split():
    parts = split_text(("строка ответа\n" * 600).strip())
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)

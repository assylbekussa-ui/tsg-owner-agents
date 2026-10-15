"""Распознавание и синтез речи.

STT_PROVIDER=groq    — по умолчанию. Тот же сервис, что у HR-бота: Groq, Whisper large-v3
                       (STT_MODEL, можно whisper-large-v3-turbo — быстрее и дешевле). Нужен GROQ_API_KEY.
STT_PROVIDER=openai  — OpenAI (STT_MODEL, по умолчанию gpt-4o-transcribe). Нужен OPENAI_API_KEY.
STT_PROVIDER=yandex  — Yandex SpeechKit, синхронное распознавание (голосовые до 30 секунд).
                       Язык — STT_LANG (ru-RU или kk-KZ). Нужен YANDEX_API_KEY.
Ответ голосом (/voice) — OpenAI TTS, нужен OPENAI_API_KEY.
"""
from __future__ import annotations

import os

import httpx


class SpeechError(RuntimeError):
    pass


async def transcribe(audio: bytes, http: httpx.AsyncClient) -> str:
    provider = os.getenv("STT_PROVIDER", "groq").lower()
    if provider == "yandex":
        resp = await http.post(
            "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
            params={"lang": os.getenv("STT_LANG", "ru-RU"), "format": "oggopus"},
            headers={"Authorization": f"Api-Key {os.environ['YANDEX_API_KEY']}"},
            content=audio,
            timeout=60,
        )
        if resp.status_code != 200:
            raise SpeechError(f"SpeechKit: {resp.status_code} {resp.text[:200]}")
        return resp.json().get("result", "").strip()

    # Groq и OpenAI принимают одинаковый запрос: Whisper-совместимый эндпоинт
    if provider == "openai":
        url, key, model = ("https://api.openai.com/v1/audio/transcriptions",
                           os.environ["OPENAI_API_KEY"], os.getenv("STT_MODEL", "gpt-4o-transcribe"))
    else:
        url, key, model = ("https://api.groq.com/openai/v1/audio/transcriptions",
                           os.environ["GROQ_API_KEY"], os.getenv("STT_MODEL", "whisper-large-v3"))
    data = {"model": model,
            "prompt": "Клининговая компания TSG, объекты OP, ФОТ, ОПУ, Битрикс, Атакент, Алматы, Астана."}
    if os.getenv("STT_LANG_HINT"):  # ru или kk; без него модель определяет язык сама
        data["language"] = os.environ["STT_LANG_HINT"]
    resp = await http.post(url, headers={"Authorization": f"Bearer {key}"}, data=data,
                           files={"file": ("voice.ogg", audio, "audio/ogg")}, timeout=120)
    if resp.status_code != 200:
        raise SpeechError(f"{provider}: {resp.status_code} {resp.text[:200]}")
    return resp.json().get("text", "").strip()


async def synthesize(text: str, http: httpx.AsyncClient) -> bytes:
    """Голосовая версия ответа в формате OGG/Opus, который Telegram принимает как voice."""
    resp = await http.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": os.getenv("TTS_MODEL", "gpt-4o-mini-tts"), "voice": os.getenv("TTS_VOICE", "alloy"),
              "input": text[:4000], "response_format": "opus"},
        timeout=120,
    )
    if resp.status_code != 200:
        raise SpeechError(f"OpenAI TTS: {resp.status_code} {resp.text[:200]}")
    return resp.content

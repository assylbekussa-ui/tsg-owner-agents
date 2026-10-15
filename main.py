"""Точка входа для Railway: один код, два сервиса.

  APP=bot  — Telegram-бот собственника (owner_bot)
  иначе    — MCP-сервер tsg-bitrix; при старте он же создаёт или обновляет агентов
             в Claude Platform (если задан ANTHROPIC_API_KEY и у сервиса есть домен).
"""
import asyncio
import logging
import os
import threading

log = logging.getLogger("main")


def provision_in_background() -> None:
    if not os.getenv("ANTHROPIC_API_KEY") or os.getenv("SKIP_PROVISION"):
        log.warning("ANTHROPIC_API_KEY не задан — агенты не создаются, работает только MCP-сервер")
        return

    def run():
        try:
            import anthropic

            from tsg_bitrix.provision import base_url, provision

            provision(anthropic.Anthropic(), base_url(), log=lambda m: log.info("автонастройка: %s", m))
        except Exception as exc:  # сервер должен работать, даже если настройка не удалась
            log.error("автонастройка агентов не удалась: %s", exc)

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if os.getenv("APP", "").lower() == "bot":
        from owner_bot.bot import main as bot_main

        asyncio.run(bot_main())
    else:
        from tsg_bitrix.app import main as server_main

        provision_in_background()
        server_main()

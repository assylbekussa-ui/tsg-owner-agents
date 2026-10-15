"""Точка входа для Railway: один код, два сервиса.

Railway сам запускает `python main.py`. Какой сервис стартует, решает переменная APP:
  APP=bot  — Telegram-бот собственника (owner_bot)
  иначе    — MCP-сервер tsg-bitrix
"""
import asyncio
import os

if __name__ == "__main__":
    if os.getenv("APP", "").lower() == "bot":
        from owner_bot.bot import main as bot_main

        asyncio.run(bot_main())
    else:
        from tsg_bitrix.app import main as server_main

        server_main()

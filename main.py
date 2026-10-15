"""Точка входа для Railway: MCP-сервер и Telegram-бот в одном сервисе.

Порядок старта:
1. MCP-сервер tsg-bitrix начинает слушать порт (PORT).
2. Автонастройка создаёт или обновляет агентов в Claude Platform (нужен ANTHROPIC_API_KEY
   и публичный домен сервиса).
3. Если задан TELEGRAM_BOT_TOKEN — запускается бот собственника.

APP=server или APP=bot — запустить только одну часть (если потом разнесёте по двум сервисам).
"""
import asyncio
import logging
import os

log = logging.getLogger("main")


async def run_server() -> None:
    import uvicorn

    from tsg_bitrix.app import create_app

    config = uvicorn.Config(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), lifespan="on")
    await uvicorn.Server(config).serve()


async def run_provision() -> None:
    if not os.getenv("ANTHROPIC_API_KEY") or os.getenv("SKIP_PROVISION"):
        log.warning("ANTHROPIC_API_KEY не задан — агенты не создаются")
        return
    try:
        import anthropic

        from tsg_bitrix.provision import base_url, provision

        await asyncio.to_thread(provision, anthropic.Anthropic(), base_url(),
                                lambda m: log.info("автонастройка: %s", m))
    except Exception as exc:  # сервер должен работать, даже если настройка не удалась
        log.error("автонастройка агентов не удалась: %s", exc)


async def run_bot() -> None:
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        log.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен")
        return
    from owner_bot.bot import main as bot_main

    while True:  # бот не должен ронять сервер: при ошибке старта пробуем снова через минуту
        try:
            await bot_main()
        except (Exception, SystemExit) as exc:
            log.error("бот остановился: %s — перезапуск через 60 с", exc)
            await asyncio.sleep(60)


async def main() -> None:
    app = os.getenv("APP", "all").lower()
    if app == "bot":
        await run_bot()
        return
    server = asyncio.create_task(run_server())
    if app != "server":
        await run_provision()
        await asyncio.gather(server, run_bot())
    else:
        await asyncio.gather(server, run_provision())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

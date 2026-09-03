"""Single-process BetaAPI launcher for the HTTP API and Telegram bot."""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import uvicorn
from tgbot import run_bot


async def _run_api():
    config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
    await uvicorn.Server(config).serve()


async def main():
    api_task = asyncio.create_task(_run_api(), name="betaapi-api")
    bot_task = asyncio.create_task(run_bot(), name="betaapi-bot")
    done, pending = await asyncio.wait(
        {api_task, bot_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exception = task.exception()
        if exception:
            raise exception


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

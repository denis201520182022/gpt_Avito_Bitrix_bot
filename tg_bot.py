import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

import config  # используем твой config.py для токенов


# --- Логирование (файл + stdout) ---
log_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(log_dir, exist_ok=True)

file_handler = RotatingFileHandler(
    os.path.join(log_dir, "tg_bot.log"),
    maxBytes=5_000_000,   # ~5MB на файл
    backupCount=3,        # хранить 3 старых лога
    encoding="utf-8"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[file_handler, logging.StreamHandler()]
)


# --- Redis ---
async def get_redis():
    return aioredis.Redis(
        host=config.REDIS_HOST,
        port=int(config.REDIS_PORT),
        db=0,
        decode_responses=True
    )


# --- Telegram bot ---
bot = Bot(token=config.TG_BOT_TOKEN)
dp = Dispatcher()


# Команда /setlimit 100
@dp.message(Command("setlimit"))
async def set_limit(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("Используй: /setlimit <число>")
    limit = int(args[1])
    r = await get_redis()
    await r.set("chat_limit", limit)
    await r.set("chat_count", 0)  # сбросить счётчик
    logging.info(f"[TG BOT] Лимит обновлён: {limit}")
    await message.reply(f"✅ Лимит установлен: {limit} новых чатов")


# Команда /status
@dp.message(Command("status"))
async def status(message: types.Message):
    r = await get_redis()
    limit = await r.get("chat_limit") or 0
    count = await r.get("chat_count") or 0
    logging.info(f"[TG BOT] Проверка статуса: limit={limit}, count={count}")
    await message.reply(f"📊 Статус:\nЛимит: {limit}\nИспользовано: {count}")


# Команда /add 50 (добавить к лимиту)
@dp.message(Command("add"))
async def add_limit(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("Используй: /add <число>")
    add_value = int(args[1])
    r = await get_redis()
    limit = int(await r.get("chat_limit") or 0)
    new_limit = limit + add_value
    await r.set("chat_limit", new_limit)
    logging.info(f"[TG BOT] Лимит увеличен: +{add_value}, новый={new_limit}")
    await message.reply(f"➕ Лимит увеличен. Новый лимит: {new_limit}")


async def main():
    logging.info("[TG BOT] Запуск Telegram-бота")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

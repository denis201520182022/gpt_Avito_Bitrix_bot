import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

import config


# --- Логирование ---
log_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(log_dir, exist_ok=True)

file_handler = RotatingFileHandler(
    os.path.join(log_dir, "tg_bot.log"),
    maxBytes=5_000_000,
    backupCount=3,
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
dp = Dispatcher(storage=MemoryStorage())


# --- Клавиатура под сообщениями ---
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="⚙️ Установить лимит"), KeyboardButton(text="➕ Добавить к лимиту")],
        [KeyboardButton(text="ℹ️ Справка")]
    ],
    resize_keyboard=True
)


# --- Инлайн-клавиатура для выбора лимита ---
def quick_limit_keyboard(mode="set"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50", callback_data=f"{mode}_limit:50"),
                InlineKeyboardButton(text="100", callback_data=f"{mode}_limit:100"),
                InlineKeyboardButton(text="150", callback_data=f"{mode}_limit:150"),
            ]
        ]
    )


# --- FSM ---
class SetLimit(StatesGroup):
    waiting_for_number = State()


class AddLimit(StatesGroup):
    waiting_for_number = State()


# --- Статус ---
@dp.message(F.text.in_(["📊 Статус", "/status"]))
async def status(message: types.Message):
    r = await get_redis()
    limit = await r.get("chat_limit") or 0
    count = await r.get("chat_count") or 0
    await message.answer(f"📊 Статус:\nЛимит: {limit}\nИспользовано: {count}", reply_markup=main_kb)


# --- Установить лимит ---
@dp.message(F.text.in_(["⚙️ Установить лимит", "/setlimit"]))
async def ask_set_limit(message: types.Message, state: FSMContext):
    await message.answer("Введите новое значение лимита или выберите готовый вариант:", 
                         reply_markup=quick_limit_keyboard("set"))
    await state.set_state(SetLimit.waiting_for_number)


# --- Обработка числового ответа (установка лимита) ---
@dp.message(SetLimit.waiting_for_number, F.text.regexp(r"^\d+$"))
async def process_limit_input(message: types.Message, state: FSMContext):
    number = int(message.text)
    r = await get_redis()
    await r.set("chat_limit", number)
    await r.set("chat_count", 0)
    await message.answer(f"✅ Лимит установлен: {number}", reply_markup=main_kb)
    await state.clear()


# --- Добавить к лимиту ---
@dp.message(F.text.in_(["➕ Добавить к лимиту", "/add"]))
async def ask_add_limit(message: types.Message, state: FSMContext):
    await message.answer("Введите число для добавления или выберите готовый вариант:", 
                         reply_markup=quick_limit_keyboard("add"))
    await state.set_state(AddLimit.waiting_for_number)


@dp.message(AddLimit.waiting_for_number, F.text.regexp(r"^\d+$"))
async def process_add_limit(message: types.Message, state: FSMContext):
    number = int(message.text)
    r = await get_redis()
    current = int(await r.get("chat_limit") or 0)
    new_limit = current + number
    await r.set("chat_limit", new_limit)
    await message.answer(f"➕ Лимит увеличен: +{number}, новый={new_limit}", reply_markup=main_kb)
    await state.clear()


# --- Обработка инлайн-кнопок ---
@dp.callback_query(F.data.regexp(r"^(set|add)_limit:(\d+)$"))
async def inline_limit_handler(callback: types.CallbackQuery, state: FSMContext):
    mode, value = callback.data.split("_limit:")
    value = int(value)
    r = await get_redis()

    if mode == "set":
        await r.set("chat_limit", value)
        await r.set("chat_count", 0)
        await callback.message.answer(f"✅ Лимит установлен: {value}", reply_markup=main_kb)
        await state.clear()
    else:  # add
        current = int(await r.get("chat_limit") or 0)
        new_limit = current + value
        await r.set("chat_limit", new_limit)
        await callback.message.answer(f"➕ Лимит увеличен: +{value}, новый={new_limit}", reply_markup=main_kb)
        await state.clear()

    await callback.answer()  # убирает "часики"


# --- Справка ---
@dp.message(F.text.in_(["ℹ️ Справка", "/help"]))
async def help_cmd(message: types.Message):
    text = (
        "ℹ️ Доступные команды:\n"
        "/status – показать текущий лимит и использование\n"
        "/setlimit – установить новый лимит\n"
        "/add – добавить к лимиту\n"
        "/help – справка\n\n"
        "Также доступны кнопки под клавиатурой 📲"
    )
    await message.answer(text, reply_markup=main_kb)


# --- Настройка меню команд ---
async def set_commands():
    commands = [
        types.BotCommand(command="status", description="Показать статус"),
        types.BotCommand(command="setlimit", description="Установить новый лимит"),
        types.BotCommand(command="add", description="Добавить к лимиту"),
        types.BotCommand(command="help", description="Справка"),
    ]
    await bot.set_my_commands(commands)


async def main():
    logging.info("[TG BOT] Запуск Telegram-бота")
    await set_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

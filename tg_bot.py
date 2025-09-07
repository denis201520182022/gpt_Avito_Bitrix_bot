import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from functools import wraps
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

# --- Клавиатуры ---
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="⚙️ Установить лимит"), KeyboardButton(text="➕ Добавить к лимиту")],
        [KeyboardButton(text="ℹ️ Справка")]
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
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

async def monitor_limit():
    r = await get_redis()
    while True:
        try:
            limit = int(await r.get("chat_limit") or 0)
            count = int(await r.get("chat_count") or 0)
            remaining = max(0, limit - count) if limit > 0 else float('inf')
            
            warning_sent = await r.get("limit_warning_sent") == "1"

            if limit > 0 and remaining <= 15 and not warning_sent:
                # отправляем уведомления всем разрешённым пользователям
                for user_id in config.ALLOWED_USERS:
                    try:
                        await bot.send_message(user_id, f"⚠️ Осталось всего {remaining} лимитов из {limit}!")
                    except Exception as e:
                        logging.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
                await r.set("limit_warning_sent", "1")
            elif remaining > 15 and warning_sent:
                # сбрасываем флаг, когда лимит снова больше 15
                await r.set("limit_warning_sent", "0")

        except Exception as e:
            logging.error(f"Ошибка при проверке лимита: {e}")

        await asyncio.sleep(3600)

# --- FSM ---
class SetLimit(StatesGroup):
    waiting_for_number = State()

class AddLimit(StatesGroup):
    waiting_for_number = State()

from functools import wraps

def restricted(func):
    @wraps(func)
    async def wrapper(message_or_callback, *args, **kwargs):
        # Проверка для обычных сообщений
        if isinstance(message_or_callback, types.Message):
            user_id = message_or_callback.from_user.id
        # Проверка для inline callback
        elif isinstance(message_or_callback, types.CallbackQuery):
            user_id = message_or_callback.from_user.id
        else:
            return await func(message_or_callback, *args, **kwargs)

        if user_id not in config.ALLOWED_USERS:
            if isinstance(message_or_callback, types.Message):
                await message_or_callback.answer("❌ У вас нет доступа к этому боту.")
            elif isinstance(message_or_callback, types.CallbackQuery):
                await message_or_callback.answer("❌ У вас нет доступа к этому боту.", show_alert=True)
            return
        return await func(message_or_callback, *args, **kwargs)
    return wrapper


# --- Хендлеры ---

@dp.message(F.text.in_(["/start"]))
@restricted
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот для управления лимитами ИИ бота компании БСК(Avito_Bitrix).\n"
        "Используйте кнопку ℹ️ Справка или команду /help для получения инструкции",
        reply_markup=main_kb
    )

@dp.message(F.text.in_(["📊 Статус", "/status"]))
@restricted
async def status(message: types.Message):
    r = await get_redis()
    limit = await r.get("chat_limit") or 0
    count = await r.get("chat_count") or 0
    await message.answer(f"📊 Статус:\nЛимит: {limit}\nИспользовано: {count}", reply_markup=main_kb)

@dp.message(F.text.in_(["⚙️ Установить лимит", "/setlimit"]))
@restricted
async def ask_set_limit(message: types.Message, state: FSMContext):
    await message.answer("Введите новое значение лимита или выберите готовый вариант:", reply_markup=quick_limit_keyboard("set"))
    await message.answer("Для отмены нажмите ❌ Отмена", reply_markup=cancel_kb)
    await state.set_state(SetLimit.waiting_for_number)

@dp.message(SetLimit.waiting_for_number, F.text.regexp(r"^\d+$"))
@restricted
async def process_limit_input(message: types.Message, state: FSMContext):
    number = int(message.text)
    r = await get_redis()
    
    # Устанавливаем новый лимит и сбрасываем счетчики
    await r.set("chat_limit", number)
    await r.set("chat_count", 0)
    
    
    # Сбрасываем флаги уведомлений
    if number > 15:
        await r.set("limit_warning_sent", "0")
    
    await message.answer(f"✅ Лимит установлен: {number}", reply_markup=main_kb)
    await state.clear()


@dp.message(F.text.in_(["➕ Добавить к лимиту", "/add"]))
@restricted
async def ask_add_limit(message: types.Message, state: FSMContext):
    await message.answer("Введите число для добавления или выберите готовый вариант:", reply_markup=quick_limit_keyboard("add"))
    await message.answer("Для отмены нажмите ❌ Отмена", reply_markup=cancel_kb)
    await state.set_state(AddLimit.waiting_for_number)

@dp.message(AddLimit.waiting_for_number, F.text.regexp(r"^\d+$"))
@restricted
async def process_add_limit(message: types.Message, state: FSMContext):
    number = int(message.text)
    r = await get_redis()
    current = int(await r.get("chat_limit") or 0)
    new_limit = current + number

    # Устанавливаем новый лимит
    await r.set("chat_limit", new_limit)

    # Сбрасываем флаг предупреждения, если лимит теперь больше 15
    if new_limit > 15:
        await r.set("limit_warning_sent", "0")

    await message.answer(f"➕ Лимит увеличен: +{number}, новый={new_limit}", reply_markup=main_kb)
    await state.clear()


@dp.message(F.text.in_(["❌ Отмена"]))
@restricted
async def cancel_fsm(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активного действия.", reply_markup=main_kb)
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_kb)

@dp.callback_query(F.data.regexp(r"^(set|add)_limit:(\d+)$"))
@restricted
async def inline_limit_handler(callback: types.CallbackQuery, state: FSMContext):
    mode, value = callback.data.split("_limit:")
    value = int(value)
    r = await get_redis()

    if mode == "set":
        await r.set("chat_limit", value)
        await r.set("chat_count", 0)
        
        await callback.message.answer(f"✅ Лимит установлен: {value}", reply_markup=main_kb)
        await state.clear()
    else:
        current = int(await r.get("chat_limit") or 0)
        new_limit = current + value
        await r.set("chat_limit", new_limit)
        await callback.message.answer(f"➕ Лимит увеличен: +{value}, новый={new_limit}", reply_markup=main_kb)
        await state.clear()

    await callback.answer()

@dp.message(F.text.in_(["ℹ️ Справка", "/help"]))
@restricted
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


async def set_commands():
    commands = [
        types.BotCommand(command="status", description="Показать статус"),
        types.BotCommand(command="setlimit", description="Установить новый лимит"),
        types.BotCommand(command="add", description="Добавить к лимиту"),
        types.BotCommand(command="help", description="Справка"),
    ]
    await bot.set_my_commands(commands)

# --- Запуск бота через поллинг ---
async def main():
    await set_commands()
    logging.info("Запуск бота через поллинг")

    # запускаем фоновую задачу
    asyncio.create_task(monitor_limit())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

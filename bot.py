import asyncio
import os
import random
import re
import time
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
import httpx
import json
import logging
from logging.handlers import RotatingFileHandler
from openai import OpenAI
import redis.asyncio as aioredis
from typing import Dict

import config

# Настройки логирования
log_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(log_dir, exist_ok=True)
file_handler = RotatingFileHandler(
    os.path.join(log_dir, "bot.log"),
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[file_handler, logging.StreamHandler()]
)

# FastAPI и Redis
app = FastAPI()
redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = aioredis.Redis(
            host=config.REDIS_HOST,
            port=int(config.REDIS_PORT),
            db=0,
            decode_responses=True
        )
    return redis_client

# OpenAI
client = OpenAI(
    api_key=config.OPENAI_API_KEY,
    base_url=config.PROXY_SERVICE_URL
)

# Константы
MAX_HISTORY_PAIRS = 60
MESSAGE_COLLECTION_WINDOW = getattr(config, "MESSAGE_COLLECTION_WINDOW")
MAX_COLLECTION_WINDOW = getattr(config, "MAX_COLLECTION_WINDOW")
MESSAGE_CHECK_INTERVAL = getattr(config, "MESSAGE_CHECK_INTERVAL")
OPENAI_TIMEOUT = getattr(config, "OPENAI_TIMEOUT")
OPENAI_RETRIES = getattr(config, "OPENAI_RETRIES")
COMBINE_MULTIPLE_MESSAGES = getattr(config, "COMBINE_MULTIPLE_MESSAGES")
HUMANIZE_MODE = getattr(config, "HUMANIZE_MODE")
OPERATOR_ID = getattr(config, "OPERATOR_ID", None)

PHONE_TRANSFER_DELAY = getattr(config, "PHONE_TRANSFER_DELAY")

# Глобальные переменные для воркеров и отложенных задач
workers = {}
workers_lock = asyncio.Lock()
worker_creation_locks: Dict[str, asyncio.Lock] = {}
WORKER_TIMEOUT = 300
MAX_CONSECUTIVE_ERRORS = 5
transfer_tasks = {}
transfer_tasks_lock = asyncio.Lock()
FETCH_AND_CLEAR = """
local msgs = redis.call('LRANGE', KEYS[1], 0, -1)
if #msgs > 0 then
    redis.call('DEL', KEYS[1])
end
return msgs
"""

# Утилиты
def has_phone_number(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    phone_patterns = [
        r'\+7\s*\(?[0-9]{3}\)?\s*[0-9]{3}[\s-]?[0-9]{2}[\s-]?[0-9]{2}',
        r'8\s*\(?[0-9]{3}\)?\s*[0-9]{3}[\s-]?[0-9]{2}[\s-]?[0-9]{2}',
        r'[0-9]{11}',
        r'[0-9]{10}',
        r'\+7[0-9]{10}',
    ]
    for pattern in phone_patterns:
        if re.search(pattern, text):
            logging.info(f"Найден номер телефона по шаблону: {pattern}")
            return True
    return False

# Функции для работы с Bitrix и GPT
async def transfer_to_operator(dialog_id: str, reason: str = "auto"):
    # (ваш код функции transfer_to_operator без изменений)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client_http:
            if OPERATOR_ID:
                method = "imopenlines.bot.session.transfer"
                params = {"DIALOG_ID": dialog_id, "OPERATOR_ID": OPERATOR_ID}
            else:
                method = "imopenlines.bot.session.operator"
                params = {"DIALOG_ID": dialog_id}
            
            resp = await client_http.post(config.BITRIX_WEBHOOK + f"{method}.json", data=params)
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get("result"):
                    logging.info(f"[Диалог {dialog_id}] Успешно переведен на оператора. Причина: {reason}")
                    return True
                else:
                    logging.warning(f"[Диалог {dialog_id}] Ошибка перевода на оператора: {result}")
                    return False
            else:
                logging.warning(f"[Диалог {dialog_id}] HTTP ошибка при переводе: {resp.status_code} - {resp.text[:200]}")
                return False
    except Exception as e:
        logging.error(f"[Диалог {dialog_id}] Исключение при переводе на оператора: {e}", exc_info=True)
        return False

async def get_dialog_history(dialog_id: str):
    r = await get_redis()
    history_json = await r.get(f"dialog:{dialog_id}")
    return json.loads(history_json) if history_json else []

async def save_dialog_history(dialog_id: str, messages):
    r = await get_redis()
    await r.set(f"dialog:{dialog_id}", json.dumps(messages))

async def get_gpt_response_with_retries(dialog_id: str, messages_for_gpt: list) -> str:
    # (ваш код функции get_gpt_response_with_retries без изменений)
    for attempt in range(OPENAI_RETRIES):
        try:
            if HUMANIZE_MODE and attempt == 0:
                thinking_time = 1 + random.uniform(0, 3)
                await asyncio.sleep(thinking_time)
            response = await asyncio.wait_for(
                asyncio.to_thread(lambda: client.chat.completions.create(model=config.GPT_MODEL, messages=messages_for_gpt)),
                timeout=OPENAI_TIMEOUT
            )
            content = response.choices[0].message.content
            if content:
                logging.info(f"[Диалог {dialog_id}] GPT ответил успешно (попытка {attempt+1})")
                return content
            else:
                logging.warning(f"[Диалог {dialog_id}] GPT вернул пустой ответ (попытка {attempt+1})")
        except asyncio.TimeoutError:
            logging.warning(f"[Диалог {dialog_id}] GPT таймаут (попытка {attempt+1}/{OPENAI_RETRIES})")
        except Exception as e:
            logging.error(f"[Диалог {dialog_id}] GPT ошибка (попытка {attempt+1}): {e}")
        if attempt < OPENAI_RETRIES - 1:
            wait_time = 2 ** attempt
            logging.info(f"[Диалог {dialog_id}] Ждем {wait_time}с перед повтором GPT")
            await asyncio.sleep(wait_time)
    logging.error(f"[Диалог {dialog_id}] GPT не ответил после {OPENAI_RETRIES} попыток")
    return None

async def send_to_bitrix_safely(dialog_id: str, message: str):
    # (ваш код функции send_to_bitrix_safely без изменений)
    for attempt in range(3):
        try:
            if HUMANIZE_MODE:
                sending_delay = 0.5 + random.uniform(0, 1.5)
                await asyncio.sleep(sending_delay)
            async with httpx.AsyncClient(timeout=20.0) as client_http:
                resp = await client_http.post(
                    config.BITRIX_WEBHOOK + "imbot.message.add.json",
                    data={"DIALOG_ID": dialog_id, "MESSAGE": message, "BOT_ID": config.BOT_ID, "CLIENT_ID": config.CLIENT_ID}
                )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("result"):
                    logging.info(f"[Диалог {dialog_id}] Ответ отправлен в Bitrix")
                    return
                else:
                    logging.warning(f"[Диалог {dialog_id}] Bitrix отклонил сообщение: {result}")
            else:
                logging.warning(f"[Диалог {dialog_id}] Bitrix HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.TimeoutException:
            logging.warning(f"[Диалог {dialog_id}] Bitrix таймаут (попытка {attempt+1})")
        except Exception as e:
            logging.error(f"[Диалог {dialog_id}] Ошибка Bitrix (попытка {attempt+1}): {e}")
        if attempt < 2:
            wait_time = 2 ** attempt
            logging.info(f"[Диалог {dialog_id}] Ждем {wait_time}с перед повтором Bitrix")
            await asyncio.sleep(wait_time)
    logging.error(f"[Диалог {dialog_id}] Не удалось отправить в Bitrix после 3 попыток")

async def ensure_worker_running(dialog_id: str, user_name: str):
    # (ваш код функции ensure_worker_running без изменений)
    if dialog_id in workers and not workers[dialog_id].done():
        return
    async with workers_lock:
        if dialog_id not in worker_creation_locks:
            worker_creation_locks[dialog_id] = asyncio.Lock()
        dialog_lock = worker_creation_locks[dialog_id]
    async with dialog_lock:
        if dialog_id not in workers or workers[dialog_id].done():
            if dialog_id in workers:
                old_worker = workers.pop(dialog_id)
                logging.info(f"[Диалог {dialog_id}] Удален завершенный воркер (done: {old_worker.done()})")
            workers[dialog_id] = asyncio.create_task(dialog_worker(dialog_id, user_name))
            logging.info(f"[Диалог {dialog_id}] Запущен новый воркер")
        else:
            logging.debug(f"[Диалог {dialog_id}] Воркер уже работает, пропускаем создание")

async def dialog_worker(dialog_id: str, user_name: str):
    # (ваш код функции dialog_worker без изменений)
    r = await get_redis()
    loop = asyncio.get_running_loop()
    last_active = loop.time()
    consecutive_errors = 0
    try:
        while True:
            try:
                initial_messages = await r.eval(FETCH_AND_CLEAR, 1, f"pending:{dialog_id}")
                if not initial_messages:
                    if loop.time() - last_active > WORKER_TIMEOUT:
                        logging.info(f"[Диалог {dialog_id}] Воркер неактивен, завершаем.")
                        break
                    await asyncio.sleep(MESSAGE_CHECK_INTERVAL)
                    continue
                consecutive_errors = 0
                last_active = loop.time()
                current_batch_user_messages = list(initial_messages)
                logging.info(f"[Диалог {dialog_id}] Первая пачка: {len(initial_messages)} сообщений.")
                collection_start_time = time.monotonic()
                absolute_start_time = time.monotonic()
                while True:
                    await asyncio.sleep(MESSAGE_CHECK_INTERVAL)
                    additional = await r.eval(FETCH_AND_CLEAR, 1, f"pending:{dialog_id}")
                    if additional:
                        current_batch_user_messages.extend(additional)
                        logging.info(f"[Диалог {dialog_id}] Добавлено {len(additional)} сообщений. Всего: {len(current_batch_user_messages)}")
                        collection_start_time = time.monotonic()
                    elapsed_since_last = time.monotonic() - collection_start_time
                    elapsed_total = time.monotonic() - absolute_start_time
                    if elapsed_since_last >= MESSAGE_COLLECTION_WINDOW or elapsed_total >= MAX_COLLECTION_WINDOW:
                        break
                await process_messages_safely(dialog_id, user_name, current_batch_user_messages, r)
            except asyncio.CancelledError:
                logging.info(f"[Диалог {dialog_id}] Воркер отменен")
                break
            except Exception as e:
                consecutive_errors += 1
                logging.error(f"[Диалог {dialog_id}] Ошибка в воркере (#{consecutive_errors}): {e}", exc_info=True)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logging.critical(f"[Диалог {dialog_id}] Слишком много ошибок подряд, завершаем воркер")
                    break
                wait_time = min(2 ** consecutive_errors, 60)
                logging.info(f"[Диалог {dialog_id}] Ждем {wait_time}с перед повтором")
                await asyncio.sleep(wait_time)
    except Exception as e:
        logging.critical(f"[Диалог {dialog_id}] Критическая ошибка воркера: {e}", exc_info=True)
    finally:
        workers.pop(dialog_id, None)
        async with workers_lock:
            if dialog_id in worker_creation_locks:
                worker_creation_locks.pop(dialog_id, None)
        logging.info(f"[Диалог {dialog_id}] Воркер завершён.")

async def process_messages_safely(dialog_id: str, user_name: str, messages: list, redis_client):
    """
    Обработка обычных текстовых сообщений через GPT.
    ВАЖНО: эта функция больше не содержит проверку на номер телефона.
    """
    try:
        dialog_history = await get_dialog_history(dialog_id)
        if len(messages) == 1:
            dialog_history.append({"role": "user", "content": messages[0]})
        else:
            if COMBINE_MULTIPLE_MESSAGES:
                combined = "\n".join([f"{i+1}. {msg}" for i, msg in enumerate(messages)])
                dialog_history.append({"role": "user", "content": combined})
            else:
                for m in messages:
                    dialog_history.append({"role": "user", "content": m})
        max_messages = MAX_HISTORY_PAIRS * 2
        if len(dialog_history) > max_messages:
            dialog_history = dialog_history[-max_messages:]
        system_prompt = config.PROMPT.replace("{имя}", user_name)
        messages_for_gpt = [{"role": "system", "content": system_prompt}] + dialog_history
        answer = await get_gpt_response_with_retries(dialog_id, messages_for_gpt)
        if answer is None:
            answer = "Извините, сейчас у меня технические трудности. Попробуйте повторить запрос."
        dialog_history.append({"role": "assistant", "content": answer})
        if len(dialog_history) > max_messages:
            dialog_history = dialog_history[-max_messages:]
        await save_dialog_history(dialog_id, dialog_history)
        await send_to_bitrix_safely(dialog_id, answer)
        logging.info(f"[Диалог {dialog_id}] Бот ответил: {answer}")
        logging.info(f"[Диалог {dialog_id}] Сообщения обработаны успешно")
    except Exception as e:
        logging.error(f"[Диалог {dialog_id}] Ошибка при обработке сообщений: {e}", exc_info=True)
        try:
            for msg in reversed(messages):
                await redis_client.lpush(f"pending:{dialog_id}", msg)
            logging.info(f"[Диалог {dialog_id}] Сообщения возвращены в очередь")
        except Exception as return_error:
            logging.error(f"[Диалог {dialog_id}] Ошибка при возврате сообщений в очередь: {return_error}")
        raise

async def schedule_transfer(dialog_id: str):
    """Создает отложенную задачу на перевод диалога на оператора."""
    delay_seconds = PHONE_TRANSFER_DELAY
    try:
        logging.info(f"[Диалог {dialog_id}] Запланирован отложенный перевод через {delay_seconds} секунд.")
        await asyncio.sleep(delay_seconds)
        transfer_success = await transfer_to_operator(dialog_id, "delayed_transfer")
        if transfer_success:
            logging.info(f"[Диалог {dialog_id}] Отложенный перевод на оператора успешно выполнен.")
        else:
            logging.warning(f"[Диалог {dialog_id}] Отложенный перевод не удался.")
    except asyncio.CancelledError:
        logging.info(f"[Диалог {dialog_id}] Отложенная задача на перевод была отменена.")
    except Exception as e:
        logging.error(f"[Диалог {dialog_id}] Критическая ошибка в задаче отложенного перевода: {e}", exc_info=True)
    finally:
        async with transfer_tasks_lock:
            transfer_tasks.pop(dialog_id, None)

@app.post("/bot")
async def bot_handler(
    request: Request,
    event: str = Form(None),
    dialog_id: str = Form(None, alias="data[PARAMS][DIALOG_ID]"),
    user_message: str = Form(None, alias="data[PARAMS][MESSAGE]"),
    user_name: str = Form("клиент", alias="data[USER][FIRST_NAME]"),
    message_type: str = Form(None, alias="data[PARAMS][MESSAGE_TYPE]"),
):
    try:
        # fallback на JSON
        if event is None or dialog_id is None or user_message is None:
            try:
                data = await request.json()
                event = data.get("event", event)
                dialog_params = data.get("data", {}).get("PARAMS", {})
                dialog_id = dialog_params.get("DIALOG_ID", dialog_id)
                user_message = dialog_params.get("MESSAGE", user_message)
                message_type = dialog_params.get("MESSAGE_TYPE", message_type)
                user_name = data.get("data", {}).get("USER", {}).get("FIRST_NAME", user_name)
            except Exception as json_error:
                logging.warning(f"Ошибка парсинга JSON: {json_error}")

        logging.info(f"RAW: event={event}, dialog_id={dialog_id}, msg={user_message}, type={message_type}")

        if not dialog_id:
            logging.error("Нет dialog_id в запросе")
            return JSONResponse({"status": "error", "message": "Missing dialog_id"}, status_code=400)

        # ФИЛЬТР 1: Игнорируем пустое сообщение
        if not user_message or not user_message.strip():
            return JSONResponse({"status": "ok", "message": "Empty message"})

        # ФИЛЬТР 2: Немедленный перевод для файлов/фото
        if message_type and message_type.upper() in ['FILE', 'ATTACH', 'STICKER', 'AUDIO', 'VIDEO']:
            logging.info(f"[Диалог {dialog_id}] Получено нетекстовое сообщение типа {message_type}, переводим на оператора.")
            transfer_success = await transfer_to_operator(dialog_id, "non_text_message")
            if transfer_success:
                return JSONResponse({"status": "transferred_to_operator"})
            else:
                return JSONResponse({"status": "transfer_failed"})

        # ФИЛЬТР 3: Отложенный перевод при обнаружении номера телефона
        if has_phone_number(user_message):
            logging.info(f"[Диалог {dialog_id}] Обнаружен номер телефона. Продолжаем диалог и планируем отложенный перевод.")
            # Планируем отложенный перевод на оператора
            async with transfer_tasks_lock:
                if dialog_id not in transfer_tasks or transfer_tasks[dialog_id].done():
                    logging.info(f"[Диалог {dialog_id}] Создаем новую задачу отложенного перевода.")
                    transfer_tasks[dialog_id] = asyncio.create_task(schedule_transfer(dialog_id))
                else:
                    logging.info(f"[Диалог {dialog_id}] Отложенный перевод уже запланирован. Пропускаем.")
            # Сообщение продолжает обрабатываться GPT, как обычный текст
        
        # Обработка всех остальных сообщений
        r = await get_redis()
        await r.rpush(f"pending:{dialog_id}", user_message.strip())
        await ensure_worker_running(dialog_id, user_name or "клиент")

        return JSONResponse({"status": "queued"})
        
    except Exception as e:
        logging.error(f"Ошибка в bot_handler: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)

# Эндпоинты для мониторинга
@app.get("/status")
async def status():
    # (ваш код функции status без изменений)
    try:
        r = await get_redis()
        await r.ping()
        redis_status = "connected"
    except Exception:
        redis_status = "disconnected"
    active_workers = sum(1 for task in workers.values() if not task.done())
    completed_workers = sum(1 for task in workers.values() if task.done())
    return {
        "status": "ok",
        "redis": redis_status,
        "workers": {"active": active_workers, "completed": completed_workers, "total": len(workers)},
        "dialogs": list(workers.keys())
    }

@app.post("/transfer_to_operator")
async def manual_transfer_to_operator(dialog_id: str):
    # (ваш код функции manual_transfer_to_operator без изменений)
    try:
        transfer_success = await transfer_to_operator(dialog_id, "manual")
        return JSONResponse({"status": "success", "message": "Dialog transferred to operator"}) if transfer_success else JSONResponse({"status": "error", "message": "Failed to transfer dialog"})
    except Exception as e:
        logging.error(f"Ошибка при ручном переводе диалога {dialog_id}: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)

# Graceful shutdown
@app.on_event("shutdown")
async def shutdown_workers():
    # (ваш код функции shutdown_workers без изменений)
    logging.info("Закрытие всех воркеров и отложенных переводов...")
    async with transfer_tasks_lock:
        transfer_tasks_to_cancel = list(transfer_tasks.values())
        transfer_tasks.clear()
    for task in transfer_tasks_to_cancel:
        if not task.done():
            task.cancel()
    if transfer_tasks_to_cancel:
        await asyncio.gather(*transfer_tasks_to_cancel, return_exceptions=True)
        logging.info(f"Отменено {len(transfer_tasks_to_cancel)} отложенных переводов")
    tasks_to_cancel = []
    for dialog_id, task in workers.items():
        if not task.done():
            logging.info(f"Отменяем воркер для диалога {dialog_id}")
            task.cancel()
            tasks_to_cancel.append(task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    logging.info("Все воркеры и задачи завершены.")

@app.on_event("shutdown")
async def shutdown_redis():
    # (ваш код функции shutdown_redis без изменений)
    global redis_client
    if redis_client:
        try:
            await redis_client.close()
            await redis_client.connection_pool.disconnect()
            logging.info("Redis connection closed gracefully")
        except Exception as e:
            logging.error(f"Ошибка при закрытии Redis: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
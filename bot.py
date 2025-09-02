from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
import httpx
import asyncio
from openai import OpenAI
import redis.asyncio as aioredis
import json
import config
import logging
from logging.handlers import RotatingFileHandler
import os
import time
from typing import Dict
import random

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

# ----------------- FastAPI -----------------
app = FastAPI()

# ----------------- Redis -----------------
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

# ----------------- История -----------------
async def get_dialog_history(dialog_id: str):
    r = await get_redis()
    history_json = await r.get(f"dialog:{dialog_id}")
    if history_json:
        return json.loads(history_json)
    return []

async def save_dialog_history(dialog_id: str, messages):
    r = await get_redis()
    await r.set(f"dialog:{dialog_id}", json.dumps(messages))

# ----------------- OpenAI -----------------
client = OpenAI(
    api_key=config.OPENAI_API_KEY,
    base_url=config.PROXY_SERVICE_URL
)

# ----------------- Константы -----------------
MAX_HISTORY_PAIRS = 60
MESSAGE_COLLECTION_WINDOW = getattr(config, "MESSAGE_COLLECTION_WINDOW")
MAX_COLLECTION_WINDOW = getattr(config, "MAX_COLLECTION_WINDOW")
MESSAGE_CHECK_INTERVAL = getattr(config, "MESSAGE_CHECK_INTERVAL")
OPENAI_TIMEOUT = getattr(config, "OPENAI_TIMEOUT")
OPENAI_RETRIES = getattr(config, "OPENAI_RETRIES")
COMBINE_MULTIPLE_MESSAGES = getattr(config, "COMBINE_MULTIPLE_MESSAGES")
HUMANIZE_MODE = getattr(config, "HUMANIZE_MODE")

# ----------------- Воркеры с защитой от ошибок -----------------
workers = {}
workers_lock = asyncio.Lock()
worker_creation_locks: Dict[str, asyncio.Lock] = {}
WORKER_TIMEOUT = 300
MAX_CONSECUTIVE_ERRORS = 5

# ---- Lua-скрипт для атомарного забора и очистки ----
FETCH_AND_CLEAR = """
local msgs = redis.call('LRANGE', KEYS[1], 0, -1)
if #msgs > 0 then
  redis.call('DEL', KEYS[1])
end
return msgs
"""

async def dialog_worker(dialog_id: str, user_name: str):
    """
    Устойчивый к ошибкам воркер с улучшенной обработкой исключений
    """
    r = await get_redis()
    loop = asyncio.get_running_loop()
    last_active = loop.time()
    consecutive_errors = 0

    try:
        while True:
            try:
                # Основной цикл обработки
                initial_messages = await r.eval(FETCH_AND_CLEAR, 1, f"pending:{dialog_id}")
                if not initial_messages:
                    if loop.time() - last_active > WORKER_TIMEOUT:
                        logging.info(f"[Диалог {dialog_id}] Воркер неактивен, завершаем.")
                        break
                    await asyncio.sleep(MESSAGE_CHECK_INTERVAL)
                    continue

                # Сброс счетчика ошибок при успешной обработке
                consecutive_errors = 0
                last_active = loop.time()
                
                current_batch_user_messages = list(initial_messages)
                logging.info(f"[Диалог {dialog_id}] Первая пачка: {len(initial_messages)} сообщений.")

                # Накопление сообщений
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

                # Обработка GPT и отправка (с защитой от ошибок)
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
                
                # Экспоненциальная задержка при ошибках
                wait_time = min(2 ** consecutive_errors, 60)  # максимум 60 секунд
                logging.info(f"[Диалог {dialog_id}] Ждем {wait_time}с перед повтором")
                await asyncio.sleep(wait_time)

    except Exception as e:
        logging.critical(f"[Диалог {dialog_id}] Критическая ошибка воркера: {e}", exc_info=True)
    finally:
        # Очистка
        workers.pop(dialog_id, None)
        # Очищаем блокировку диалога при завершении воркера
        async with workers_lock:
            if dialog_id in worker_creation_locks:
                worker_creation_locks.pop(dialog_id, None)
        logging.info(f"[Диалог {dialog_id}] Воркер завершён.")

async def process_messages_safely(dialog_id: str, user_name: str, messages: list, redis_client):
    """
    Безопасная обработка сообщений с изолированной обработкой ошибок
    """
    try:
        # Получение и обновление истории
        dialog_history = await get_dialog_history(dialog_id)
        
        if len(messages) == 1:
            dialog_history.append({"role": "user", "content": messages[0]})
        else:
            if COMBINE_MULTIPLE_MESSAGES:
                combined = "\n".join([f"{i+1}. {msg}" for i, msg in enumerate(messages)])
                dialog_history.append({"role": "user", "content": combined})
                logging.info(f"[Диалог {dialog_id}] Объединяем {len(messages)} сообщений в одно")
            else:
                for m in messages:
                    dialog_history.append({"role": "user", "content": m})
                logging.info(f"[Диалог {dialog_id}] Добавляем {len(messages)} сообщений отдельно")

        max_messages = MAX_HISTORY_PAIRS * 2
        if len(dialog_history) > max_messages:
            dialog_history = dialog_history[-max_messages:]

        system_prompt = config.PROMPT.replace("{имя}", user_name)
        messages_for_gpt = [{"role": "system", "content": system_prompt}] + dialog_history

        # GPT запрос с повторами
        answer = await get_gpt_response_with_retries(dialog_id, messages_for_gpt)
        
        if answer is None:
            answer = "Извините, сейчас у меня технические трудности. Попробуйте повторить запрос."

        # Сохранение истории
        dialog_history.append({"role": "assistant", "content": answer})
        if len(dialog_history) > max_messages:
            dialog_history = dialog_history[-max_messages:]
        await save_dialog_history(dialog_id, dialog_history)

        # Отправка в Bitrix
        await send_to_bitrix_safely(dialog_id, answer)
        
        logging.info(f"[Диалог {dialog_id}] Сообщения обработаны успешно")
        
    except Exception as e:
        logging.error(f"[Диалог {dialog_id}] Ошибка при обработке сообщений: {e}", exc_info=True)
        # Возвращаем сообщения обратно в очередь при ошибке
        try:
            for msg in reversed(messages):
                await redis_client.lpush(f"pending:{dialog_id}", msg)
            logging.info(f"[Диалог {dialog_id}] Сообщения возвращены в очередь")
        except Exception as return_error:
            logging.error(f"[Диалог {dialog_id}] Ошибка при возврате сообщений в очередь: {return_error}")
        raise

async def get_gpt_response_with_retries(dialog_id: str, messages_for_gpt: list) -> str:
    """Получение ответа GPT с повторными попытками"""
    for attempt in range(OPENAI_RETRIES):
        try:
            if HUMANIZE_MODE and attempt == 0:  # только в первую попытку
                thinking_time = 1 + random.uniform(0, 3)
                await asyncio.sleep(thinking_time)

            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: client.chat.completions.create(
                        model=config.GPT_MODEL,
                        messages=messages_for_gpt
                    )
                ),
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
            wait_time = 2 ** attempt  # экспоненциальная задержка
            logging.info(f"[Диалог {dialog_id}] Ждем {wait_time}с перед повтором GPT")
            await asyncio.sleep(wait_time)
    
    logging.error(f"[Диалог {dialog_id}] GPT не ответил после {OPENAI_RETRIES} попыток")
    return None

async def send_to_bitrix_safely(dialog_id: str, message: str):
    """Безопасная отправка в Bitrix с повторными попытками"""
    for attempt in range(3):
        try:
            if HUMANIZE_MODE:
                sending_delay = 0.5 + random.uniform(0, 1.5)
                await asyncio.sleep(sending_delay)

            async with httpx.AsyncClient(timeout=20.0) as client_http:
                resp = await client_http.post(
                    config.BITRIX_WEBHOOK + "imbot.message.add.json",
                    data={
                        "DIALOG_ID": dialog_id,
                        "MESSAGE": message,
                        "BOT_ID": config.BOT_ID,
                        "CLIENT_ID": config.CLIENT_ID
                    }
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
    """
    Гарантированно запускает воркер для диалога с атомарностью на уровне диалога
    """
    # Быстрая проверка без блокировки
    if dialog_id in workers and not workers[dialog_id].done():
        return
    
    # Получаем или создаем блокировку для этого диалога
    async with workers_lock:
        if dialog_id not in worker_creation_locks:
            worker_creation_locks[dialog_id] = asyncio.Lock()
        dialog_lock = worker_creation_locks[dialog_id]
    
    # Блокируемся на уровне конкретного диалога (более эффективно)
    async with dialog_lock:
        # Повторная проверка под блокировкой
        if dialog_id not in workers or workers[dialog_id].done():
            # Удаляем завершенный воркер, если есть
            if dialog_id in workers:
                old_worker = workers.pop(dialog_id)
                logging.info(f"[Диалог {dialog_id}] Удален завершенный воркер (done: {old_worker.done()})")
            
            # Создаем новый воркер
            workers[dialog_id] = asyncio.create_task(dialog_worker(dialog_id, user_name))
            logging.info(f"[Диалог {dialog_id}] Запущен новый воркер")
        else:
            logging.debug(f"[Диалог {dialog_id}] Воркер уже работает, пропускаем создание")

# ----------------- Хендлер -----------------
@app.post("/bot")
async def bot_handler(
    request: Request,
    event: str = Form(None),
    dialog_id: str = Form(None, alias="data[PARAMS][DIALOG_ID]"),
    user_message: str = Form(None, alias="data[PARAMS][MESSAGE]"),
    user_name: str = Form("клиент", alias="data[USER][FIRST_NAME]"),
):
    try:
        # fallback на JSON
        if event is None or dialog_id is None or user_message is None:
            try:
                data = await request.json()
                event = data.get("event", event)
                dialog_id = data.get("data", {}).get("PARAMS", {}).get("DIALOG_ID", dialog_id)
                user_message = data.get("data", {}).get("PARAMS", {}).get("MESSAGE", user_message)
                user_name = data.get("data", {}).get("USER", {}).get("FIRST_NAME", user_name)
            except Exception as json_error:
                logging.warning(f"Ошибка парсинга JSON: {json_error}")

        logging.info(f"RAW: event={event}, dialog_id={dialog_id}, msg={user_message}")

        if not user_message or not user_message.strip():
            return JSONResponse({"status": "ok"})

        if not dialog_id:
            logging.error("Нет dialog_id в запросе")
            return JSONResponse({"status": "error", "message": "Missing dialog_id"}, status_code=400)

        # Добавление сообщения в очередь
        r = await get_redis()
        await r.rpush(f"pending:{dialog_id}", user_message.strip())

        # Обеспечиваем существование воркера с полной атомарностью
        await ensure_worker_running(dialog_id, user_name or "клиент")

        return JSONResponse({"status": "queued"})
        
    except Exception as e:
        logging.error(f"Ошибка в bot_handler: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)

# ---- Эндпоинт для мониторинга ----
@app.get("/status")
async def status():
    """Статус воркеров для мониторинга"""
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
        "workers": {
            "active": active_workers,
            "completed": completed_workers,
            "total": len(workers)
        },
        "dialogs": list(workers.keys())
    }

# ---- graceful shutdown всех воркеров ----
@app.on_event("shutdown")
async def shutdown_workers():
    logging.info("Закрытие всех воркеров...")
    tasks_to_cancel = []
    
    for dialog_id, task in workers.items():
        if not task.done():
            logging.info(f"Отменяем воркер для диалога {dialog_id}")
            task.cancel()
            tasks_to_cancel.append(task)
    
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    
    logging.info("Все воркеры завершены.")

# ---- graceful shutdown Redis ----
@app.on_event("shutdown")
async def shutdown_redis():
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
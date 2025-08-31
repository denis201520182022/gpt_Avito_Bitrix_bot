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

file_handler = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")

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
            host="redis", port=6379, db=0, decode_responses=True
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
client = OpenAI(api_key=config.OPENAI_API_KEY)

# ----------------- Ограничение истории -----------------
MAX_HISTORY_PAIRS = 50  # лимит пар пользователь→бот

# ----------------- Очередь обработки с воркером -----------------
workers = {}  # {dialog_id: asyncio.Task}
WORKER_TIMEOUT = 300  # секунда неактивности после которой воркер завершится
OPENAI_RETRIES = 3     # число попыток при таймауте

async def dialog_worker(dialog_id: str, user_name: str):
    """
    Вечный воркер для диалога: берёт накопленные сообщения и обрабатывает их пачками.
    Автоудаление после WORKER_TIMEOUT сек. без активности.
    """
    r = await get_redis()
    last_active = asyncio.get_event_loop().time()

    try:
        while True:
            messages = await r.lrange(f"pending:{dialog_id}", 0, -1)
            if not messages:
                if asyncio.get_event_loop().time() - last_active > WORKER_TIMEOUT:
                    logging.info(f"[Диалог {dialog_id}] Воркер неактивен, завершаем.")
                    break
                await asyncio.sleep(0.5)
                continue

            last_active = asyncio.get_event_loop().time()  # обновляем активность
            await r.delete(f"pending:{dialog_id}")  # забираем все на обработку

            dialog_history = await get_dialog_history(dialog_id)
            for m in messages:
                dialog_history.append({"role": "user", "content": m})

            max_messages = MAX_HISTORY_PAIRS * 2
            if len(dialog_history) > max_messages:
                dialog_history = dialog_history[-max_messages:]

            system_prompt = config.PROMPT.replace("{имя}", user_name)
            messages_for_gpt = [{"role": "system", "content": system_prompt}] + dialog_history

            logging.info(f"[Диалог {dialog_id}] Отправляем в GPT {len(messages)} сообщений")

            # ---------------- OpenAI с повторными попытками ----------------
            answer = None
            for attempt in range(OPENAI_RETRIES):
                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: client.chat.completions.create(
                                model=config.GPT_MODEL,
                                messages=messages_for_gpt
                            )
                        ),
                        timeout=60.0
                    )
                    answer = response.choices[0].message.content
                    break
                except asyncio.TimeoutError:
                    logging.warning(f"[Диалог {dialog_id}] OpenAI таймаут, попытка {attempt+1}/{OPENAI_RETRIES}")
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.error(f"[Диалог {dialog_id}] OpenAI ошибка: {e}")
                    await asyncio.sleep(1)

            if answer is None:
                answer = "Извините, я временно недоступен."

            dialog_history.append({"role": "assistant", "content": answer})
            if len(dialog_history) > max_messages:
                dialog_history = dialog_history[-max_messages:]
            await save_dialog_history(dialog_id, dialog_history)

            logging.info(f"[Диалог {dialog_id}] Бот: {answer}")

            # Отправка в Bitrix
            try:
                async with httpx.AsyncClient(timeout=20.0) as client_http:
                    resp = await client_http.post(
                        config.BITRIX_WEBHOOK + "imbot.message.add.json",
                        data={
                            "DIALOG_ID": dialog_id,
                            "MESSAGE": answer,
                            "BOT_ID": config.BOT_ID,
                            "CLIENT_ID": config.CLIENT_ID
                        }
                    )
                    logging.info(f"Bitrix response: {resp.status_code} {resp.text}")
            except Exception as e:
                logging.error(f"Bitrix request error: {e}")

            await asyncio.sleep(0.1)
    finally:
        workers.pop(dialog_id, None)
        logging.info(f"[Диалог {dialog_id}] Воркер завершён.")

# ----------------- Хендлер -----------------
@app.post("/bot")
async def bot_handler(
    request: Request,
    event: str = Form(None),
    dialog_id: str = Form(None, alias="data[PARAMS][DIALOG_ID]"),
    user_message: str = Form(None, alias="data[PARAMS][MESSAGE]"),
    user_name: str = Form("клиент", alias="data[USER][FIRST_NAME]"),
):
    # fallback на JSON
    if event is None or dialog_id is None or user_message is None:
        try:
            data = await request.json()
            event = data.get("event", event)
            dialog_id = data.get("data", {}).get("PARAMS", {}).get("DIALOG_ID", dialog_id)
            user_message = data.get("data", {}).get("PARAMS", {}).get("MESSAGE", user_message)
            user_name = data.get("data", {}).get("USER", {}).get("FIRST_NAME", user_name)
        except Exception:
            pass

    logging.info(f"RAW: event={event}, dialog_id={dialog_id}, msg={user_message}")

    if not user_message or not user_message.strip():
        return JSONResponse({"status": "ok"})

    r = await get_redis()
    await r.rpush(f"pending:{dialog_id}", user_message)

    # Запускаем воркер диалога, если ещё не запущен
    if dialog_id not in workers:
        workers[dialog_id] = asyncio.create_task(dialog_worker(dialog_id, user_name))
        logging.info(f"[Диалог {dialog_id}] Запущен воркер")

    return JSONResponse({"status": "queued"})

# ---- graceful shutdown всех воркеров ----
@app.on_event("shutdown")
async def shutdown_workers():
    logging.info("Закрытие всех воркеров...")
    for task in workers.values():
        task.cancel()
    await asyncio.gather(*workers.values(), return_exceptions=True)
    logging.info("Все воркеры завершены.")

# ---- graceful shutdown Redis ----
@app.on_event("shutdown")
async def shutdown_redis():
    global redis_client
    if redis_client:
        await redis_client.close()
        await redis_client.connection_pool.disconnect()
        logging.info("Redis connection closed gracefully")

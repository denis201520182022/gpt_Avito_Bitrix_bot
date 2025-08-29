from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse
import httpx
import asyncio
from openai import OpenAI
import redis.asyncio as aioredis
import json
import config
import logging

# ----------------- Логирование -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    encoding="utf-8"
)


# ----------------- FastAPI -----------------
app = FastAPI()

# ----------------- Redis -----------------
redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = aioredis.Redis(
            host='redis', port=6379, db=0, decode_responses=True
        )

    return redis_client

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
MAX_HISTORY_PAIRS = 50  # лимит пар сообщений: пользователь → бот

# ----------------- Обработчик сообщений -----------------
@app.post("/bot")
async def bot_handler(
    event: str = Form(...),
    dialog_id: str = Form(..., alias="data[PARAMS][DIALOG_ID]"),
    user_message: str = Form(..., alias="data[PARAMS][MESSAGE]"),
    user_name: str = Form("клиент", alias="data[USER][FIRST_NAME]"),
):
    logging.info(f"RAW DATA: event={event}, dialog_id={dialog_id}, message={user_message}")

    # ----------------- Задержка -----------------
    await asyncio.sleep(30)  # 30 секунд

    # ----------------- Фильтрация пустых сообщений -----------------
    if not user_message.strip():
        logging.info(f"[Диалог {dialog_id}] Пустое сообщение, ответ не генерируем")
        return JSONResponse({"status": "ok"})

    system_prompt = config.PROMPT.replace("{имя}", user_name)
    dialog_history = await get_dialog_history(dialog_id)

    # Добавляем сообщение пользователя
    dialog_history.append({"role": "user", "content": user_message})

    # Ограничиваем историю последними MAX_HISTORY_PAIRS парами
    max_messages = MAX_HISTORY_PAIRS * 2
    if len(dialog_history) > max_messages:
        dialog_history = dialog_history[-max_messages:]

    messages_for_gpt = [{"role": "system", "content": system_prompt}] + dialog_history

    # ----------------- OpenAI с повторной отправкой -----------------
    answer = None
    start_time = asyncio.get_event_loop().time()
    while answer is None and (asyncio.get_event_loop().time() - start_time < 300):  # 5 минут
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: client.chat.completions.create(
                        model=config.GPT_MODEL,
                        messages=messages_for_gpt
                    )
                ),
                timeout=20.0
            )
            answer = response.choices[0].message.content
        except asyncio.TimeoutError:
            logging.error(f"[Диалог {dialog_id}] OpenAI timeout, повторная попытка...")
            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"[Диалог {dialog_id}] OpenAI error: {e}, повторная попытка...")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    logging.error(f"[Диалог {dialog_id}] OpenAI response: {e.response.text}")
                except Exception:
                    pass
            await asyncio.sleep(2)

    if answer is None:
        answer = "Скоро вернусь к вам с ответом"
        logging.error(f"[Диалог {dialog_id}] Не удалось получить ответ от OpenAI за 5 минут")

    dialog_history.append({"role": "assistant", "content": answer})

    # Снова обрезаем историю после добавления ответа
    if len(dialog_history) > max_messages:
        dialog_history = dialog_history[-max_messages:]
    await save_dialog_history(dialog_id, dialog_history)
    

    

    logging.info(f"[Диалог {dialog_id}] Пользователь: {user_message}")
    logging.info(f"[Диалог {dialog_id}] Бот: {answer}")

    # ----------------- Bitrix -----------------
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

    return JSONResponse({"status": "ok"})

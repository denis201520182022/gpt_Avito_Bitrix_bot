from flask import Flask, request, jsonify
import requests
from openai import OpenAI
import redis
import json
import config
import logging

# Настройка логирования
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    encoding="utf-8"
)

# Подключение к Redis
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

app = Flask(__name__)

# Инициализация OpenAI клиента
client = OpenAI(api_key=config.OPENAI_API_KEY)


def get_dialog_history(dialog_id):
    """Получаем историю диалога из Redis"""
    history_json = r.get(f"dialog:{dialog_id}")
    if history_json:
        return json.loads(history_json)
    return []

def save_dialog_history(dialog_id, messages):
    """Сохраняем историю диалога в Redis"""
    r.set(f"dialog:{dialog_id}", json.dumps(messages))


@app.route("/bot", methods=["POST"])
def bot_handler():
    data = request.form.to_dict()  # Битрикс шлёт form-data
    logging.info(f"RAW DATA: {data}")

    # Извлекаем данные
    user_message = data.get("data[PARAMS][MESSAGE]")
    dialog_id = data.get("data[PARAMS][DIALOG_ID]")
    user_name = data.get("data[USER][FIRST_NAME]", "клиент")  # имя пользователя для подстановки

    if not user_message or not dialog_id:
        return jsonify({"status": "no message"})

    # Подставляем имя пользователя в промт
    system_prompt = config.PROMPT.replace("{имя}", user_name)

    # Получаем историю переписки
    dialog_history = get_dialog_history(dialog_id)

    # Добавляем новое сообщение пользователя
    dialog_history.append({"role": "user", "content": user_message})

    # Формируем сообщения для OpenAI: системное + история
    messages_for_gpt = [{"role": "system", "content": system_prompt}] + dialog_history

    # GPT ответ
    response = client.chat.completions.create(
        model=config.GPT_MODEL,
        messages=messages_for_gpt
    )
    answer = response.choices[0].message.content

    # Добавляем ответ бота в историю
    dialog_history.append({"role": "assistant", "content": answer})
    save_dialog_history(dialog_id, dialog_history)

    # Лог в консоль и в файл
    print(f"[Диалог {dialog_id}] Пользователь: {user_message}")
    print(f"[Диалог {dialog_id}] Бот: {answer}")
    logging.info(f"[Диалог {dialog_id}] Пользователь: {user_message}")
    logging.info(f"[Диалог {dialog_id}] Бот: {answer}")

    # Отправка ответа в Битрикс24
    resp = requests.post(
        config.BITRIX_WEBHOOK + "imbot.message.add.json",
        data={
            "DIALOG_ID": dialog_id,
            "MESSAGE": answer,
            "BOT_ID": config.BOT_ID,
            "CLIENT_ID": config.CLIENT_ID
        }
    )

    # Логируем ответ от Bitrix
    logging.info(f"Bitrix response: {resp.status_code} {resp.text}")
    print(f"Bitrix response: {resp.status_code} {resp.text}")

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(port=5000)

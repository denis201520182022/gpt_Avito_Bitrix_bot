import os
from dotenv import load_dotenv

load_dotenv(".env.local")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")
BOT_ID = os.getenv("BOT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Добавляем прокси URL
PROXY_SERVICE_URL = os.getenv("PROXY_SERVICE_URL")

# PROMPT можно хранить прямо в .env или в файле
PROMPT_FILE = os.getenv("PROMPT_FILE")

with open(PROMPT_FILE, "r", encoding="utf-8") as f:
    PROMPT = f.read()


# Настройки обработки сообщений
MESSAGE_COLLECTION_WINDOW = float(os.getenv("MESSAGE_COLLECTION_WINDOW", "20.0"))  # секунды
MAX_COLLECTION_WINDOW = float(os.getenv("MAX_COLLECTION_WINDOW", "60.0"))        # секунды
MESSAGE_CHECK_INTERVAL = float(os.getenv("MESSAGE_CHECK_INTERVAL", "0.5"))       # секунды

# Настройки OpenAI
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))                          # секунды
OPENAI_RETRIES = int(os.getenv("OPENAI_RETRIES", "3"))                          # попытки

# Настройки поведения
COMBINE_MULTIPLE_MESSAGES = os.getenv("COMBINE_MULTIPLE_MESSAGES", "true").lower() == "true"
HUMANIZE_MODE = os.getenv("HUMANIZE_MODE", "true").lower() == "true"

# ID конкретного оператора (если нужно переводить на конкретного)
OPERATOR_ID = "22"  # или None для любого доступного

PHONE_TRANSFER_DELAY = 1800

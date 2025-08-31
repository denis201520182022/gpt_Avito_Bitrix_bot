import os
from dotenv import load_dotenv

load_dotenv(".env.prod")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")
BOT_ID = os.getenv("BOT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# PROMPT можно хранить прямо в .env или в файле
PROMPT_FILE = os.getenv("PROMPT_FILE")

with open(PROMPT_FILE, "r", encoding="utf-8") as f:
    PROMPT = f.read()

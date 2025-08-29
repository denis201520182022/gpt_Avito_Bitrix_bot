# Базовый образ с Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Скопируем зависимости
COPY requirements.txt .

# Установим зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Скопируем проект
COPY . .

# Открываем порт для FastAPI
EXPOSE 8000

# Запускаем Uvicorn сервер
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8000"]

# Образ с Python 3.11 (соответствует требованию Bothost)
FROM python:3.11-slim

# Не создавать .pyc и не буферизовать вывод (важно для логов на хостинге)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Рабочая директория приложения
WORKDIR /app

# Сначала зависимости — так слой кешируется и пересобирается только при их изменении
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем сам код
COPY . .

# Запуск бота
CMD ["python", "main.py"]

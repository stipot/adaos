FROM python:3.11-slim

WORKDIR /app

# Установим системные зависимости
RUN apt-get update && apt-get install -y git

# Установим Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем проект
COPY . .

CMD ["python", "cli.py"]

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DATA_FILE=/app/data/config.json

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py login.py check_auth.py ./

RUN mkdir -p /app/data

# Сессия и config — в volume / data (не в образе)
VOLUME ["/app/data"]

CMD ["python", "-u", "bot.py"]

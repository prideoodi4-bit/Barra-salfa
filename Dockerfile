FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN useradd --uid 10001 --create-home bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot
ENV PYTHONUNBUFFERED=1 DATABASE_PATH=/app/data/game.sqlite3
CMD ["python", "bot.py"]

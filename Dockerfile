# Lightweight, production-safe image. Runs as a non-root user; binds to $PORT so it works on
# Render (or any platform that assigns a dynamic port) without code changes.
FROM python:3.12-slim

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

USER app

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log"]

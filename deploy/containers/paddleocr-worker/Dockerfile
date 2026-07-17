FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Seoul \
    PADDLEOCR_HOME=/models/paddleocr \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --system --no-cache ".[ocr]"
COPY config ./config

CMD ["celery", "-A", "wisdome_writer", "worker", "--loglevel=INFO", "--queues=extract.ocr.paddle", "--concurrency=1"]

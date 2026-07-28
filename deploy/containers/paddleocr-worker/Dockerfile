FROM python:3.12.10-slim@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ARG UV_VERSION=0.11.26
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    TZ=Asia/Seoul \
    PADDLEOCR_HOME=/models/paddleocr \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra ocr --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --extra ocr

CMD ["celery", "-A", "wisdome_writer", "worker", "--loglevel=INFO", "--queues=extract.ocr.paddle", "--concurrency=1"]

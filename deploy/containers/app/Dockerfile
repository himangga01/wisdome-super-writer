FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Seoul

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --system --no-cache .
COPY config ./config

EXPOSE 8000
CMD ["python", "src/manage.py", "runserver", "0.0.0.0:8000"]


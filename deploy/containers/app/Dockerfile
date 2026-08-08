FROM python:3.12.10-slim@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ARG UV_VERSION=0.11.26
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    TZ=Asia/Seoul

WORKDIR /app
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml \
    ./specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml
COPY deploy/containers/app/entrypoint.sh ./deploy/containers/app/entrypoint.sh
COPY deploy/containers/hwp-worker ./deploy/containers/hwp-worker
RUN uv sync --frozen --no-dev \
    && chmod 0755 ./deploy/containers/app/entrypoint.sh

EXPOSE 8000
CMD ["/app/deploy/containers/app/entrypoint.sh"]

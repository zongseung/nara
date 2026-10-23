FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
WORKDIR /app
COPY docker/requirements-updater.lock /app/requirements-updater.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-updater.lock
COPY core /app/core
COPY src /app/src
COPY docker/prefect.yaml /app/prefect.yaml
ENV PYTHONUNBUFFERED=1 PREFECT_SERVER_ANALYTICS_ENABLED=false NARA_DATA_DIR=/data
CMD ["prefect", "worker", "start", "--pool", "g2b-price-updates", "--type", "process", "--limit", "1"]

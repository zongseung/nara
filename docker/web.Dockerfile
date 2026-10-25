FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
WORKDIR /app
COPY docker/requirements-web.lock /app/requirements-web.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-web.lock
COPY core /app/core
COPY src /app/src
RUN mkdir -p /data && chown 10001:10001 /data
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/nara PREFECT_SERVER_ANALYTICS_ENABLED=false
USER 10001:10001
CMD ["uvicorn", "src.web.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM python:3.12.14-slim
# The Render free Blueprint overrides TERRA_DB to ephemeral /tmp/terra.db.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 TERRA_DB=/data/terra.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 terra \
    && mkdir /data && chown terra:terra /data
COPY --chown=terra:terra . .
USER terra
EXPOSE 8000
# Mount durable storage at /data. Never increase the worker or replica count.
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log --log-config log_config.yaml"]

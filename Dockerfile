FROM ghcr.io/astral-sh/uv:0.10.7 AS uv
FROM python:3.12-slim AS runtime
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SWITCHBOARD_DB=/data/switchboard.sqlite SWITCHBOARD_TELEPHONY_DB=/data/telephony.sqlite
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project && \
    mkdir /data && chown 10001:10001 /data
COPY switchboard/ ./switchboard/
COPY clinics/ ./clinics/
USER 10001:10001
EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "switchboard.api:app", "--host", "0.0.0.0", "--port", "8080", "--ws-max-size", "8192", "--timeout-graceful-shutdown", "10"]

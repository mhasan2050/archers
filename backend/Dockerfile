FROM python:3.12.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser
RUN mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 8000
CMD ["archers", "api"]

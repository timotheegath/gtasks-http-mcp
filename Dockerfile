FROM ghcr.io/astral-sh/uv:0.11.16-python3.12-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY server.py google_client.py auth.py ./
RUN useradd --uid 1001 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER 1001
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "server.py"]

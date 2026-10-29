FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NUTRIENT2API_HOST=0.0.0.0 \
    NUTRIENT2API_PORT=8787 \
    NUTRIENT2API_DATA_DIR=/app/data

WORKDIR /app
COPY --chown=10001:10001 nutrient2api/ ./nutrient2api/
RUN mkdir -p /app/data && chown 10001:10001 /app/data && chmod 700 /app/data
USER 10001:10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).close()"

CMD ["python", "-m", "nutrient2api"]

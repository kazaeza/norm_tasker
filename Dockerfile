FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    GOOGLE_APPLICATION_CREDENTIALS=/data/service-account.json

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Настройки, ключ Google, база и кэш календаря лежат в /data: это папка data рядом с compose-файлом
# или том (volume), подключённый к /data на Railway. Строки VOLUME здесь нет намеренно —
# Railway отклоняет сборку с ней.

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "norm_tasker", "health"]

CMD ["python", "-m", "norm_tasker", "run"]

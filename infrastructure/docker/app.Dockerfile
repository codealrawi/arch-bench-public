# Один образ для ВСЕХ Python-компонентов всех четырёх архитектур (протокол §1): одинаковые
# Python, зависимости (lock-файл с хешами) и код. Компонент выбирается переменными
# APP_FACTORY / APP_PORT / APP_WORKERS (entrypoint.sh); миграции — командой database.manage.
# Резервные копии снимаются в контейнере postgres:16 (pg_dump той же версии, что сервер).
#
#   docker build -f infrastructure/docker/app.Dockerfile -t arch-bench-app:2.0.0 .
# На сервере PYTHON_IMAGE задаётся ссылкой по digest (RECORDED в meta.json).
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY apps /app/apps
COPY serverless /app/serverless
COPY database /app/database
COPY experiments/__init__.py experiments/config.py /app/experiments/
COPY experiments/configs /app/experiments/configs
COPY infrastructure/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY infrastructure/docker/healthcheck.py /usr/local/bin/healthcheck.py

RUN chmod 0755 /usr/local/bin/entrypoint.sh \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "/usr/local/bin/healthcheck.py"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

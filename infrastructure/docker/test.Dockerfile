# Образ для запуска тестов в контейнере (docker-compose.test.yml): код + зависимости dev/analysis.
#   docker compose -f docker-compose.test.yml run --rm tests
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY pyproject.toml /app/
COPY apps /app/apps
COPY serverless /app/serverless
COPY database /app/database
COPY infrastructure /app/infrastructure
COPY experiments /app/experiments
COPY scripts /app/scripts
COPY tests /app/tests
COPY docker-compose*.yml /app/
RUN pip install --no-cache-dir -e ".[analysis,dev]"
CMD ["python", "-m", "pytest", "-q"]

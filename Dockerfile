FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COVERAGE_DB_PATH=/data/coverage.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单容器即可；SQLite WAL 允许换报写入与查询并发。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

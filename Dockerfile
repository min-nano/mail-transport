FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# 非 root で動かす
RUN useradd --create-home --uid 1001 appuser
USER appuser

ENV PORT=8080
EXPOSE 8080

# Cloud Run は 1 リクエスト = 1 同期。同時実行は Firestore のロックで抑止するが、
# ワーカーも 1 に固定して無駄なメモリ確保を避ける。
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 540 mailtransport.app:app

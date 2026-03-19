FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py core.py middleware.py stats.py storage.py dashboard.html ./

EXPOSE 9191

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9191/health')" || exit 1

# MinIO connection configured via environment variables:
#   MINIO_ENDPOINT  (default: minio:9000)
#   MINIO_ACCESS_KEY (default: minioadmin)
#   MINIO_SECRET_KEY (default: minioadmin)
#   MINIO_SECURE     (default: false)

ENTRYPOINT ["python", "server.py"]
CMD ["--port", "9191"]

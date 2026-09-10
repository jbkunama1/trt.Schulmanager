FROM python:3.12-slim

LABEL org.opencontainers.image.title="trt.Schulmanager" \
      org.opencontainers.image.description="Klassenbuch, Schülerverwaltung und Noten in einer App — FastAPI + SQLite"

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app.py .
COPY index.html static/index.html

ENV DB_PATH=/data/schulmanager.db \
    APP_PASSWORD=lehrer2026

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]

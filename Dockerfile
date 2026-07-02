FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R appuser:appuser /app

USER appuser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080

# Cloud Run should start the wrapper in server.py.  server.py imports the
# FastAPI app from optimize_campaigns.py and adds the dashboard overrides and
# live PPC routes.  app.py is an older alternate entrypoint.
CMD ["gunicorn", "server:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", \
     "--bind", "0.0.0.0:8080", \
     "--timeout", "3600", \
     "--log-level", "info"]

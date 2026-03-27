FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --break-system-packages

# Copy application files
COPY campaign_optimizer.py .
COPY apply_optimizations.py .
COPY fetch_campaign_ids.py .
COPY optimizer_config.json .
COPY app.py .
COPY run_optimizer.py .
COPY server.py .

# Copy templates and static files for the dashboard
COPY templates/ ./templates/
COPY static/ ./static/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV GCP_PROJECT_ID=amazon-ppc-bid-optimizer
ENV PORT=8080

# Run the full FastAPI application with uvicorn
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]

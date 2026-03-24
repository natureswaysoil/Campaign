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

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV GCP_PROJECT_ID=amazon-ppc-bid-optimizer
ENV PORT=8080

# Run the web server
CMD ["python", "server.py"]

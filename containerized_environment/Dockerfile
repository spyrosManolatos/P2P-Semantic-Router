FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH="/app/containerized_environment/config.prod.yaml"

WORKDIR /app

# Install system dependencies if required for compiling
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code, configurations and necessary model weights/datasets
COPY src/ /app/src/
COPY data/ /app/data/
COPY containerized_environment/ /app/containerized_environment/

# Expose the default XML-RPC port
EXPOSE 5000

# Default command to run (will be overridden in docker-compose)
ENTRYPOINT ["python", "containerized_environment/app.py"]

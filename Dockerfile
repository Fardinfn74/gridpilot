FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for PuLP/CBC solver
RUN apt-get update && apt-get install -y --no-install-recommends \
    coinor-cbc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# Expose port (configurable via PORT env var, default 8000)
EXPOSE 8000

# Run with uvicorn — bind all interfaces, use PORT env var
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

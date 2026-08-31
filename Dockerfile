FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for Docker cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create auth directory for token storage
RUN mkdir -p /app/auth

# Expose port (Render uses PORT env var)
EXPOSE 5001

# Start the dashboard
CMD python dashboard.py

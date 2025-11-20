FROM python:3.11-alpine

# Install system dependencies
RUN apk add --no-cache \
    ffmpeg \
    sqlite \
    gcc \
    musl-dev \
    linux-headers \
    && rm -rf /var/cache/apk/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create temp directories
RUN mkdir -p /tmp/audio_uploads /tmp/segments /tmp/yt_downloads

# Run with single worker and memory limits
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--limit-concurrency", "10", \
     "--timeout-keep-alive", "30"]
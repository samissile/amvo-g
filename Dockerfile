# Use Debian-based slim image instead of Alpine (Better FFmpeg/Network support)
FROM python:3.11-slim

# Install system dependencies including aria2
# ✅ FIXED: Combined into a single RUN command with correct cleanup
RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create temp directories
RUN mkdir -p /tmp/audio_uploads /tmp/segments /tmp/yt_downloads

# Run with hypercorn (or uvicorn as recommended previously)
CMD ["hypercorn", "app.main:app", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--keep-alive", "30"]
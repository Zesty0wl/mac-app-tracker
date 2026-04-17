FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    p7zip-full \
    cpio \
    curl \
    file \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy application files
COPY download_and_analyze.py .
COPY web_app.py .
COPY scheduler.py .
COPY enhanced_tracker.py .
COPY requirements.txt .
COPY .env .
COPY templates/ templates/
COPY static/ static/
COPY tracker/ tracker/
COPY admin/ admin/
COPY notifications/ notifications/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories
RUN mkdir -p downloads /data

# Set environment variables
ENV DB_PATH=/data/microsoft_apps_versions.db
ENV PYTHONUNBUFFERED=1

# Expose port for web app
EXPOSE 5000

# Copy entrypoint script
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

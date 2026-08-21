FROM python:3.12-slim

LABEL maintainer="flixpatrol-to-mdblist"
LABEL description="Sync FlixPatrol Top 10 lists to MDBList"

# Don't buffer Python output (important for container logs)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -u 1000 appuser

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ .

# Keep the complete default config outside the mounted /app/config directory.
# The application installs it when the config volume is empty.
COPY config/default.json /app/default.json

# Config volume
VOLUME /app/config

# Fix permissions
RUN mkdir -p /app/config && chown -R appuser:appuser /app

USER appuser

# Health check: process must be running
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import os, signal; os.kill(1, 0)" || exit 1

ENTRYPOINT ["python", "flixpatrol_to_mdblist.py"]

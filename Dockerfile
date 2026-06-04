FROM python:3.12-slim

# Set environment variables for python unbuffered output and disabling cache files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies if any are needed (none for pure python, but we keep it slim)
# Create a non-root system user and group (UID 1000, GID 1000)
RUN groupadd -g 1000 appgroup && \
    useradd -r -u 1000 -g appgroup -d /app -m appuser

# Create standard directories for configuration and maildir storage
RUN mkdir -p /config /data && \
    chown -R appuser:appgroup /config /data

WORKDIR /app

# Copy python dependency list and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the daemon script and set executable permissions
COPY sync_service.py .
RUN chmod +x sync_service.py

# Switch to the non-root user by default (can be overridden at runtime using --user or -u)
USER 1000

# Define mountpoints for volumes
VOLUME ["/config", "/data"]

# Start the sync service daemon by default
CMD ["python", "sync_service.py"]

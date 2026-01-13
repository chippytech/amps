# Stage 1: Build Stage (not strictly necessary but good practice)
FROM python:3.9-slim AS builder

WORKDIR /app

# Install system dependencies, including ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final Stage
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies (only ffmpeg is needed at runtime)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from the builder stage
COPY --from=builder /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code and config
COPY amps/ ./amps/
COPY config.yaml ./config.yaml

# Expose the port the server listens on
EXPOSE 5000

# Set the entrypoint to the Amps CLI
# This will run the Gunicorn server by default when server.debug is false
CMD ["python", "-m", "amps", "serve", "--config", "config.yaml"]

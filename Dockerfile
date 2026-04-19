FROM python:3.12-slim

LABEL maintainer="Luis Miguel Cota"
LABEL description="Engram — persistent memory for AI-assisted development"

WORKDIR /app

# Copy source
COPY src/ ./src/

# Create data directory for volume mount
RUN mkdir -p /data

# Default DB path inside container
ENV ENGRAM_DB_PATH=/data/memory.db

ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["stats"]

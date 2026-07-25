FROM python:3.12-slim

LABEL maintainer="Luis Miguel Cota"
LABEL description="Engram — persistent memory for AI-assisted development"

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY cursor-rules/ ./cursor-rules/
COPY claude-skills/ ./claude-skills/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir .

RUN mkdir -p /data
ENV ENGRAM_DB_PATH=/data/memory.db

ENTRYPOINT ["engram"]
CMD ["stats"]

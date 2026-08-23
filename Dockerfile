# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY install/wire-agents.ps1 install/wire-agents.ps1
COPY --from=frontend /app/frontend/dist frontend/dist

ENV GATEKEEP_HOST=0.0.0.0 \
    GATEKEEP_PORT=9477 \
    GATEKEEP_AGENT_HOST=0.0.0.0 \
    GATEKEEP_AGENT_PORT=9478 \
    GATEKEEP_DATA_DIR=/var/lib/gatekeep

RUN mkdir -p /var/lib/gatekeep

EXPOSE 9477 9478

WORKDIR /app/backend
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:9477/api/stats" >/dev/null || exit 1

CMD ["python", "main.py"]

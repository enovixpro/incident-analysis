# Multi-Agent DevOps Incident Analysis Suite — Hugging Face Spaces (Docker SDK).
#
# Build:  docker build -t incident-suite .
# Run:    docker run -p 7860:7860 \
#           -e ANTHROPIC_API_KEY=...   \
#           -e OPENROUTER_API_KEY=...  \
#           -e SLACK_BOT_TOKEN=...     \
#           -e JIRA_URL=... -e JIRA_EMAIL=... -e JIRA_API_TOKEN=... -e JIRA_PROJECT_KEY=... \
#           incident-suite
#
# HF Spaces convention: container listens on 7860 and the runtime user is "user" (UID 1000).

FROM python:3.12-slim

# Build deps for any wheels that need compilation (chromadb pulls onnxruntime which has prebuilt
# wheels for linux/amd64, but build-essential keeps us safe across architectures).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user expected by HF Spaces.
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:${PATH}"

WORKDIR /app

# Install Python deps in their own layer for caching.
COPY --chown=user:user requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy source.
COPY --chown=user:user . .

# Pre-seed Chroma at build time so first request doesn't pay the seed cost.
# Idempotent — safe if you ever rebuild.
RUN python -m src.tools.seed_vectorstore

# Quiet LangSmith if no key is configured at runtime — keeps the logs readable.
ENV LANGSMITH_TRACING=""

EXPOSE 7860
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "7860"]

# SmartMemory MCP server for Docker's MCP Catalog / Toolkit.
# Lite mode by default: in-process store, no external services, no keys.
# Set SMARTMEMORY_MCP_MODE=remote plus SMARTMEMORY_API_KEY for cloud mode.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    SMARTMEMORY_MCP_MODE=local \
    HOME=/data \
    HF_HOME=/opt/hf

WORKDIR /app
COPY pyproject.toml README.md ./
COPY smartmemory_mcp ./smartmemory_mcp
RUN pip install smartmemory
RUN python -m spacy download en_core_web_sm
RUN python -c "from huggingface_hub import snapshot_download as d; d('sentence-transformers/all-MiniLM-L6-v2', allow_patterns=['onnx/model.onnx', '*.json', '*.txt'])" \
    && chmod -R a+rX /opt/hf
RUN pip install . \
    && useradd --create-home --home-dir /data --uid 10001 smartmemory \
    && chown -R smartmemory /data

USER smartmemory
VOLUME /data
# stdio transport: the MCP Toolkit gateway talks to the container over stdin/stdout.
ENTRYPOINT ["smartmemory-mcp"]

# SmartMemory MCP server for Docker's MCP Catalog / Toolkit.
# Lite mode by default: in-process store, no external services, no keys.
# Set SMARTMEMORY_MCP_MODE=remote plus SMARTMEMORY_API_KEY for cloud mode.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SMARTMEMORY_MCP_MODE=local \
    HOME=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY smartmemory_mcp ./smartmemory_mcp
RUN pip install . smartmemory \
    && useradd --create-home --home-dir /data --uid 10001 smartmemory \
    && chown -R smartmemory /data

USER smartmemory
VOLUME /data
# stdio transport: the MCP Toolkit gateway talks to the container over stdin/stdout.
ENTRYPOINT ["smartmemory-mcp"]

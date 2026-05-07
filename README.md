# smart-memory-mcp

Unified SmartMemory MCP (Model Context Protocol) server — tiered tools, local + remote backends.

## Overview

MCP server exposing SmartMemory operations to MCP-compatible clients (Claude Desktop, Cursor, etc.). Implements the full memory toolset (add, search, recall, decisions, plans, anchors, code-index) and routes to either a local SmartMemory instance or a remote `smart-memory-service` API endpoint.

## Status

**Version:** 0.2.1

## Quick start

```bash
pip install -e .

# Run the server
smartmemory-mcp        # or python -m smartmemory_mcp
```

Tests:

```bash
pytest tests/ -v
```

## Documentation

Full SmartMemory documentation: https://docs.smartmemory.ai

## Part of SmartMemory

This is one component of the SmartMemory ecosystem. See the [main repo](https://github.com/smart-memory/smart-memory) for the broader project.

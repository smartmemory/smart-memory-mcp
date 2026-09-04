#!/usr/bin/env python3
"""Measure unauthenticated hosted MCP and invalid-authorize request handling."""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from dataclasses import dataclass

import httpx

DEFAULT_BASE = "https://mcp.smartmemory.ai"


@dataclass(frozen=True)
class Sample:
    status: str
    latency_ms: float


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


async def _issue_request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    samples: list[Sample],
    *,
    method: str,
    path: str,
    json: dict[str, object] | None = None,
) -> None:
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.request(method, path, json=json)
            status = str(response.status_code)
        except httpx.HTTPError:
            status = "transport_error"
        samples.append(
            Sample(status=status, latency_ms=(time.perf_counter() - started) * 1000)
        )


async def _run(base: str, concurrency: int, requests_per_endpoint: int) -> list[Sample]:
    semaphore = asyncio.Semaphore(concurrency)
    samples: list[Sample] = []
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "hosted-abuse-probe", "version": "1"},
        },
    }
    bad_authorize = "/authorize?response_type=token&client_id=invalid&redirect_uri="
    bad_authorize += "http%3A%2F%2F127.0.0.1%3A1%2Finvalid&state=bad"
    limits = httpx.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency
    )
    async with httpx.AsyncClient(base_url=base, timeout=30.0, limits=limits) as client:
        async with asyncio.TaskGroup() as group:
            for _ in range(requests_per_endpoint):
                group.create_task(
                    _issue_request(
                        client,
                        semaphore,
                        samples,
                        method="POST",
                        path="/mcp",
                        json=initialize,
                    )
                )
                group.create_task(
                    _issue_request(
                        client,
                        semaphore,
                        samples,
                        method="GET",
                        path=bad_authorize,
                    )
                )
    return samples


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help="Hosted MCP public base URL"
    )
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument(
        "--requests",
        type=int,
        default=500,
        help="Requests to send to each endpoint (total requests are twice this value)",
    )
    args = parser.parse_args()
    if args.concurrency < 1 or args.requests < 1:
        parser.error("--concurrency and --requests must both be positive")
    return args


def main() -> int:
    args = _parse_args()
    samples = asyncio.run(_run(args.base.rstrip("/"), args.concurrency, args.requests))
    statuses = Counter(sample.status for sample in samples)
    latencies = [sample.latency_ms for sample in samples]

    print(f"Requests completed: {len(samples)}")
    print("Status histogram:")
    for status, count in sorted(statuses.items()):
        print(f"  {status}: {count}")
    print(f"Latency p50: {_percentile(latencies, 0.50):.1f} ms")
    print(f"Latency p95: {_percentile(latencies, 0.95):.1f} ms")

    has_5xx = any(status.isdigit() and 500 <= int(status) < 600 for status in statuses)
    if has_5xx:
        print("Probe failed: one or more requests returned 5xx status codes.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

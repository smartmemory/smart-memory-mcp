#!/usr/bin/env python3
"""Exercise the static-client OAuth + MCP path used by Grok web and Grok Bot.

Run this against a reachable hosted MCP deployment. The script deliberately
uses only httpx and the Python standard library: it is a protocol probe, not a
FastMCP client.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

DEFAULT_BASE = "https://mcp.smartmemory.ai"
DEFAULT_PORT = 8765
CALLBACK_PATH = "/callback"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _required_string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"OAuth metadata has no usable {key!r}.")
    return value


def _scope_from_metadata(metadata: dict[str, Any]) -> str:
    scopes = metadata.get("scopes_supported")
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) for scope in scopes
    ):
        raise RuntimeError("OAuth metadata has no usable 'scopes_supported' list.")
    return " ".join(scopes)


class _CallbackServer(HTTPServer):
    authorization_code: str | None = None
    error: str | None = None
    expected_state: str


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return

        params = parse_qs(parsed.query)
        state = params.get("state", [None])[0]
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]
        if state != self.server.expected_state:
            self.server.error = "OAuth callback state did not match."
        elif error:
            description = params.get("error_description", [""])[0]
            self.server.error = (
                f"OAuth authorization failed: {error} {description}".strip()
            )
        elif not code:
            self.server.error = "OAuth callback had neither code nor error."
        else:
            self.server.authorization_code = code

        body = (
            "Authorization received. You may return to the terminal."
            if self.server.authorization_code
            else "Authorization could not be completed. Check the terminal."
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _wait_for_callback(port: int, state: str) -> str:
    server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    server.expected_state = state
    print(
        f"Waiting for the OAuth callback on http://127.0.0.1:{port}{CALLBACK_PATH} ..."
    )
    try:
        while server.authorization_code is None and server.error is None:
            server.handle_request()
    finally:
        server.server_close()
    if server.error:
        raise RuntimeError(server.error)
    assert server.authorization_code is not None
    return server.authorization_code


def _post_mcp(
    client: httpx.Client,
    mcp_url: str,
    access_token: str,
    payload: dict[str, Any],
    session_id: str | None = None,
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    response = client.post(mcp_url, headers=headers, json=payload)
    response.raise_for_status()
    return response


def _decode_mcp_body(response: httpx.Response) -> dict[str, Any]:
    """Streamable HTTP may answer JSON or a text/event-stream with one data: line."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.json()
    last: dict[str, Any] | None = None
    for line in response.text.splitlines():
        if line.startswith("data:"):
            candidate = line[5:].strip()
            if candidate:
                last = json.loads(candidate)
    if last is None:
        raise RuntimeError("MCP SSE response carried no data: frame.")
    return last


def _mcp_tool_count(client: httpx.Client, base: str, access_token: str) -> int:
    mcp_url = f"{base}/mcp"
    initialized = _post_mcp(
        client,
        mcp_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "oauth-static-client-probe", "version": "1"},
            },
        },
    )
    session_id = initialized.headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("MCP initialize response did not provide mcp-session-id.")
    _post_mcp(
        client,
        mcp_url,
        access_token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id,
    )
    response = _post_mcp(
        client,
        mcp_url,
        access_token,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        session_id,
    )
    body = _decode_mcp_body(response)
    if "error" in body:
        raise RuntimeError(
            f"MCP tools/list returned an error: {json.dumps(body['error'])}"
        )
    tools = body.get("result", {}).get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("MCP tools/list response had no tools list.")
    return len(tools)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help="Hosted MCP public base URL"
    )
    parser.add_argument(
        "--client-id", required=True, help="Pre-registered public OAuth client ID"
    )
    parser.add_argument(
        "--port", default=DEFAULT_PORT, type=int, help="Loopback callback port"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    base = args.base.rstrip("/")
    redirect_uri = f"http://127.0.0.1:{args.port}{CALLBACK_PATH}"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        metadata_response = client.get(f"{base}/.well-known/oauth-authorization-server")
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if not isinstance(metadata, dict):
            raise RuntimeError("OAuth metadata response was not a JSON object.")
        authorization_endpoint = _required_string(metadata, "authorization_endpoint")
        token_endpoint = _required_string(metadata, "token_endpoint")
        scope = _scope_from_metadata(metadata)

        authorize_url = f"{authorization_endpoint}?{
            urlencode(
                {
                    'response_type': 'code',
                    'client_id': args.client_id,
                    'redirect_uri': redirect_uri,
                    'scope': scope,
                    'state': state,
                    'code_challenge': challenge,
                    'code_challenge_method': 'S256',
                    'resource': f'{base}/mcp',
                }
            )
        }"
        print("Open this URL in a browser and complete consent:\n")
        print(authorize_url)
        code = _wait_for_callback(args.port, state)

        token_response = client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": args.client_id,
                "code_verifier": verifier,
            },
        )
        token_response.raise_for_status()
        tokens = token_response.json()
        access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Token exchange did not return an access_token.")

        print(
            f"MCP tools/list returned {_mcp_tool_count(client, base, access_token)} tools."
        )

        refresh_token = (
            tokens.get("refresh_token") if isinstance(tokens, dict) else None
        )
        if isinstance(refresh_token, str) and refresh_token:
            refresh_response = client.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": args.client_id,
                },
            )
            refresh_response.raise_for_status()
            refreshed = refresh_response.json()
            has_access_token = isinstance(refreshed, dict) and bool(
                refreshed.get("access_token")
            )
            print(f"Refresh returned a new access token: {has_access_token}.")
        else:
            print("No refresh_token was issued; refresh probe skipped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, OSError, RuntimeError) as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

"""Contract-oriented external provider and MCP neighbors for real-process tests."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import ssl
from collections import Counter
from typing import Any

from aiohttp import web

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class Fixtures:
    def __init__(self) -> None:
        self.provider_calls: Counter[str] = Counter()
        self.mcp_calls: Counter[str] = Counter()
        self.sessions: set[str] = set()
        self.session_sequence = 0
        self.peer_calls: Counter[str] = Counter()
        self.peer_tasks: dict[str, dict[str, Any]] = {}

    async def provider(self, request: web.Request) -> web.Response:
        if request.content_type != "application/json" or request.headers.get("X-MASI-Provider-Profile") != "analysis-provider/v1":
            return web.Response(status=415)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.Response(status=400)
        if not isinstance(body, dict) or body.get("schema_version") != "masi-analysis-provider-request/v1":
            return web.Response(status=400)
        phase = str(body.get("phase"))
        content = str(body.get("untrusted_content_request", ""))
        self.provider_calls[phase] += 1
        if "provider-delay-200ms" in content:
            await asyncio.sleep(0.2)
        if "provider-timeout" in content:
            await asyncio.sleep(7)
        if "malformed-provider" in content:
            return web.Response(body=b"{bad", content_type="application/json")
        tool_requested = phase == "hypothesis" and "needs-tool" in content
        invalid_tool_target = phase == "hypothesis" and "invalid-tool-target" in content
        tool_timeout = phase == "hypothesis" and "tool-timeout" in content
        peer_requested = phase == "hypothesis" and "needs-peer" in content
        unsafe = "unsafe-output" in content
        insufficient = "insufficient" in content
        no_metadata = "missing-explanation" in content
        response: dict[str, Any] = {
            "schema_version": "masi-analysis-provider-response/v1",
            "request_id": body.get("request_id"),
            "input_digest": body.get("input_digest"),
            "outcome_hint": "insufficient_evidence" if insufficient else "succeeded",
            "observed_claims": (
                []
                if insufficient
                else [
                    {
                        "claim": "execute P4Runtime now" if unsafe else "Authorized evidence was observed.",
                        "evidence_refs": ["evidence-1"],
                    }
                ]
            ),
            "model_result_facts": [] if insufficient else [{"claim": "The frozen model result is an input fact.", "evidence_refs": ["evidence-1"]}],
            "model_explanation_facts": [] if insufficient else [{"claim": "TreeSHAP shows a qualified association.", "evidence_refs": ["explanation-1"]}],
            "inferred_claims": [] if insufficient else ["Further investigation may be useful."],
            "uncertainties": ["Evidence coverage is bounded."],
            "limitations": ["Association is not causality."],
            "missing_evidence": ["Independent outcome evidence remains desirable."],
            "recommendations": ["Review current Go-owned facts before preparing a separate proposal."],
            "generated_content": ["Bounded incident analysis draft."],
            "tool_requests": [],
            "delegations": [{"peer_id": "fixture-peer", "question": "Provide bounded peer context"}] if peer_requested else [],
            "response_digest": DIGEST_A,
        }
        if tool_requested and not no_metadata:
            response["tool_requests"] = [{"kind": "tool", "name": "masi.evidence.get", "arguments": {"evidence_id": "explanation-1"}}]
        elif invalid_tool_target:
            response["tool_requests"] = [{"kind": "tool", "name": "masi.events.get", "arguments": {"event_id": "outside-frozen-input"}}]
        elif tool_timeout:
            response["tool_requests"] = [{"kind": "tool", "name": "masi.targets.get", "arguments": {"target_id": "target-1"}}]
        response["response_digest"] = canonical_digest({key: value for key, value in response.items() if key != "response_digest"})
        return web.json_response(response)

    async def peer(self, request: web.Request) -> web.Response:
        if request.headers.get("A2A-Version") != "1.0":
            return web.Response(status=400)
        headers = {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}
        if request.method == "POST" and request.path == "/message:send":
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return web.Response(status=400)
            if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
                return web.Response(status=400)
            task_id = "peer-task-" + hashlib.sha256(str(body["message"].get("messageId", "")).encode()).hexdigest()[:24]
            message = body["message"]
            metadata = message.get("metadata") if isinstance(message, dict) else None
            outer_metadata = body.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(outer_metadata, dict):
                return web.Response(status=400)
            self.peer_tasks[task_id] = {
                "input_digest": metadata.get("inputDigest"),
                "trace_id": outer_metadata.get("traceId"),
            }
            self.peer_calls["submit"] += 1
            value = {
                "task": {
                    "id": task_id,
                    "contextId": "peer-context-1",
                    "status": {"state": "TASK_STATE_WORKING", "timestamp": "2033-05-18T03:33:20Z"},
                    "artifacts": [],
                    "history": [],
                    "metadata": {
                        "schemaVersion": "masi-analysis-peer-task/v1",
                        "pluginId": "fixture.peer.agent",
                        "bindingGeneration": 1,
                        "inputDigest": metadata.get("inputDigest"),
                        "traceId": outer_metadata.get("traceId"),
                        "reasonCode": "PEER_WORKING",
                    },
                }
            }
            return web.Response(body=json.dumps(value, separators=(",", ":")).encode(), headers=headers)
        if request.method == "GET" and request.path.startswith("/tasks/"):
            task_id = request.path.rsplit("/", 1)[-1]
            task_metadata = self.peer_tasks.get(task_id)
            if task_metadata is None:
                return web.Response(status=404)
            self.peer_calls["poll"] += 1
            artifact_data: dict[str, Any] = {
                "schema_version": "masi-analysis-peer-artifact/v1",
                "summary": "Peer analysis is untrusted bounded context.",
                "source_refs": [],
                "content_digest": DIGEST_A,
            }
            artifact_data["content_digest"] = canonical_digest({key: item for key, item in artifact_data.items() if key != "content_digest"})
            value = {
                "task": {
                    "id": task_id,
                    "contextId": "peer-context-1",
                    "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2033-05-18T03:33:21Z"},
                    "artifacts": [
                        {
                            "artifactId": "peer-artifact-1",
                            "name": "Bounded fixture peer context",
                            "description": "Untrusted read-only A2A peer Artifact.",
                            "parts": [
                                {
                                    "data": artifact_data,
                                    "mediaType": "application/json",
                                }
                            ],
                            "metadata": {"contentDigest": artifact_data["content_digest"], "nonExecutable": True},
                        }
                    ],
                    "history": [],
                    "metadata": {
                        "schemaVersion": "masi-analysis-peer-task/v1",
                        "pluginId": "fixture.peer.agent",
                        "bindingGeneration": 1,
                        "inputDigest": task_metadata["input_digest"],
                        "traceId": task_metadata["trace_id"],
                        "reasonCode": "PEER_SUCCEEDED",
                    },
                }
            }
            return web.Response(body=json.dumps(value, separators=(",", ":")).encode(), headers=headers)
        return web.Response(status=404)

    async def mcp(self, request: web.Request) -> web.Response:
        if request.headers.get("MCP-Protocol-Version") != "2025-11-25":
            return web.Response(status=400)
        if request.method == "DELETE":
            session_id = request.headers.get("MCP-Session-Id", "")
            self.sessions.discard(session_id)
            return web.Response(status=204)
        if request.method != "POST":
            return web.Response(status=405)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.Response(status=400)
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return web.Response(status=400)
        method = str(body.get("method"))
        self.mcp_calls[method] += 1
        if method == "initialize":
            self.session_sequence += 1
            session_id = f"fixture-session-{self.session_sequence}"
            self.sessions.add(session_id)
            response = web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False}},
                        "serverInfo": {"name": "fixture-mcp", "version": "1.0.0"},
                    },
                }
            )
            response.headers["MCP-Session-Id"] = session_id
            return response
        session_id = request.headers.get("MCP-Session-Id", "")
        if session_id not in self.sessions:
            return web.Response(status=400)
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/call":
            params = body.get("params")
            if isinstance(params, dict) and params.get("name") == "masi.targets.get":
                await asyncio.sleep(3)
            if not isinstance(params, dict) or params.get("name") not in {"masi.evidence.get", "masi.targets.get"}:
                return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32602, "message": "denied"}})
            structured = {
                "evidence_id": "explanation-1",
                "kind": "offline-model-explanation",
                "reference_digest": DIGEST_B,
                "explanation": {
                    "evidence_id": "explanation-1",
                    "method": "tree-shap",
                    "background_digest": DIGEST_A,
                    "model_digest": DIGEST_A,
                    "scaler_digest": DIGEST_B,
                    "sample_digest": DIGEST_B,
                    "coverage": 1.0,
                    "truncated": False,
                    "limitations": ["Association is not causality."],
                },
            }
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": "bounded explanation metadata"}],
                        "structuredContent": structured,
                        "isError": False,
                    },
                }
            )
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "method denied"}})

    async def stats(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "provider_calls": dict(self.provider_calls),
                "mcp_calls": dict(self.mcp_calls),
                "active_sessions": len(self.sessions),
                "peer_calls": dict(self.peer_calls),
            }
        )


def ssl_context(cert: str, key: str, ca: str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert, key)
    context.load_verify_locations(cafile=ca)
    return context


async def run(args: argparse.Namespace) -> None:
    fixtures = Fixtures()
    provider_app = web.Application(client_max_size=65536)
    provider_app.add_routes([web.post("/v1/analyze", fixtures.provider), web.get("/stats", fixtures.stats)])
    mcp_app = web.Application(client_max_size=65536)
    mcp_app.add_routes([web.route("*", "/mcp", fixtures.mcp), web.get("/stats", fixtures.stats)])
    peer_app = web.Application(client_max_size=65536)
    peer_app.add_routes([web.route("*", "/message:send", fixtures.peer), web.route("*", r"/tasks/{task_id}", fixtures.peer), web.get("/stats", fixtures.stats)])
    provider_runner = web.AppRunner(provider_app, access_log=None)
    mcp_runner = web.AppRunner(mcp_app, access_log=None)
    peer_runner = web.AppRunner(peer_app, access_log=None)
    await provider_runner.setup()
    await mcp_runner.setup()
    await peer_runner.setup()
    if args.plaintext:
        context = None
    else:
        if not args.cert or not args.key or not args.ca:
            raise ValueError("TLS fixture mode requires --cert, --key, and --ca")
        context = ssl_context(args.cert, args.key, args.ca)
    provider_site = web.TCPSite(provider_runner, args.listen_host, args.provider_port, ssl_context=context)
    mcp_site = web.TCPSite(mcp_runner, args.listen_host, args.mcp_port, ssl_context=context)
    peer_site = web.TCPSite(peer_runner, args.listen_host, args.peer_port, ssl_context=context)
    await provider_site.start()
    await mcp_site.start()
    await peer_site.start()
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-port", type=int, required=True)
    parser.add_argument("--mcp-port", type=int, required=True)
    parser.add_argument("--peer-port", type=int, required=True)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--ca")
    parser.add_argument("--plaintext", action="store_true")
    parser.add_argument("--listen-host", default="127.0.0.1")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

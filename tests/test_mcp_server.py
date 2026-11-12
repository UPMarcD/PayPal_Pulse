"""Automated integration tests for PayPal Pulse Model Context Protocol (MCP) server."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest


def _send_mcp_message(proc: subprocess.Popen, msg: dict[str, Any]) -> dict[str, Any]:
    """Send a single JSON-RPC message to the MCP process and read the response line."""
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, "MCP server closed stdout unexpectedly"
    return json.loads(line)


@pytest.fixture
def mcp_process():
    """Spawn an isolated MCP server process with demo mode enabled."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pulse", "mcp", "--demo"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    yield proc
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.terminate()
    proc.wait(timeout=5)


def test_mcp_initialize_and_ping(mcp_process):
    """Verify MCP protocol initialization handshake and ping response."""
    # 1. Initialize
    init_res = _send_mcp_message(
        mcp_process,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert init_res["jsonrpc"] == "2.0"
    assert init_res["id"] == 1
    assert "result" in init_res
    result = init_res["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "paypal-pulse"
    assert "tools" in result["capabilities"]

    # 2. Ping
    ping_res = _send_mcp_message(
        mcp_process,
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )
    assert ping_res["result"] == {}


def test_mcp_tools_and_collections_list(mcp_process):
    """Verify tools/list, resources/list, and prompts/list conformance."""
    # Initialize first
    _send_mcp_message(mcp_process, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    # tools/list
    tools_res = _send_mcp_message(mcp_process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = tools_res["result"]["tools"]
    tool_names = {t["name"] for t in tools}

    expected_tools = {
        "pulse_get_financial_summary",
        "pulse_get_disputes",
        "pulse_create_order",
        "pulse_create_invoice",
        "pulse_get_timeline",
        "pulse_draft_dispute_evidence",
    }
    assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"

    for tool in tools:
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"

    # resources/list & prompts/list
    res_res = _send_mcp_message(mcp_process, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert res_res["result"] == {"resources": []}

    prompt_res = _send_mcp_message(mcp_process, {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
    assert prompt_res["result"] == {"prompts": []}


def test_mcp_tool_execution_telemetry(mcp_process):
    """Verify telemetry tool execution over JSON-RPC."""
    # 1. Financial summary
    fin_res = _send_mcp_message(
        mcp_process,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "pulse_get_financial_summary", "arguments": {}},
        },
    )
    assert not fin_res["result"]["isError"]
    fin_data = json.loads(fin_res["result"]["content"][0]["text"])
    assert "gross_volume" in fin_data or "seeded" in fin_data

    # 2. Disputes
    disp_res = _send_mcp_message(
        mcp_process,
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "pulse_get_disputes", "arguments": {"only_open": True}},
        },
    )
    assert not disp_res["result"]["isError"]
    disputes = json.loads(disp_res["result"]["content"][0]["text"])
    assert isinstance(disputes, list)
    assert len(disputes) > 0
    assert disputes[0]["status"] == "OPEN"

    # 3. Timeline
    time_res = _send_mcp_message(
        mcp_process,
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "pulse_get_timeline", "arguments": {}},
        },
    )
    assert not time_res["result"]["isError"]
    timeline = json.loads(time_res["result"]["content"][0]["text"])
    assert isinstance(timeline, list)
    assert len(timeline) > 0

    # 4. Dispute evidence draft
    evidence_res = _send_mcp_message(
        mcp_process,
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "pulse_draft_dispute_evidence",
                "arguments": {"dispute_id": disputes[0]["dispute_id"]},
            },
        },
    )
    assert not evidence_res["result"]["isError"]
    ev_data = json.loads(evidence_res["result"]["content"][0]["text"])
    assert ev_data["dispute_id"] == disputes[0]["dispute_id"]
    assert "evidence_draft" in ev_data
    assert len(ev_data["evidence_draft"]) > 20


    # 5. Simulate webhook with release attribution
    wh_res = _send_mcp_message(
        mcp_process,
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "pulse_simulate_webhook",
                "arguments": {
                    "event_type": "payment.capture.completed",
                    "amount": 89.00,
                    "currency": "USD",
                    "release_version": "v3.5.0",
                },
            },
        },
    )
    assert not wh_res["result"]["isError"]
    wh_data = json.loads(wh_res["result"]["content"][0]["text"])
    assert wh_data["status"] == "INGESTED"
    assert wh_data["receipt"]["release_version"] == "v3.5.0"


def test_mcp_error_handling(mcp_process):
    """Verify error isolation for unknown tools and methods."""
    # Unknown tool
    bad_tool = _send_mcp_message(
        mcp_process,
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "pulse_nonexistent_action", "arguments": {}},
        },
    )
    assert bad_tool["result"]["isError"] is True
    assert "Unknown tool" in bad_tool["result"]["content"][0]["text"]

    # Unknown JSON-RPC method
    bad_method = _send_mcp_message(
        mcp_process,
        {"jsonrpc": "2.0", "id": 21, "method": "unsupported/method", "params": {}},
    )
    assert "error" in bad_method
    assert bad_method["error"]["code"] == -32601

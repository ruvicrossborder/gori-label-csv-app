import os
import json
import requests

# This client talks to the gori-mcp server (a working, already-proven wrapper
# around the real Gori API) via its MCP JSON-RPC endpoint, rather than calling
# api.goricompany.com directly. Direct calls to api.goricompany.com consistently
# returned 500 errors regardless of request shape; the gori-mcp server's own
# get_rates/create_shipment tools reliably succeed for the exact same inputs,
# so we reuse that proven path instead of re-guessing the raw API's schema.
GORI_MCP_URL = os.environ.get("GORI_MCP_URL", "https://gori-mcp-production.up.railway.app/mcp")


def _call_tool(name, arguments):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    resp = requests.post(
        GORI_MCP_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=60,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        data_line = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data_line = line[len("data:"):].strip()
                break
        if not data_line:
            raise RuntimeError(f"No data line in SSE response: {resp.text[:500]}")
        rpc_result = json.loads(data_line)
    else:
        rpc_result = resp.json()

    if "error" in rpc_result:
        raise RuntimeError(f"gori-mcp error: {rpc_result['error']}")

    result = rpc_result.get("result", {})
    content = result.get("content", [])
    if not content:
        raise RuntimeError(f"gori-mcp returned no content: {rpc_result}")

    text = content[0].get("text", "")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        raise RuntimeError(f"gori-mcp tool call failed: {text}")

    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"gori-mcp tool error: {parsed['error']}")

    return parsed


def _split_name(full_name):
    full_name = (full_name or "").strip()
    if not full_name:
        return "", ""
    parts = full_name.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _to_mcp_address(addr):
    """Accepts either {first_name, last_name, ...} or {name, ...} and returns
    the gori-mcp address schema (first_name/last_name)."""
    addr = dict(addr or {})
    if "first_name" not in addr and "name" in addr:
        first, last = _split_name(addr.pop("name"))
        addr["first_name"] = first
        addr["last_name"] = last
    return addr


def get_rates(to_address, from_address, parcel):
    args = {
        "to_address": _to_mcp_address(to_address),
        "from_address": _to_mcp_address(from_address),
        "parcel": parcel,
    }
    return _call_tool("get_rates", args)


def create_shipment(service, to_address, from_address, parcel, reference_1=None, reference_2=None):
    args = {
        "service": service,
        "to_address": _to_mcp_address(to_address),
        "from_address": _to_mcp_address(from_address),
        "parcel": parcel,
    }
    if reference_1:
        args["reference_1"] = reference_1
    if reference_2:
        args["reference_2"] = reference_2
    return _call_tool("create_shipment", args)

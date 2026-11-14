"""Checks 2 and 3: drive the MongoDB MCP server (Path 1, scripts/mcp-mongodb.sh) over stdio.

2. list-databases on the cluster
3. insert into holdshort.smoke_test, read it back, drop the collection
"""
import json
import subprocess
import sys
import uuid

from _env import URI_HELP, ROOT, redact, require

require("MONGODB_URI", URI_HELP)
DB = __import__("os").environ.get("MONGODB_DB", "holdshort")

proc = subprocess.Popen([str(ROOT / "scripts" / "mcp-mongodb.sh")], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
_next = 0


def rpc(method, params=None):
    global _next
    _next += 1
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _next, "method": method,
                                 "params": params or {}}) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            sys.exit("FAIL: MCP server exited unexpectedly")
        msg = json.loads(line)
        if msg.get("id") == _next:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg["result"]


def tool(name, **args):
    res = rpc("tools/call", {"name": name, "arguments": args})
    text = "\n".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError"):
        raise RuntimeError(f"{name}: {text}")
    return text


ok = True
try:
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "holdshort-verify", "version": "0"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()

    # The server registers MDB_MCP_CONNECTION_STRING as connection "preconfigured".
    conns = tool("list-connections")
    if '"preconfigured"' not in conns:
        sys.exit(f"FAIL 2: MCP server has no preconfigured connection: {redact(conns[:300])}")
    CID = "preconfigured"
    try:
        out = tool("list-databases", connectionId=CID)
        print("PASS 2: list-databases ->", out.split(":")[0])
    except Exception as e:
        ok = False
        print("FAIL 2: list-databases:", redact(e))

    marker = str(uuid.uuid4())
    try:
        tool("insert-many", connectionId=CID, database=DB, collection="smoke_test",
             documents=[{"marker": marker, "kind": "smoke"}])
        found = tool("find", connectionId=CID, database=DB, collection="smoke_test", filter={"marker": marker})
        if marker not in found:
            raise RuntimeError(f"inserted doc not read back: {found[:300]}")
        tool("drop-collection", connectionId=CID, database=DB, collection="smoke_test")
        print(f"PASS 3: insert/find/drop on {DB}.smoke_test")
    except Exception as e:
        ok = False
        print("FAIL 3:", redact(e))
finally:
    proc.terminate()
    proc.wait(timeout=10)
sys.exit(0 if ok else 1)

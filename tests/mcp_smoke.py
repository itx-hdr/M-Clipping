"""A real, independent stdio MCP client. Reads app state only; never queues clips or calls an AI provider."""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def request(rid, method, params=None):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def main():
    messages = [request(0, "tools/list"),
                request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "mclipper-smoke", "version": "1"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, request(2, "tools/list"),
                request(3, "tools/call", {"name": "get_niche_brief"}),
                request(4, "tools/call", {"name": "queue_source", "arguments": {"url": "https://www.youtube.com/watch?v=mLp8LS5_EQ8"}}),
                request(5, "tools/call", {"name": "search_sources", "arguments": {"query": "Elden Ring"}}),
                request(6, "tools/call", {"name": "mclipper_status"}), request(9, "unknown/method"),
                request(10, "tools/call", {"name": "queue_source", "arguments": {"url": []}}),
                request(11, "tools/call", {"name": "edit_clip_plan", "arguments": {"job_id":"unreviewed-job","clip_id":"clip01",
                    "segments":[{"start":0,"end":5}],"framing":{},"hook":"TEST ONLY"}})]
    if len(sys.argv) == 3:
        args = {"job_id": sys.argv[1], "clip_id": sys.argv[2]}
        messages += [request(7, "tools/call", {"name": "inspect_clip", "arguments": args}),
                     request(8, "tools/call", {"name": "export_clip", "arguments": args})]
    with tempfile.TemporaryDirectory(prefix="mclipper-mcp-smoke-") as temp:
        env = {**os.environ, "MCLIPPER_MCP_DATA": temp}
        data = "{bad json}\n" + "".join(json.dumps(m) + "\n" for m in messages)
        result = subprocess.run([sys.executable, str(ROOT / "run_mclipper_mcp.py")], cwd=temp, env=env,
                                input=data, capture_output=True, text=True, encoding="utf-8", timeout=90)
        assert result.returncode == 0, result.stderr
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        by_id = {r["id"]: r for r in replies}
        assert len(replies) == sum("id" in m for m in messages) + 1
        assert by_id[None]["error"]["code"] == -32700
        assert by_id[0]["error"]["code"] == -32000
        assert by_id[1]["result"]["protocolVersion"] == "2025-11-25"
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        required = {"get_niche_brief", "mclipper_status", "search_sources", "sample_source", "review_source", "queue_source",
                    "get_job", "inspect_clip", "render_clip", "edit_clip_plan", "review_clip", "export_clip"}
        assert required <= names, names
        brief = json.loads(by_id[3]["result"]["content"][0]["text"])
        assert brief["game"] == "Grand Theft Auto VI" and brief["language"] == "English"
        assert by_id[4]["result"]["isError"] and by_id[5]["result"]["isError"]
        assert by_id[11]["result"]["isError"], "Montage editing must not bypass the source review"
        assert by_id[9]["error"]["code"] == -32601 and by_id[10]["error"]["code"] == -32602
        status = by_id[6]["result"]
        assert not status["isError"], status
        ai = json.loads(status["content"][0]["text"])["ai"]
        assert "key" not in ai and "key_hint" not in ai
        print(json.dumps({"protocol": "initialized", "tools": sorted(names), "niche": brief["game"],
                          "unreviewed_source_blocked": True, "off_niche_search_blocked": True,
                          "invalid_arguments_rejected": True, "local_api": "reachable", "ai_ready": ai["ready"]}))
        if 7 in by_id:
            media = by_id[7]["result"]
            assert not media["isError"], media
            kinds = [b["type"] for b in media["content"]]
            assert kinds.count("image") == 5 and kinds.count("audio") == 1, kinds
            for block in media["content"]:
                if block["type"] == "image": assert base64.b64decode(block["data"]).startswith(b"\xff\xd8")
                if block["type"] == "audio": assert base64.b64decode(block["data"]).startswith(b"RIFF")
            assert by_id[8]["result"]["isError"], "An old job without a GTA VI source review must not export"
            print(json.dumps({"media_contract": kinds, "old_unreviewed_job_export_blocked": True}))


if __name__ == "__main__":
    main()

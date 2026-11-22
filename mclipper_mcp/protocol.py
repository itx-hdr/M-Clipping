"""The MCP stdio tools transport: UTF-8 JSON-RPC messages, one per line.

Only tools are advertised. This bridge makes no sampling, task, remote HTTP or cancellation capability claims.
Specification: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
"""
import base64
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import get_type_hints

from pydantic import ConfigDict, ValidationError, create_model

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class TextContent(dict):
    def __init__(self, type="text", text=""):
        super().__init__(type=type, text=text)


class CallToolResult(dict):
    def __init__(self, content, isError=False):
        super().__init__(content=content, isError=isError)


class Image:
    def __init__(self, path):
        self.path = Path(path)

    def to_image_content(self):
        return {"type": "image", "data": base64.b64encode(self.path.read_bytes()).decode("ascii"), "mimeType": "image/jpeg"}


class Audio:
    def __init__(self, data, format="wav"):
        self.data, self.format = data, format

    def to_audio_content(self):
        return {"type": "audio", "data": base64.b64encode(self.data).decode("ascii"), "mimeType": "audio/" + self.format}


class Server:
    def __init__(self, name, instructions=""):
        self.name, self.instructions = name, instructions
        self.tools = {}
        self.initialized = False

    def tool(self):
        def register(fn):
            hints = get_type_hints(fn)
            fields = {name: (hints.get(name, str), ... if p.default is inspect.Parameter.empty else p.default)
                      for name, p in inspect.signature(fn).parameters.items()}
            model = create_model(fn.__name__ + "Input", __config__=ConfigDict(extra="forbid", strict=True), **fields)
            self.tools[fn.__name__] = (fn, model)
            return fn
        return register

    @staticmethod
    def error(rid, code, text):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self.error(None, -32600, "Invalid JSON-RPC request")
        method, rid = message["method"], message.get("id")
        if "id" not in message:
            # Notifications have no response. The tool operations themselves are bounded by timeouts.
            return None
        if not isinstance(rid, (int, str)) or isinstance(rid, bool):
            return self.error(None, -32600, "Invalid request identifier")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self.error(rid, -32602, "Parameters must be an object")
        if method == "initialize":
            if self.initialized or not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                return self.error(rid, -32602, "Expected protocolVersion, capabilities and clientInfo")
            self.initialized = True
            version = params["protocolVersion"] if params["protocolVersion"] in VERSIONS else VERSIONS[0]
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": self.name, "version": "0.1.0"}, "instructions": self.instructions}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return self.error(rid, -32000, "Initialize the connection first")
        elif method == "tools/list":
            if params.get("cursor"):
                return self.error(rid, -32602, "There are no more tool pages")
            result = {"tools": [{"name": name, "description": inspect.getdoc(fn) or "", "inputSchema": model.model_json_schema()}
                                for name, (fn, model) in self.tools.items()]}
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                return self.error(rid, -32602, "Unknown tool")
            fn, model = self.tools[name]
            try:
                args = model.model_validate(params.get("arguments", {}))
            except ValidationError:
                return self.error(rid, -32602, "Arguments do not match the tool's input schema")
            try:
                value = fn(**{key: getattr(args, key) for key in model.model_fields})
                result = value if isinstance(value, CallToolResult) else CallToolResult([TextContent(text=json.dumps(value, ensure_ascii=False, allow_nan=False))])
                if isinstance(value, dict) and not isinstance(value, CallToolResult):
                    result["structuredContent"] = value
            except ValueError as e:
                result = CallToolResult([TextContent(text=str(e))], isError=True)
            except Exception:
                logging.exception("M clipper MCP tool failed: %s", name)
                result = CallToolResult([TextContent(text="The M clipper operation failed. Inspect the local log before retrying.")], isError=True)
        else:
            return self.error(rid, -32601, "Method not supported")
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def run(self, transport="stdio"):
        if transport != "stdio":
            raise ValueError("This local bridge supports stdio only.")
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        maximum = 2 * 1024 * 1024
        while True:
            line = sys.stdin.buffer.readline(maximum + 1)
            if not line:
                break
            if len(line) > maximum:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(maximum + 1)
                response = self.error(None, -32600, "Request is too large")
            else:
                try:
                    message = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    response = self.dispatch(message)
                except (ValueError, UnicodeError):
                    response = self.error(None, -32700, "Invalid JSON")
            if response is not None:
                sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()

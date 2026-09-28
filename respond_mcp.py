"""Read-only client for respond.io's hosted MCP server (https://mcp.respond.io/mcp).

Auth: OAuth 2.1 + PKCE, public client (no secret). `python respond_mcp.py login`
does the one-time browser sign-in and saves a refresh token to token.json.
Only READ scopes are requested, so the token itself cannot send messages,
edit contacts or change conversations - even if a write tool is called by mistake.

Other commands:
  python respond_mcp.py tools             # list tools the server exposes
  python respond_mcp.py call <tool> '{json args}'
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.json")
CLIENT_FILE = os.path.join(HERE, "oauth_client.json")

MCP_URL = "https://mcp.respond.io/mcp"
AUTH_BASE = "https://oauth.respond.io"
REDIRECT_PORT = 8791
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

# Read-only. Deliberately excludes contacts:write, conversations:write,
# comments:write, messages:send, tags:write, space:write, ai_agents:write.
SCOPES = [
    "contacts:read",
    "conversations:read",
    "channels:read",
    "user:read",
    "space:read",
]


def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def _load(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------- OAuth

def _register_client():
    client = _load(CLIENT_FILE)
    if client:
        return client
    r = requests.post(f"{AUTH_BASE}/reg", json={
        "client_name": "Fetch CS Performance Dashboard (read-only)",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(SCOPES),
    }, timeout=30)
    r.raise_for_status()
    client = r.json()
    _save(CLIENT_FILE, client)
    return client


def login():
    client = _register_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": MCP_URL,
    }
    url = f"{AUTH_BASE}/as/auth?" + urllib.parse.urlencode(params)

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Connected - you can close this tab." if "code" in q else \
                f"Sign-in failed: {q.get('error_description', q.get('error', ['unknown']))[0]}"
            self.wfile.write(f"<h2>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    print("Opening browser for respond.io sign-in (read-only permissions)...")
    print("If it doesn't open, visit:\n", url)
    webbrowser.open(url)
    while not result:
        server.handle_request()

    if "code" not in result:
        sys.exit(f"Sign-in failed: {result}")
    if result.get("state") != state:
        sys.exit("State mismatch - aborting.")

    r = requests.post(f"{AUTH_BASE}/token", data={
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client["client_id"],
        "code_verifier": verifier,
        "resource": MCP_URL,
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    tok["obtained_at"] = int(time.time())
    _save(TOKEN_FILE, tok)
    print("Saved token. Granted scopes:", tok.get("scope", "(not reported)"))


def _access_token():
    tok = _load(TOKEN_FILE)
    if not tok:
        sys.exit("Not signed in - run: python respond_mcp.py login")
    expires = tok["obtained_at"] + int(tok.get("expires_in", 3600)) - 120
    if time.time() < expires:
        return tok["access_token"]
    client = _load(CLIENT_FILE)
    r = requests.post(f"{AUTH_BASE}/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": client["client_id"],
        "resource": MCP_URL,
    }, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"Token refresh failed ({r.status_code}): {r.text}\n"
                 "Re-run: python respond_mcp.py login")
    new = r.json()
    new.setdefault("refresh_token", tok["refresh_token"])  # rotation-safe
    new["obtained_at"] = int(time.time())
    _save(TOKEN_FILE, new)
    return new["access_token"]


# ---------------------------------------------------------------- MCP (streamable HTTP)

class MCP:
    def __init__(self):
        self.s = requests.Session()
        self.session_id = None
        self._id = 0
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fetch-cs-dashboard", "version": "1.0"},
        })
        self._notify("notifications/initialized")

    def _headers(self):
        h = {
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(resp):
        body = resp.content.decode("utf-8")  # SSE has no charset header; requests guesses latin-1
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            # One event may span several `data:` lines; events end with a blank line.
            data, buf = None, []
            for line in body.splitlines() + [""]:
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
                elif line == "" and buf:
                    data = json.loads("\n".join(buf))
                    buf = []
            return data
        return json.loads(body)

    def _notify(self, method, params=None):
        self.s.post(MCP_URL, headers=self._headers(),
                    json={"jsonrpc": "2.0", "method": method, "params": params or {}},
                    timeout=30)

    def _rpc(self, method, params=None):
        self._id += 1
        r = self.s.post(MCP_URL, headers=self._headers(), timeout=120, json={
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if r.status_code >= 400:
            raise RuntimeError(f"{method} -> HTTP {r.status_code}: {r.text[:500]}")
        self.session_id = r.headers.get("Mcp-Session-Id", self.session_id)
        msg = self._parse(r)
        if msg and "error" in msg:
            raise RuntimeError(f"{method} -> {msg['error']}")
        return msg["result"] if msg else None

    def tools(self):
        out, cursor = [], None
        while True:
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            out += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def call(self, name, args=None):
        res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        if res.get("isError"):
            raise RuntimeError(f"{name}: {res.get('content')}")
        if "structuredContent" in res:
            return res["structuredContent"]
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tools"
    if cmd == "login":
        login()
    elif cmd == "tools":
        for t in MCP().tools():
            props = list((t.get("inputSchema") or {}).get("properties", {}).keys())
            print(f"- {t['name']}({', '.join(props)}): {(t.get('description') or '').splitlines()[0][:140]}")
    elif cmd == "call":
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(MCP().call(sys.argv[2], args), indent=2, default=str)[:20000])
    else:
        sys.exit(__doc__)

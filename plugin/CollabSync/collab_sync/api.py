"""REST client for freecad-collab-server (standard library only)."""

import json
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Api:
    def __init__(self, base_url, token, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def ws_url(self, document_id):
        u = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        return urllib.parse.urlunparse((scheme, u.netloc, f"{u.path}/v1/documents/{document_id}/ws", "", "", ""))

    def request(self, method, path, data=None, content_type=None):
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        status, _, raw = self.request(method, path, data, "application/json" if data else None)
        if status >= 400:
            try:
                message = json.loads(raw).get("message", raw.decode())
            except ValueError:
                message = raw.decode("utf-8", "replace")
            raise ApiError(status, message)
        return json.loads(raw) if raw else None

    def me(self):
        return self.call("GET", "/v1/me")

    def list_documents(self):
        return self.call("GET", "/v1/documents")

    def create_document(self, name, description=None):
        return self.call("POST", "/v1/documents", {"name": name, "description": description})

    def set_member(self, document_id, user_name, role):
        return self.call("PUT", f"/v1/documents/{document_id}/members", {"user_name": user_name, "role": role})

    def get_snapshot(self, document_id):
        """(seq, bytes) of the latest snapshot, or None if there is none."""
        status, headers, raw = self.request("GET", f"/v1/documents/{document_id}/snapshot")
        if status == 404:
            try:
                if json.loads(raw).get("message", "").startswith("snapshot"):
                    return None
            except ValueError:
                pass
            raise ApiError(404, "document not found")
        if status >= 400:
            raise ApiError(status, raw.decode("utf-8", "replace"))
        seq = next((v for k, v in headers.items() if k.lower() == "x-snapshot-seq"), "0")
        return int(seq), raw

    def put_snapshot(self, document_id, seq, data):
        status, _, raw = self.request(
            "PUT", f"/v1/documents/{document_id}/snapshot?seq={int(seq)}", data, "application/zip"
        )
        if status >= 400:
            raise ApiError(status, raw.decode("utf-8", "replace"))
        return json.loads(raw)

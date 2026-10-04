"""Minimal RFC 6455 WebSocket client using only the standard library.

FreeCAD ships its own Python without third-party WebSocket packages, so the
plugin carries this small client instead of depending on pip installs.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 64 * 1024 * 1024
READ_TIMEOUT = 90  # server pings every 20 s by default


class HandshakeError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _xor(data, mask):
    n = len(data)
    if n == 0:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


class WebSocket:
    def __init__(self, url, headers=None, timeout=10):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError(f"unsupported scheme {u.scheme!r}")
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)

        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        host = u.hostname + (f":{u.port}" if u.port else "")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ] + [f"{k}: {v}" for k, v in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during handshake")
            resp += chunk
            if len(resp) > 65536:
                raise ConnectionError("handshake response too large")
        head, self._buf = resp.split(b"\r\n\r\n", 1)
        head_lines = head.decode("latin-1").split("\r\n")
        parts = head_lines[0].split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if status != 101:
            body = self._buf.decode("utf-8", "replace")[:300]
            sock.close()
            raise HandshakeError(status, body or head_lines[0])
        hdrs = {}
        for line in head_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if hdrs.get("sec-websocket-accept") != expected:
            sock.close()
            raise ConnectionError("invalid Sec-WebSocket-Accept")

        sock.settimeout(READ_TIMEOUT)
        self.sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        with self._send_lock:
            self.sock.sendall(bytes(header) + mask + _xor(payload, mask))

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self):
        """Return ("text", str) or ("close", code, reason)."""
        frags, first_op = [], None
        while True:
            b1, b2 = self._recv_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            if n > MAX_FRAME:
                raise ConnectionError("frame too large")
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n)
            if mask:
                data = _xor(data, mask)

            if op == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x8:  # close
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                reason = data[2:].decode("utf-8", "replace")
                self.close(code)
                return ("close", code, reason)
            if op in (0x1, 0x2):
                first_op, frags = op, [data]
            elif op == 0x0:
                frags.append(data)
            if fin and first_op is not None:
                payload = b"".join(frags)
                if first_op == 0x1:
                    return ("text", payload.decode("utf-8"))
                frags, first_op = [], None

    def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(0x8, struct.pack("!H", code))
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# Handshake status -> synthetic close code understood by the session.
HANDSHAKE_CODES = {401: 4401, 403: 4401, 404: 4404}


class Connection(threading.Thread):
    """Background reader. Posts (generation, kind, payload) tuples to `inbox`:
    ("open", None), ("msg", dict), ("closed", (code, reason))."""

    def __init__(self, url, token, hello, inbox, generation):
        super().__init__(daemon=True, name="collab-ws")
        self.url, self.token, self.hello = url, token, hello
        self.inbox, self.generation = inbox, generation
        self.ws = None
        self._stopping = False

    def _post(self, kind, payload=None):
        self.inbox.put((self.generation, kind, payload))

    def run(self):
        try:
            self.ws = WebSocket(self.url, {"Authorization": f"Bearer {self.token}"})
            if self._stopping:
                self.ws.close()
                return
            self.ws.send_text(json.dumps(self.hello))
            self._post("open")
            while True:
                item = self.ws.recv()
                if item[0] == "text":
                    try:
                        self._post("msg", json.loads(item[1]))
                    except ValueError:
                        continue
                else:
                    self._post("closed", (item[1], item[2]))
                    return
        except HandshakeError as e:
            self._post("closed", (HANDSHAKE_CODES.get(e.status, 1006), str(e)))
        except Exception as e:  # network failure, timeout, ...
            if not self._stopping:
                self._post("closed", (1006, str(e) or type(e).__name__))
        finally:
            if self.ws is not None:
                self.ws.close()

    def send(self, obj):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("not connected")
        self.ws.send_text(json.dumps(obj, separators=(",", ":")))

    def stop(self):
        self._stopping = True
        if self.ws is not None:
            self.ws.close()

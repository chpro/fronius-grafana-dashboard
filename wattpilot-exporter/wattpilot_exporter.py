#!/usr/bin/env python3
"""Prometheus exporter for the Fronius Wattpilot (local WebSocket API).

The Wattpilot has no HTTP API. It pushes its status over a password protected WebSocket
(ws://<host>/ws): after the login it sends a fullStatus and then deltaStatus messages with
every change. This exporter keeps that connection open and serves the last known values on
http://<listen>/metrics for VictoriaMetrics/vmagent/Prometheus.

Only the Python standard library is used, so the script runs in a plain python:3-alpine container.

Environment:
  WATTPILOT_HOST      hostname or IP of the Wattpilot (required)
  WATTPILOT_PASSWORD  password of the Wattpilot (the one used in the Solar.wattpilot app, required)
  LISTEN_PORT         port for /metrics, default 9101

Login protocol (firmware with "hash": "pbkdf2", e.g. 42.x), see https://github.com/joscha82/wattpilot:
  hashed = base64(pbkdf2_hmac(sha512, password, serial, 100000, 256))[:32]
  hash1  = sha256(token1 + hashed)
  hash   = sha256(token3 + token2 + hash1), token3 = random
"""
import base64, hashlib, http.server, json, os, secrets, socket, struct, sys, threading, time

HOST = os.environ.get("WATTPILOT_HOST") or sys.exit("WATTPILOT_HOST is not set")
PASSWORD = os.environ.get("WATTPILOT_PASSWORD") or sys.exit("WATTPILOT_PASSWORD is not set")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9101"))
STALE_AFTER = 120  # seconds without any message -> values are no longer exported

status = {}
info = {}
state = {"authenticated": False, "last_msg": 0.0}
lock = threading.Lock()


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


# --- minimal WebSocket client (RFC 6455), text frames only -------------------------------------

class WebSocket:
    def __init__(self, host, path="/ws", timeout=60):
        self.sock = socket.create_connection((host, 80), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("connection closed during handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("handshake failed: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _send(self, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = secrets.token_bytes(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._send(0x1, text.encode())

    def recv(self):
        """Returns the next complete text message."""
        message = b""
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("closed by Wattpilot")
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if b0 & 0x80:
                    return message.decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --- Wattpilot protocol --------------------------------------------------------------------------

def run_once():
    ws = WebSocket(HOST)
    try:
        hashed = None
        while True:
            msg = json.loads(ws.recv())
            kind = msg.get("type")
            with lock:
                state["last_msg"] = time.time()
            if kind == "hello":
                with lock:
                    info.update({k: str(msg.get(k, "")) for k in ("serial", "friendly_name", "version", "devicetype")})
            elif kind == "authRequired":
                if msg.get("hash", "pbkdf2") != "pbkdf2":
                    raise RuntimeError(f"unsupported password hash {msg.get('hash')!r} (only pbkdf2)")
                if hashed is None:
                    hashed = base64.b64encode(hashlib.pbkdf2_hmac(
                        "sha512", PASSWORD.encode(), info["serial"].encode(), 100000, 256))[:32]
                token3 = secrets.token_hex(16)
                hash1 = hashlib.sha256(msg["token1"].encode() + hashed).hexdigest()
                ws.send(json.dumps({"type": "auth", "token3": token3, "hash": hashlib.sha256(
                    (token3 + msg["token2"] + hash1).encode()).hexdigest()}))
            elif kind == "authSuccess":
                log(f"connected to {info.get('friendly_name') or HOST} (firmware {info.get('version')})")
                with lock:
                    state["authenticated"] = True
            elif kind == "authError":
                raise PermissionError("login failed, check WATTPILOT_PASSWORD: " + str(msg.get("message", "")))
            elif kind in ("fullStatus", "deltaStatus"):
                with lock:
                    status.update(msg.get("status", {}))
    finally:
        with lock:
            state["authenticated"] = False
        ws.close()


def connect_loop():
    while True:
        try:
            run_once()
        except PermissionError as ex:
            log(ex)
            time.sleep(300)  # do not hammer the Wattpilot with wrong passwords
            continue
        except Exception as ex:  # noqa: BLE001 - reconnect on any error
            log(f"connection error: {ex}")
        time.sleep(10)


# --- metrics -------------------------------------------------------------------------------------

def num(v):
    if isinstance(v, bool):
        return int(v)
    return v if isinstance(v, (int, float)) else None


def nrg(i):
    return lambda s: num(s["nrg"][i]) if len(s.get("nrg") or []) > i else None


# name, help, getter. Keys: https://github.com/joscha82/wattpilot/blob/main/API.md
METRICS = [
    ("wattpilot_power", "Charging power in W", nrg(11)),
    ("wattpilot_power_L1", "Power phase 1 in W", nrg(7)),
    ("wattpilot_power_L2", "Power phase 2 in W", nrg(8)),
    ("wattpilot_power_L3", "Power phase 3 in W", nrg(9)),
    ("wattpilot_voltage_L1", "Voltage phase 1 in V", nrg(0)),
    ("wattpilot_voltage_L2", "Voltage phase 2 in V", nrg(1)),
    ("wattpilot_voltage_L3", "Voltage phase 3 in V", nrg(2)),
    ("wattpilot_current_L1", "Current phase 1 in A", nrg(4)),
    ("wattpilot_current_L2", "Current phase 2 in A", nrg(5)),
    ("wattpilot_current_L3", "Current phase 3 in A", nrg(6)),
    ("wattpilot_energy_total", "Energy charged in total in Wh (eto)", lambda s: num(s.get("eto"))),
    ("wattpilot_energy_session", "Energy charged since the car was connected in Wh (wh)", lambda s: num(s.get("wh"))),
    ("wattpilot_car_state", "Car state: 1 idle, 2 charging, 3 waiting for car, 4 complete, 5 error (car)",
     lambda s: num(s.get("car"))),
    ("wattpilot_charging_allowed", "1 if the car is allowed to charge (alw)", lambda s: num(s.get("alw"))),
    ("wattpilot_current_limit", "Requested charging current in A (amp)", lambda s: num(s.get("amp"))),
    ("wattpilot_logic_mode", "Charging mode: 3 default, 4 eco, 5 next trip (lmo)", lambda s: num(s.get("lmo"))),
    ("wattpilot_error", "Error code, 0 = no error (err)", lambda s: num(s.get("err"))),
]


def render():
    with lock:
        fresh = state["authenticated"] and time.time() - state["last_msg"] < STALE_AFTER
        s = dict(status)
        i = dict(info)
    out = ["# HELP wattpilot_up 1 if the exporter is logged in to the Wattpilot and receives data",
           "# TYPE wattpilot_up gauge", f"wattpilot_up {int(fresh)}"]
    if i:
        labels = ",".join(f'{k}="{v.replace(chr(92), "").replace(chr(34), "")}"' for k, v in sorted(i.items()))
        out += ["# TYPE wattpilot_info gauge", f"wattpilot_info{{{labels}}} 1"]
    if fresh:
        for name, help_, get in METRICS:
            v = get(s)
            if v is not None:
                out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge", f"{name} {v}"]
    return ("\n".join(out) + "\n").encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_error(404)
            return
        body = render()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=connect_loop, daemon=True).start()
    log(f"serving metrics on :{LISTEN_PORT}/metrics")
    http.server.ThreadingHTTPServer(("", LISTEN_PORT), Handler).serve_forever()

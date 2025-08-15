import os, sys, time, json, socket, threading, subprocess, signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen, Request

ADAOS_CMD = os.environ.get("ADAOS_CMD", sys.executable + " -m adaos.sdk.cli app start")
TARGET_HOST = os.environ.get("ADAOS_TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("ADAOS_TARGET_PORT", "8788"))  # где слушает ядро
LISTEN_PORT = int(os.environ.get("ADAOS_GW_PORT", "8777"))  # куда стучатся клиенты
AUTH_TOKEN = os.environ.get("ADAOS_TOKEN", "dev-local-token")
READY_PATH = os.environ.get("ADAOS_READY_PATH", f"http://{TARGET_HOST}:{TARGET_PORT}/ready")

proc = None
proc_lock = threading.Lock()


def is_ready(timeout=0.3) -> bool:
    try:
        req = Request(READY_PATH, headers={"X-AdaOS-Token": AUTH_TOKEN})
        with urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_core():
    global proc
    with proc_lock:
        if proc and proc.poll() is None:
            return
        # Запуск ядра как подпроцесса (без шелла безопаснее)
        args = ADAOS_CMD.split()
        env = os.environ.copy()
        env["ADAOS_TOKEN"] = AUTH_TOKEN
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_until_ready(deadline_sec=20):
    start = time.time()
    while time.time() - start < deadline_sec:
        if is_ready():
            return True
        time.sleep(0.2)
    return False


class Gateway(BaseHTTPRequestHandler):
    def _auth_ok(self):
        return self.headers.get("X-AdaOS-Token") == AUTH_TOKEN

    def _proxy(self, method: str, path: str, body: bytes):
        # простейший прокси только для POST /api/say и GET /health|/ready для примера
        import http.client

        conn = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT, timeout=10)
        headers = {k: v for k, v in self.headers.items()}
        headers["X-AdaOS-Token"] = AUTH_TOKEN
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/ready"):
            # не стартуем ядро ради health-check самого лончера
            code = 200 if (proc and proc.poll() is None and is_ready()) else 503
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"ok":%s}' % (b"true" if code == 200 else b"false"))
            return

        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return

        if not is_ready():
            start_core()
            if not wait_until_ready():
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"core not ready"}')
                return
        self._proxy("GET", self.path, b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)

        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return

        if not is_ready():
            start_core()
            if not wait_until_ready():
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"core not ready"}')
                return

        self._proxy("POST", self.path, body)


def run():
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), Gateway)
    print(f"[sentinel] listen http://127.0.0.1:{LISTEN_PORT}  → core {TARGET_HOST}:{TARGET_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with proc_lock:
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    run()

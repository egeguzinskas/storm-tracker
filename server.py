#!/usr/bin/env python3
"""Storm tracker: read the ESP32 weather station over USB serial and serve a live dashboard.

Usage:  python server.py [--port /dev/cu.wchusbserial310] [--http 8000] [--lan]
Then open http://localhost:8000 (or http://<this Mac's IP>:8000/phone from a phone with --lan)
"""
import argparse
import csv
import glob
import json
import os
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import socket

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "readings.csv")
FIELDS = ["t", "temp_c", "temp_f", "humidity", "pressure", "trend", "rain_label", "rain_pct"]

# Temp: 19.7 C (67.4 F)  Humidity: 51 %  Pressure: 1017.4 hPa, trend in 30 min  Rain: dry (0%)
LINE_RE = re.compile(
    r"Temp:\s*(?P<temp_c>-?[\d.]+)\s*C\s*\((?P<temp_f>-?[\d.]+)\s*F\)\s*"
    r"Humidity:\s*(?P<humidity>[\d.]+)\s*%\s*"
    r"Pressure:\s*(?P<pressure>[\d.]+)\s*hPa,?\s*(?P<trend>.*?)\s*"
    r"Rain:\s*(?P<rain_label>[^(]*?)\s*\((?P<rain_pct>[\d.]+)%\)"
)

history = deque(maxlen=50_000)  # ~2 days at one reading every 3 s
status = {"connected": False, "port": None, "last_line": None}
subscribers = []
lock = threading.Lock()


def parse(line):
    m = LINE_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    return {
        "t": round(time.time(), 3),
        "temp_c": float(d["temp_c"]),
        "temp_f": float(d["temp_f"]),
        "humidity": float(d["humidity"]),
        "pressure": float(d["pressure"]),
        "trend": d["trend"].strip().rstrip(",") or None,
        "rain_label": d["rain_label"].strip(),
        "rain_pct": float(d["rain_pct"]),
    }


def load_log():
    if not os.path.exists(LOG_PATH):
        return
    with open(LOG_PATH, newline="") as f:
        for row in csv.DictReader(f):
            try:
                history.append({
                    "t": float(row["t"]),
                    "temp_c": float(row["temp_c"]),
                    "temp_f": float(row["temp_f"]),
                    "humidity": float(row["humidity"]),
                    "pressure": float(row["pressure"]),
                    "trend": row["trend"] or None,
                    "rain_label": row["rain_label"],
                    "rain_pct": float(row["rain_pct"]),
                })
            except (KeyError, ValueError):
                continue


def append_log(rec):
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: ("" if rec[k] is None else rec[k]) for k in FIELDS})


def broadcast(event, data):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    with lock:
        for q in list(subscribers):
            q.append(msg)


def find_port():
    ports = sorted(glob.glob("/dev/cu.wchusbserial*") + glob.glob("/dev/cu.usbserial*")
                   + glob.glob("/dev/ttyUSB*"))
    return ports[0] if ports else None


def serial_loop(port_arg, baud):
    while True:
        port = port_arg or find_port()
        if not port:
            status.update(connected=False, port=None)
            broadcast("status", status)
            time.sleep(2)
            continue
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, baud, 1
            s.dtr = s.rts = False  # try not to reset the board on open
            s.open()
            status.update(connected=True, port=port)
            broadcast("status", status)
            print(f"Connected to {port}")
            buf = b""
            while True:
                buf += s.read(256)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    status["last_line"] = line
                    rec = parse(line)
                    if rec:
                        history.append(rec)
                        append_log(rec)
                        broadcast("reading", rec)
        except (serial.SerialException, OSError) as e:
            print(f"Serial error on {port}: {e}; retrying")
            status.update(connected=False)
            broadcast("status", status)
            time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path in ("/phone", "/phone.html"):
            with open(os.path.join(HERE, "phone.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/history":
            self._send(200, json.dumps({"readings": list(history), "status": status}).encode(),
                       "application/json")
        elif self.path.startswith("/latest"):
            # Polling fallback for proxies that buffer /events (e.g. Cloudflare quick tunnels)
            try:
                since = float(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            with lock:
                new = [r for r in list(history)[-200:] if r["t"] > since]
            self._send(200, json.dumps({"readings": new, "status": status}).encode(),
                       "application/json")
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = deque()
            with lock:
                subscribers.append(q)
            try:
                self.wfile.write(f"event: status\ndata: {json.dumps(status)}\n\n".encode())
                self.wfile.flush()
                last_ping = time.time()
                while True:
                    while q:
                        self.wfile.write(q.popleft())
                    if time.time() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        last_ping = time.time()
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    subscribers.remove(q)
        else:
            self._send(404, b"not found", "text/plain")


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outbound interface
            return s.getsockname()[0]
        except OSError:
            return "localhost"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--http", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="also serve to other devices on this Wi-Fi")
    args = ap.parse_args()

    load_log()
    threading.Thread(target=serial_loop, args=(args.port, args.baud), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.http), Handler)
    print(f"Dashboard: http://localhost:{args.http}")
    if args.lan:
        print(f"Phone:     http://{lan_ip()}:{args.http}/phone")
    server.serve_forever()


if __name__ == "__main__":
    main()

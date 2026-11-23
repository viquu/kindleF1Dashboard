#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KPW1 遥测接收端(Oracle Cloud,仅标准库,无第三方依赖)。

Kindle 每轮常显拉图后 POST 一份遥测 JSON 到 /api/telemetry,本服务只做两件
事:原子写 /opt/f1dash/out/telemetry.json(nginx 现成静态服务,Muse 用普通
HTTP GET 就能看,不用 SSH),再追加一行到 telemetry.log(超 200KB 只留最后
500 行,约 7 天量)。指令反方向走静态文件:Kindle 每轮 GET
http://<host>:8080/f1/command.json,Muse 经 SSH 写那个文件即可,本服务不管。

安全说明(v1):与 latest.png 同等公开暴露(无鉴权)。只收固定字段的 JSON
(dict、<=64KB),不执行任何客户端提供的内容,不把客户端给的路径当文件
用,写死两个输出路径。若以后要收紧,给 /api/ 加 token 再收。
部署见 cloud/deploy.sh(新增 telemetry 服务 + nginx /api/ 代理)。
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT_DIR = os.environ.get("F1DASH_OUT_DIR", "/opt/f1dash/out")
TELEMETRY_FILE = os.path.join(OUT_DIR, "telemetry.json")
TELEMETRY_LOG = os.path.join(OUT_DIR, "telemetry.log")
MAX_BODY = 64 * 1024
MAX_LOG_BYTES = 200 * 1024
KEEP_LOG_LINES = 500
BIND = ("127.0.0.1", int(os.environ.get("F1DASH_TELEMETRY_PORT", "8081")))


def _store(payload):
    """原子写 telemetry.json + 追加一行日志(超限截断留尾);失败抛异常。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = TELEMETRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, TELEMETRY_FILE)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with open(TELEMETRY_LOG, "a") as f:
        f.write(line)
    try:
        if os.path.getsize(TELEMETRY_LOG) > MAX_LOG_BYTES:
            with open(TELEMETRY_LOG) as f:
                lines = f.readlines()[-KEEP_LOG_LINES:]
            with open(TELEMETRY_LOG, "w") as f:
                f.writelines(lines)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 日志走 stdout(进 journald),不写文件
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/api/health", "/api/telemetry"):
            if self.path == "/api/health":
                return self._send_json(200, {"ok": True})
            try:
                with open(TELEMETRY_FILE) as f:
                    return self._send_json(200, json.load(f))
            except Exception:
                return self._send_json(200, {"ok": False, "error": "no telemetry yet"})
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/telemetry":
            return self._send_json(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send_json(400, {"ok": False, "error": "bad Content-Length"})
        if length <= 0 or length > MAX_BODY:
            return self._send_json(413, {"ok": False, "error": "body too large"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._send_json(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"ok": False, "error": "payload must be an object"})
        try:
            _store(payload)
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": repr(exc)[:80]})
        return self._send_json(200, {"ok": True})


def main():
    server = ThreadingHTTPServer(BIND, Handler)
    print("telemetry server listening on %s:%d, out=%s" % (BIND[0], BIND[1], OUT_DIR), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

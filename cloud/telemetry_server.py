#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KPW1 遥测 + 诊断日志接收端(Oracle Cloud,仅标准库,无第三方依赖)。

Kindle 每轮常显拉图后 POST 一份遥测 JSON 到 /api/telemetry,本服务只做两件
事:原子写 /opt/f1dash/out/telemetry.json(nginx 现成静态服务,Muse 用普通
HTTP GET 就能看,不用 SSH),再追加一行到 telemetry.log(超 200KB 只留最后
500 行,约 7 天量)。指令反方向走静态文件:Kindle 每轮 GET
http://<host>:8080/f1/command.json,Muse 经 SSH 写那个文件即可,本服务不管。

诊断日志:Kindle 收到 upload_log 指令(或自更新重启后首轮自动上传)时
POST 服务日志尾到 /api/log,本服务只追加一行 JSON 到
/opt/f1dash/out/diagnostic.log(只留最后 200 次上传),GET /api/log 读最新
一次。service.log 按设计不含任何凭据(Kindle 端上传前还会脱敏+截断),
这里仍只收固定字段的 JSON,不执行客户端内容。

安全说明(v1):与 latest.png 同等公开暴露(无鉴权)。只收固定字段的 JSON
(dict、<=64KB),不执行任何客户端提供的内容,不把客户端给的路径当文件
用,写死三个输出路径。若以后要收紧,给 /api/ 加 token 再收。
部署见 cloud/deploy.sh(新增 telemetry 服务 + nginx /api/ 代理)。
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT_DIR = os.environ.get("F1DASH_OUT_DIR", "/opt/f1dash/out")
TELEMETRY_FILE = os.path.join(OUT_DIR, "telemetry.json")
TELEMETRY_LOG = os.path.join(OUT_DIR, "telemetry.log")
DIAGNOSTIC_FILE = os.path.join(OUT_DIR, "diagnostic.log")
MAX_BODY = 64 * 1024
MAX_LOG_BYTES = 200 * 1024
KEEP_LOG_LINES = 500
KEEP_DIAGNOSTIC_LINES = 200
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


def _store_diagnostic(payload):
    """诊断日志追加一行 JSON 到 diagnostic.log,只留最后 200 次上传。

    每行就是 Kindle 上传的原样 payload(dict,含 log/battery_csv 列表)。
    超限后用 tmp+replace 原子截断,避免读到一半的文件。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with open(DIAGNOSTIC_FILE, "a") as f:
        f.write(line)
    try:
        with open(DIAGNOSTIC_FILE) as f:
            lines = f.readlines()
        if len(lines) > KEEP_DIAGNOSTIC_LINES:
            tmp = DIAGNOSTIC_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines[-KEEP_DIAGNOSTIC_LINES:])
            os.replace(tmp, DIAGNOSTIC_FILE)
    except Exception:
        pass


def _latest_diagnostic():
    """读 diagnostic.log 最后一行并解析;没有返回 None。"""
    try:
        with open(DIAGNOSTIC_FILE) as f:
            lines = f.readlines()
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


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

    def _read_json_body(self):
        """读 POST JSON 体;返回 (payload, error_code, error_obj)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, 400, {"ok": False, "error": "bad Content-Length"}
        if length <= 0 or length > MAX_BODY:
            return None, 413, {"ok": False, "error": "body too large"}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None, 400, {"ok": False, "error": "bad JSON"}
        if not isinstance(payload, dict):
            return None, 400, {"ok": False, "error": "payload must be an object"}
        return payload, None, None

    def do_GET(self):
        if self.path == "/api/health":
            return self._send_json(200, {"ok": True})
        if self.path == "/api/telemetry":
            try:
                with open(TELEMETRY_FILE) as f:
                    return self._send_json(200, json.load(f))
            except Exception:
                return self._send_json(200, {"ok": False, "error": "no telemetry yet"})
        if self.path == "/api/log":
            latest = _latest_diagnostic()
            if latest is None:
                return self._send_json(200, {"ok": False, "error": "no diagnostic yet"})
            return self._send_json(200, latest)
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/api/telemetry":
            payload, err, err_obj = self._read_json_body()
            if err:
                return self._send_json(err, err_obj)
            try:
                _store(payload)
            except Exception as exc:
                return self._send_json(500, {"ok": False, "error": repr(exc)[:80]})
            return self._send_json(200, {"ok": True})
        if self.path == "/api/log":
            payload, err, err_obj = self._read_json_body()
            if err:
                return self._send_json(err, err_obj)
            if not isinstance(payload.get("log"), list):
                return self._send_json(400, {"ok": False, "error": "log must be a list"})
            try:
                _store_diagnostic(payload)
            except Exception as exc:
                return self._send_json(500, {"ok": False, "error": repr(exc)[:80]})
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"ok": False, "error": "not found"})


def main():
    server = ThreadingHTTPServer(BIND, Handler)
    print("telemetry server listening on %s:%d, out=%s" % (BIND[0], BIND[1], OUT_DIR), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

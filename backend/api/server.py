import hmac
import json
import logging
import mimetypes
import shutil
import tempfile
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.lua import codec as lua
from ..core.storage import Error, ini_text, read_ini, write_many
from ..services.mods import shard_name

LOGS = {
    "panel": "panel.log",
    "master": "master.log",
    "caves": "caves.log",
    "steamcmd": "steamcmd.log",
    "mods": "mods.log",
}


logger = logging.getLogger(__name__)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, runtime, auth):
        self.runtime, self.auth = runtime, auth
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s %s", self.client_address[0], fmt % args)

    def send(
        self, status, body, content_type="application/json; charset=utf-8", headers=None
    ):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self, token="", delete=False):
        value = (
            "dst_session="
            + token
            + "; Path=/; HttpOnly; SameSite=Strict; Max-Age="
            + ("0" if delete else "43200")
        )
        if self.server.runtime.config.cookie_secure:
            value += "; Secure"
        return {"Set-Cookie": value}

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["dst_session"].value if "dst_session" in cookie else ""
        except (CookieError, ValueError):
            token = ""
        return token, self.server.auth.session(token)

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in (
                "http",
                "https",
            ) or parsed.netloc != self.headers.get("Host"):
                raise Error("不允许跨站请求", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise Error("不允许跨站请求", 403)

    def length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise Error("不支持分块请求体", 400)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise Error("Content-Length 不合法") from exc
        if not 0 < size <= maximum:
            raise Error("请求为空或超过大小限制", 413)
        return size

    def body(self):
        if (
            self.headers.get("Content-Type", "").split(";")[0].strip()
            != "application/json"
        ):
            raise Error("请求必须使用 application/json", 415)
        raw = self.rfile.read(self.length(2 * 1024 * 1024))
        try:
            value = json.loads(
                raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x))
            )
        except (ValueError, UnicodeError) as exc:
            raise Error("JSON 格式不正确") from exc
        if not isinstance(value, dict):
            raise Error("请求体必须是对象")
        return value

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def handle_request(self, method):
        try:
            self.route(method)
        except Error as exc:
            self.close_connection = True
            self.send(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            logger.exception("HTTP 请求失败")
            self.send(500, {"error": "操作失败，请查看面板日志"})

    def route(self, method):
        r, auth = self.server.runtime, self.server.auth
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if method == "GET" and path in ("/", "/app.js", "/style.css"):
            filename = "index.html" if path == "/" else path[1:]
            content = (r.config.root / "frontend" / filename).read_bytes()
            self.send(
                200, content, mimetypes.guess_type(filename)[0] + "; charset=utf-8"
            )
            return
        if method == "GET" and path == "/healthz":
            self.send(200, {"ok": True})
            return
        if method == "POST":
            self.same_origin()
        if method == "POST" and path == "/api/login":
            token, session = auth.login(
                self.body().get("password"), self.client_address[0]
            )
            self.send(200, {"csrf": session["csrf"]}, headers=self.cookie(token))
            return
        token, session = self.session()
        if method == "POST" and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()
        ):
            raise Error("会话校验失败，请刷新后重试", 403)
        if method == "GET" and path == "/api/session":
            self.send(
                200, {"csrf": session["csrf"], "upload_limit": r.config.upload_limit}
            )
        elif method == "POST" and path == "/api/logout":
            self.body()
            auth.logout(token)
            self.send(200, {"ok": True}, headers=self.cookie(delete=True))
        elif method == "POST" and path == "/api/password":
            body = self.body()
            auth.change(body.get("current"), body.get("password"))
            self.send(200, {"ok": True}, headers=self.cookie(delete=True))
        elif method == "GET" and path == "/api/status":
            self.send(200, r.status())
        elif method == "GET" and path == "/api/world":
            shard = shard_name(query.get("shard", ["Master"])[0])
            level = r.config.cluster / shard / "leveldataoverride.lua"
            data = lua.read(level) if level.exists() else {"overrides": {}}
            self.send(
                200,
                {
                    "sections": read_ini(r.config.cluster / "cluster.ini"),
                    "level": data,
                    "token_configured": r.has_token(),
                },
            )
        elif method == "POST" and path == "/api/world":
            body = self.body()
            shard = shard_name(body.get("shard"))
            level = body.get("level")
            if not isinstance(level, dict) or not isinstance(
                level.get("overrides"), dict
            ):
                raise Error("世界参数必须包含 overrides 对象")
            sections = body.get("sections")
            if (
                not isinstance(sections, dict)
                or "NETWORK" not in sections
                or "GAMEPLAY" not in sections
            ):
                raise Error("服务器设置缺少 NETWORK 或 GAMEPLAY")
            ini = ini_text(sections)
            try:
                players = int(sections["GAMEPLAY"]["max_players"])
                if not 1 <= players <= 64:
                    raise ValueError()
            except (ValueError, KeyError, TypeError) as exc:
                raise Error("人数应为 1—64 的整数") from exc
            contents = {
                r.config.cluster / "cluster.ini": ini,
                r.config.cluster / shard / "leveldataoverride.lua": lua.dumps(level),
            }
            new_token = body.get("token", "")
            if not isinstance(new_token, str):
                raise Error("Token 必须是字符串")
            if new_token:
                if (
                    not new_token.strip()
                    or len(new_token.strip()) > 1024
                    or any(x in new_token for x in "\r\n\0")
                ):
                    raise Error("Token 格式不合法")
                contents[r.config.cluster / "cluster_token.txt"] = (
                    new_token.strip() + "\n"
                )
            with r.edit():
                write_many(contents)
            self.send(200, {"ok": True})
        elif method == "GET" and path == "/api/mods":
            shard = shard_name(query.get("shard", ["Master"])[0])
            self.send(200, {"mods": r.mods.view(shard)})
        elif method == "POST" and path == "/api/mods":
            body = self.body()
            with r.edit():
                r.mods.save(
                    body.get("shard"), body.get("mods"), body.get("both") is True
                )
            self.send(200, {"ok": True})
        elif method == "POST" and path == "/api/task":
            body = self.body()
            self.send(
                202,
                r.submit(
                    body.get("action"),
                    **{k: v for k, v in body.items() if k != "action"},
                ),
            )
        elif method == "GET" and path == "/api/archives":
            self.send(200, {"archives": r.archives.list()})
        elif method == "POST" and path == "/api/upload":
            self.upload()
        elif method == "GET" and path == "/api/download":
            self.download(r.archives.path(query.get("name", [""])[0]))
        elif method == "GET" and path == "/api/logs":
            self.logs(query)
        else:
            raise Error("接口不存在", 404)

    def upload(self):
        r = self.server.runtime
        if self.headers.get("Content-Type") != "application/octet-stream":
            raise Error("上传内容类型不正确", 415)
        remaining = self.length(r.config.upload_limit)
        fd, name = tempfile.mkstemp(prefix="incoming-", dir=r.config.panel)
        import os

        try:
            with os.fdopen(fd, "wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise Error("上传中断，存档未导入")
                    handle.write(chunk)
                    remaining -= len(chunk)
            archive = r.archives.import_file(
                Path(name), unquote(self.headers.get("X-Archive-Label", "上传存档"))
            )
            self.send(200, {"archive": archive})
        finally:
            Path(name).unlink(missing_ok=True)

    def download(self, path):
        # 先打开文件再取大小，下载期间不会混入其他备份。
        with path.open("rb") as handle:
            import os

            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            self.send_header(
                "Content-Disposition", 'attachment; filename="' + path.name + '"'
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(handle, self.wfile, 1024 * 1024)

    def logs(self, query):
        r = self.server.runtime
        name = query.get("name", ["panel"])[0]
        if name not in LOGS:
            raise Error("未知日志")
        path = r.config.logs / LOGS[name]
        if not path.exists():
            self.send(200, {"offset": 0, "text": "", "reset": True})
            return
        try:
            offset = int(query.get("offset", ["-1"])[0])
        except ValueError as exc:
            raise Error("日志偏移应为整数") from exc
        with path.open("rb") as handle:
            size = path.stat().st_size
            reset = offset < 0 or offset > size
            if reset:
                offset = max(0, size - 65536)
            handle.seek(offset)
            data = handle.read(262144)
        self.send(
            200,
            {
                "offset": offset + len(data),
                "text": data.decode("utf-8", errors="replace"),
                "reset": reset,
            },
        )

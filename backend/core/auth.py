import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque

from .storage import Error, write


class Auth:
    def __init__(self, directory):
        self.path = directory / "auth.json"
        self.lock = threading.RLock()
        self.sessions = {}
        self.failures = defaultdict(deque)
        self.initial_password = None
        if not self.path.exists():
            self.initial_password = secrets.token_urlsafe(18)
            self._save(self.initial_password)
        self.record = json.loads(self.path.read_text())

    def _save(self, password):
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), 600000
        ).hex()
        write(self.path, json.dumps({"salt": salt, "digest": digest}))
        self.path.chmod(0o600)

    def _verify(self, password):
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(self.record["salt"]), 600000
        ).hex()
        return hmac.compare_digest(digest, self.record["digest"])

    def login(self, password, address):
        if not isinstance(password, str) or len(password.encode()) > 1024:
            raise Error("密码不正确", 401)
        with self.lock:
            now = time.time()
            if len(self.failures) > 1024:
                self.failures = defaultdict(
                    deque,
                    {k: v for k, v in self.failures.items() if v and v[-1] > now - 60},
                )
            failures = self.failures[address]
            while failures and failures[0] < now - 60:
                failures.popleft()
            if len(failures) >= 5:
                raise Error("尝试次数过多，请一分钟后重试", 429)
            if not self._verify(password):
                failures.append(now)
                raise Error("密码不正确", 401)
            failures.clear()
            self.sessions = {
                k: v for k, v in self.sessions.items() if v["expires"] > now
            }
            token = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "expires": now + 12 * 3600}
            self.sessions[token] = session
            return token, session

    def session(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if not session or session["expires"] < time.time():
                self.sessions.pop(token, None)
                raise Error("请先登录", 401)
            return dict(session)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def change(self, current, password):
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise Error("新密码长度应为 10—128 个字符")
        if not isinstance(current, str) or len(current) > 1024:
            raise Error("当前密码不正确")
        with self.lock:
            if not self._verify(current):
                raise Error("当前密码不正确")
            self._save(password)
            self.record = json.loads(self.path.read_text())
            self.sessions.clear()

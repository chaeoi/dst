import os
import re
from pathlib import Path


class Config:
    def __init__(self):
        self.data = Path(os.getenv("DST_DATA_DIR", "/data")).resolve()
        self.install = Path(os.getenv("DST_INSTALL_DIR", "/opt/dst/server")).resolve()
        self.steamcmd = Path(os.getenv("DST_STEAMCMD_DIR", "/opt/steamcmd")).resolve()
        self.root = Path(__file__).resolve().parents[2]
        self.defaults = self.root / "defaults"
        self.cluster_name = os.getenv("DST_CLUSTER_NAME", "Cluster_1")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.cluster_name):
            raise ValueError("DST_CLUSTER_NAME 只允许字母、数字、下划线和短横线")
        self.cluster = self.data / self.cluster_name
        self.panel = self.data / ".panel"
        self.logs = self.data / "logs"
        self.backups = self.data / "backups"
        self.host = os.getenv("DST_WEB_BIND", "0.0.0.0")
        self.port = int(os.getenv("DST_WEB_PORT", "8080"))
        self.shutdown_timeout = int(os.getenv("DST_SHUTDOWN_TIMEOUT", "60"))
        self.upload_limit = int(os.getenv("DST_UPLOAD_LIMIT_MB", "512")) * 1024 * 1024
        self.unpack_limit = 2 * 1024 * 1024 * 1024
        self.cookie_secure = os.getenv("DST_COOKIE_SECURE", "0") == "1"
        self.token = os.getenv("DST_CLUSTER_TOKEN", "").strip()
        if (
            not 1 <= self.port <= 65535
            or self.shutdown_timeout < 1
            or self.upload_limit < 1
        ):
            raise ValueError("端口、停服超时或上传限制不合法")
        for path in (self.data, self.panel, self.logs, self.backups):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def binary(self):
        return self.install / "bin64/dontstarve_dedicated_server_nullrenderer_x64"

    def game_args(self):
        return [
            str(self.binary),
            "-console",
            "-persistent_storage_root",
            str(self.data.parent),
            "-conf_dir",
            self.data.name,
            "-cluster",
            self.cluster_name,
            "-ugc_directory",
            str(self.install / "ugc"),
        ]

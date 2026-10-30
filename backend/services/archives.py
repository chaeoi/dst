import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from ..core.lua import codec as lua
from ..core.storage import Error, read_ini, write

REQUIRED = ("cluster.ini", "Master/server.ini", "Caves/server.ini")


def validate_cluster(root):
    root = Path(root)
    token = root / "cluster_token.txt"
    if token.exists() and (
        token.is_symlink() or not token.is_file() or token.stat().st_size > 2048
    ):
        raise Error("存档 Token 文件不合法或超过 2 KB")
    for name in REQUIRED:
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > 2 * 1024 * 1024
        ):
            raise Error(f"存档缺少或包含不合法的 {name}")
        ini = read_ini(path)
        if "NETWORK" not in ini:
            raise Error(f"{name} 缺少 NETWORK 配置段")
        if name != "cluster.ini":
            try:
                port = int(ini["NETWORK"]["server_port"])
            except (ValueError, KeyError) as exc:
                raise Error(f"{name} 缺少有效的游戏端口") from exc
            if not 1 <= port <= 65535:
                raise Error(f"{name} 游戏端口超出范围")
    for shard in ("Master", "Caves"):
        for name in ("modoverrides.lua", "leveldataoverride.lua"):
            path = root / shard / name
            if path.exists() and not isinstance(lua.read(path), dict):
                raise Error(f"{shard}/{name} 必须返回键值表")


class Archives:
    def __init__(self, config):
        self.config = config

    def path(self, name):
        import re

        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+\.tar\.gz", name
        ):
            raise Error("存档包名称不合法")
        path = self.config.backups / name
        if path.is_symlink() or not path.is_file():
            raise Error("存档包不存在", 404)
        return path

    def list(self):
        result = []
        for path in self.config.backups.glob("*.tar.gz"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                meta = json.loads(path.with_suffix(".json").read_text())
            except (OSError, ValueError):
                meta = {}
            result.append(
                {
                    "name": path.name,
                    "label": meta.get("label", ""),
                    "source": meta.get("source", "backup"),
                    "size": path.stat().st_size,
                    "created": meta.get("created", path.stat().st_mtime),
                }
            )
        return sorted(result, key=lambda x: x["created"], reverse=True)

    def create(self, root, label="", source="backup"):
        validate_cluster(root)
        for path in Path(root).rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise Error("存档包含链接或非常规文件，无法备份")
        name = (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            + "_"
            + uuid.uuid4().hex[:12]
            + ".tar.gz"
        )
        dest = self.config.backups / name
        fd, tmp = tempfile.mkstemp(dir=self.config.backups, suffix=".tmp")
        os.close(fd)
        try:
            with tarfile.open(tmp, "w:gz") as archive:
                archive.add(root, arcname=self.config.cluster_name)
            os.chmod(tmp, 0o600)
            write(
                dest.with_suffix(".json"),
                json.dumps(
                    {
                        "label": str(label)[:128],
                        "source": source,
                        "created": time.time(),
                    }
                ),
            )
            os.replace(tmp, dest)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return next(x for x in self.list() if x["name"] == name)

    def extract(self, archive_path, work):
        """逐个复制常规文件；从不调用 tar/zip 的 extractall。"""
        total, count = 0, 0
        seen = set()

        def destination(name, size, is_dir):
            nonlocal total, count
            if "\\" in name or "\0" in name:
                raise Error("存档包含不安全路径")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise Error("存档包含目录穿越路径")
            if not path.parts or "__MACOSX" in path.parts or path.name == ".DS_Store":
                return None
            count += 1
            total += size
            if count > 20000 or total > self.config.unpack_limit or size < 0:
                raise Error("存档包文件过多或解压大小超过 2 GB")
            canonical = str(path)
            if canonical in seen:
                raise Error("存档包包含重复路径")
            seen.add(canonical)
            target = work.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if is_dir:
                target.mkdir(exist_ok=True)
            return target

        try:
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path) as archive:
                    for member in archive.infolist():
                        if member.flag_bits & 1:
                            raise Error("不支持加密 ZIP，请先解密再上传")
                        mode = member.external_attr >> 16
                        if stat.S_ISLNK(mode) or (
                            stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                        ):
                            raise Error("存档包不允许包含链接或非常规文件")
                        target = destination(
                            member.filename, member.file_size, member.is_dir()
                        )
                        if target and not member.is_dir():
                            with archive.open(member) as src, target.open("xb") as dst:
                                shutil.copyfileobj(src, dst, 1024 * 1024)
            else:
                with tarfile.open(archive_path, "r:gz") as archive:
                    for member in archive:
                        if not (member.isfile() or member.isdir()):
                            raise Error("存档包不允许包含链接或非常规文件")
                        target = destination(member.name, member.size, member.isdir())
                        if target and member.isfile():
                            with (
                                archive.extractfile(member) as src,
                                target.open("xb") as dst,
                            ):
                                shutil.copyfileobj(src, dst, 1024 * 1024)
        except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
            raise Error(f"存档包无法解压：{exc}") from exc
        candidates = [p.parent for p in work.rglob("cluster.ini")]
        if len(candidates) != 1:
            raise Error("存档包必须且只能包含一套服务器存档")
        cluster = candidates[0]
        for path in work.rglob("*"):
            if path.is_file() and not path.is_relative_to(cluster):
                raise Error("存档包包含存档目录以外的文件")
        validate_cluster(cluster)
        return cluster

    def import_file(self, path, label):
        with tempfile.TemporaryDirectory(
            prefix="upload-", dir=self.config.panel
        ) as work:
            cluster = self.extract(path, Path(work))
            return self.create(cluster, label, "upload")

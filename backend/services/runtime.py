import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..core.storage import Error, ini_text, read_ini, write
from .archives import Archives, validate_cluster
from .mods import SHARDS, Mods, shard_name, workshop_id

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config):
        self.config = config
        self.archives = Archives(config)
        self.mods = Mods(config)
        self.lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.processes = {}
        self.tool = None
        self.closing = threading.Event()
        self.phase = "stopped"
        self.error = ""
        self.task = None
        self.worker = None
        self.dirty = False
        self.started = time.time()
        self.initialize()

    def initialize(self):
        c = self.config
        previous = c.panel / "restore-previous"
        committed = c.panel / "restore-committed"
        # 恢复替换中断时优先救回原目录，不能在下次启动误当作新服务器。
        if committed.exists():
            shutil.rmtree(previous, ignore_errors=True)
            committed.unlink()
        elif previous.exists():
            shutil.rmtree(c.cluster, ignore_errors=True)
            os.replace(previous, c.cluster)
        for pattern in ("incoming-*", "upload-*", "restore-*"):
            for leftover in c.panel.glob(pattern):
                if leftover.is_dir():
                    shutil.rmtree(leftover)
                else:
                    leftover.unlink(missing_ok=True)
        for leftover in c.backups.glob("*.tmp"):
            leftover.unlink(missing_ok=True)
        if not c.cluster.exists() or not any(c.cluster.iterdir()):
            c.cluster.mkdir(parents=True, exist_ok=True)
            shutil.copytree(c.defaults, c.cluster, dirs_exist_ok=True)
            sections = read_ini(c.cluster / "cluster.ini")
            sections["NETWORK"]["cluster_cloud_id"] = uuid.uuid4().hex[:16].upper()
            sections["SHARD"]["cluster_key"] = uuid.uuid4().hex
            write(c.cluster / "cluster.ini", ini_text(sections))
            logger.info("没有现有存档，已初始化默认世界配置")
        else:
            logger.info("使用现有存档：%s", c.cluster_name)
        if c.token:
            self.save_token(c.token)

    def save_token(self, value):
        if (
            not isinstance(value, str)
            or not 1 <= len(value.strip()) <= 1024
            or any(x in value for x in "\r\n\0")
        ):
            raise Error("服务器 Token 不能为空或包含换行")
        write(self.config.cluster / "cluster_token.txt", value.strip() + "\n")
        (self.config.cluster / "cluster_token.txt").chmod(0o600)

    def has_token(self):
        path = self.config.cluster / "cluster_token.txt"
        return (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size <= 2048
            and bool(path.read_text().strip())
        )

    @contextmanager
    def edit(self):
        with self.state_lock:
            if (
                self.task and self.task["status"] == "running"
            ) or not self.lock.acquire(blocking=False):
                raise Error("服务器正在执行任务，请完成后重试", 409)
        try:
            yield
            self.dirty = True
        finally:
            self.lock.release()

    def status(self):
        with self.state_lock:
            return {
                "phase": self.phase,
                "error": self.error,
                "task": dict(self.task) if self.task else None,
                "shards": {s: p.poll() is None for s, p in self.processes.items()},
                "dirty": self.dirty,
                "uptime": int(time.time() - self.started),
                "cluster": self.config.cluster_name,
                "token_configured": self.has_token(),
            }

    def submit(self, action, **kwargs):
        if action not in ("start", "restart", "backup", "restore", "download"):
            raise Error("未知操作")
        with self.state_lock:
            if self.closing.is_set() or (
                self.task and self.task["status"] == "running"
            ):
                raise Error("已有任务正在执行，请稍后重试", 409)
            if action == "start" and any(
                p.poll() is None for p in self.processes.values()
            ):
                raise Error("服务器已经运行", 409)
            if action == "restore":
                self.archives.path(kwargs.get("name"))
            if action == "download":
                workshop_id(kwargs.get("id", ""))
                shard_name(kwargs.get("shard", "Master"))
            self.task = {
                "id": uuid.uuid4().hex,
                "action": action,
                "status": "running",
                "message": "正在准备",
            }
            self.worker = threading.Thread(
                target=self._job, args=(action, kwargs), daemon=True
            )
            self.worker.start()
            return dict(self.task)

    def progress(self, message):
        with self.state_lock:
            if self.task:
                self.task["message"] = message
        logger.info(message)

    def run_tool(self, args, log_name, timeout=1800):
        if self.closing.is_set():
            raise Error("服务器正在关闭", 409)
        with (self.config.logs / log_name).open("ab") as output:
            self.tool = subprocess.Popen(
                args,
                cwd=self.config.install / "bin64"
                if self.config.binary.exists()
                else self.config.steamcmd,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                status = self.tool.wait(timeout=timeout)
                if status:
                    raise Error(f"下载或更新失败，请查看 {log_name}（退出码 {status}）")
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(self.tool.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.tool.wait()
                raise Error("下载或更新超时，请查看日志后重试") from exc
            finally:
                self.tool = None

    def prepare_game(self, update=False):
        c = self.config
        validate_cluster(c.cluster)
        c.install.mkdir(parents=True, exist_ok=True)
        if update or not c.binary.exists():
            self.progress("正在下载游戏服务端，首次启动需要几分钟")
            self.run_tool(
                [
                    str(c.steamcmd / "steamcmd.sh"),
                    "+force_install_dir",
                    str(c.install),
                    "+login",
                    "anonymous",
                    "+app_update",
                    "343050",
                    "+quit",
                ],
                "steamcmd.log",
            )
        client = c.steamcmd / "linux64/steamclient.so"
        sdk = Path.home() / ".steam/sdk64"
        if client.is_file():
            sdk.mkdir(parents=True, exist_ok=True)
            if not (sdk / "steamclient.so").exists():
                (sdk / "steamclient.so").symlink_to(client)
        self.mods.sync()
        if self.mods.ids():
            self.download_mods()

    def download_mods(self, extra=()):
        self.mods.sync(extra)
        self.progress("正在下载模组及配置选项")
        try:
            self.run_tool(
                self.config.game_args() + ["-only_update_server_mods"],
                "mods.log",
                timeout=900,
            )
        finally:
            self.mods.sync()
            self.mods.cache.clear()

    def start_shards(self):
        if self.closing.is_set():
            return
        if not self.has_token():
            raise Error("请在世界设置中填写 Klei 服务器 Token，再点击重启服务器")
        validate_cluster(self.config.cluster)
        self.mods.sync()
        self.progress("正在启动地面和洞穴")
        self.phase = "starting"
        try:
            for shard in SHARDS:
                with (self.config.logs / (shard.lower() + ".log")).open("ab") as output:
                    p = subprocess.Popen(
                        self.config.game_args()
                        + ["-skip_update_server_mods", "-shard", shard],
                        cwd=self.config.binary.parent,
                        stdin=subprocess.PIPE,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    with self.state_lock:
                        self.processes[shard] = p
            for _ in range(20):
                if self.closing.wait(0.1):
                    self.stop_shards()
                    return
                if any(p.poll() is not None for p in self.processes.values()):
                    raise Error("游戏进程启动失败，请查看地面和洞穴日志")
        except Exception:
            self.stop_shards()
            raise
        self.phase = "running"
        self.dirty = False

    def stop_shards(self):
        processes = list(self.processes.values())
        for p in processes:
            if p.poll() is None and p.stdin:
                try:
                    p.stdin.write(b"c_shutdown(true)\n")
                    p.stdin.flush()
                except (OSError, ValueError):
                    pass
        deadline = time.monotonic() + self.config.shutdown_timeout
        while any(p.poll() is None for p in processes) and time.monotonic() < deadline:
            time.sleep(0.1)
        clean = all(p.poll() is not None and p.returncode == 0 for p in processes)
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            if p.stdin:
                p.stdin.close()
        with self.state_lock:
            self.processes.clear()
        return clean

    def restore(self, name):
        c = self.config
        archive = self.archives.path(name)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=c.panel) as work:
            staged = self.archives.extract(archive, Path(work))
            token_path = staged / "cluster_token.txt"
            if (
                not token_path.exists() or not token_path.read_text().strip()
            ) and self.has_token():
                shutil.copyfile(c.cluster / "cluster_token.txt", token_path)
            if not token_path.exists() or not token_path.read_text().strip():
                raise Error("上传的存档没有 Token，请先在世界设置中填写服务器 Token")
            self.progress("正在保存并停止当前世界")
            if not self.stop_shards():
                raise Error("游戏未能正常保存退出，已取消恢复，当前存档保留")
            previous = c.panel / "restore-previous"
            os.replace(c.cluster, previous)
            try:
                os.replace(staged, c.cluster)
                self.download_mods() if self.mods.ids() else self.mods.sync()
                self.start_shards()
                write(c.panel / "restore-committed", "committed\n")
            except Exception:
                self.stop_shards()
                shutil.rmtree(c.cluster, ignore_errors=True)
                os.replace(previous, c.cluster)
                self.start_shards()
                raise
            shutil.rmtree(previous)
            (c.panel / "restore-committed").unlink(missing_ok=True)

    def _job(self, action, kwargs):
        result = None
        with self.lock:
            self.error = ""
            self.phase = "maintenance" if action != "start" else "starting"
            try:
                if action == "restore":
                    self.restore(kwargs["name"])
                else:
                    running = any(p.poll() is None for p in self.processes.values())
                    if action != "start":
                        self.progress("正在保存并停止两个分片")
                        if not self.stop_shards():
                            raise Error("游戏未能正常保存退出，操作已取消，请查看日志")
                    if action in ("start", "restart"):
                        self.prepare_game(update=action == "start")
                    elif action == "backup":
                        self.progress("正在创建完整存档备份")
                        result = self.archives.create(
                            self.config.cluster, kwargs.get("label", "")
                        )
                    elif action == "download":
                        mid = workshop_id(kwargs["id"])
                        shard = shard_name(kwargs.get("shard", "Master"))
                        self.download_mods([mid])
                        if not self.mods.info_path(mid):
                            raise Error(
                                "下载结束但未找到模组文件，请检查模组 ID 和模组日志"
                            )
                        info = self.mods.info(mid)
                        if info.get("client_only"):
                            raise Error("这是客户端模组，不能在服务器上启用")
                        mods = self.mods.view(shard)
                        if not any(x["id"] == mid for x in mods):
                            mods.append({"id": mid, "enabled": True, "options": {}})
                            self.mods.save(shard, mods, kwargs.get("both", False))
                    if action in ("start", "restart") or running:
                        self.start_shards()
                    else:
                        self.phase = "stopped"
                with self.state_lock:
                    self.task.update(
                        status="success", message="操作完成", result=result
                    )
            except Exception as exc:
                logger.exception("操作失败：%s", action)
                self.error = str(exc)
                if (
                    not self.closing.is_set()
                    and not self.processes
                    and self.config.binary.exists()
                    and self.has_token()
                ):
                    try:
                        self.start_shards()
                    except Exception:
                        logger.exception("恢复运行失败")
                self.phase = "running" if self.processes else "error"
                with self.state_lock:
                    self.task.update(status="error", message=str(exc))

    def monitor(self):
        last_rotate = 0
        while not self.closing.wait(1):
            with self.state_lock:
                idle = not self.task or self.task["status"] != "running"
                crashed = any(p.poll() is not None for p in self.processes.values())
            if idle and crashed and self.lock.acquire(blocking=False):
                try:
                    self.stop_shards()
                    self.error = "游戏进程已退出，请查看日志后重启"
                    self.phase = "error"
                finally:
                    self.lock.release()
            if time.monotonic() - last_rotate > 30:
                self.rotate_logs()
                last_rotate = time.monotonic()

    def rotate_logs(self):
        for path in self.config.logs.glob("*.log"):
            try:
                if path.stat().st_size <= 10 * 1024 * 1024:
                    continue
                for n in range(2, 0, -1):
                    old = path.with_name(path.name + "." + str(n))
                    if old.exists():
                        os.replace(old, path.with_name(path.name + "." + str(n + 1)))
                shutil.copyfile(path, path.with_name(path.name + ".1"))
                with path.open("wb"):
                    pass
            except OSError:
                logger.exception("日志轮转失败")

    def close(self):
        self.closing.set()
        tool = self.tool
        if tool and tool.poll() is None:
            try:
                os.killpg(tool.pid, signal.SIGTERM)
                tool.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(tool.pid, signal.SIGKILL)
                tool.wait()
            except ProcessLookupError:
                pass
        if self.worker:
            self.worker.join(timeout=self.config.shutdown_timeout + 5)
        self.stop_shards()

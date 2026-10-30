import http.cookiejar
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.api.server import Server
from backend.core.auth import Auth
from backend.core.config import Config
from backend.core.lua import codec as lua
from backend.core.storage import Error, write_many
from backend.services.runtime import Runtime


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "DST_DATA_DIR": str(self.root / "data"),
                "DST_INSTALL_DIR": str(self.root / "game"),
                "DST_CLUSTER_NAME": "Cluster_1",
                "DST_CLUSTER_TOKEN": "test-only-token",
                "DST_SHUTDOWN_TIMEOUT": "2",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Config()
        self.runtime = Runtime(self.config)
        self.addCleanup(self.runtime.close)

    def fake_game(self):
        binary = self.config.binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("""#!/usr/bin/env python3
import sys,time
from pathlib import Path
args=sys.argv
if '-only_update_server_mods' in args:
    sys.exit(0)
root=Path(args[args.index('-persistent_storage_root')+1])/args[args.index('-conf_dir')+1]/args[args.index('-cluster')+1]
shard=args[args.index('-shard')+1]
print('world process started',flush=True)
for line in sys.stdin:
    if 'c_shutdown' in line:
        time.sleep(0.05 if shard=='Master' else 0.35)
        (root/shard/'saved-marker').write_text('saved')
        sys.exit(0)
""")
        binary.chmod(0o755)

    def finish(self, action, **kwargs):
        self.runtime.submit(action, **kwargs)
        self.runtime.worker.join(timeout=15)
        self.assertFalse(self.runtime.worker.is_alive())
        return self.runtime.task


class ConfigTests(Fixture):
    def test_interrupted_restore_rolls_back_before_boot(self):
        previous = self.config.panel / "restore-previous"
        shutil.copytree(self.config.cluster, previous)
        (previous / "original-world").write_text("original")
        (self.config.cluster / "new-world").write_text("incomplete replacement")
        self.runtime.initialize()
        self.assertTrue((self.config.cluster / "original-world").is_file())
        self.assertFalse((self.config.cluster / "new-world").exists())

    def test_committed_restore_cleanup_never_replaces_new_world(self):
        previous = self.config.panel / "restore-previous"
        previous.mkdir()
        (previous / "partially-deleted").write_text("old")
        (self.config.panel / "restore-committed").write_text("committed")
        (self.config.cluster / "new-world").write_text("keep")
        self.runtime.initialize()
        self.assertEqual((self.config.cluster / "new-world").read_text(), "keep")
        self.assertFalse(previous.exists())

    def test_existing_world_is_never_overwritten(self):
        path = self.config.cluster / "cluster.ini"
        path.write_text("existing custom file")
        self.runtime.initialize()
        self.assertEqual(path.read_text(), "existing custom file")

    def test_no_automatic_backups_or_bak_files(self):
        self.assertEqual(list(self.config.backups.iterdir()), [])
        self.assertEqual(list(self.config.data.rglob("*.bak")), [])

    def test_multifile_failure_rolls_back(self):
        a, b = self.root / "a", self.root / "b"
        a.write_text("old-a")
        b.write_text("old-b")
        original = os.replace
        failed = False

        def replace(src, dst):
            nonlocal failed
            if Path(dst) == b and not failed:
                failed = True
                raise OSError("simulated disk failure")
            return original(src, dst)

        with (
            patch("backend.core.storage.os.replace", side_effect=replace),
            self.assertRaises(OSError),
        ):
            write_many({a: "new-a", b: "new-b"})
        self.assertEqual(a.read_text(), "old-a")
        self.assertEqual(b.read_text(), "old-b")
        self.assertEqual(
            sorted(x.name for x in self.root.iterdir()), ["a", "b", "data"]
        )

    def test_rejected_mod_request_does_not_change_files(self):
        path = self.config.cluster / "Master/modoverrides.lua"
        old = path.read_bytes()
        with self.assertRaises(Error):
            self.runtime.mods.save(
                "Master",
                [
                    {"id": "123", "enabled": True, "options": {}},
                    {"id": "invalid", "enabled": True, "options": {}},
                ],
            )
        self.assertEqual(path.read_bytes(), old)

    def test_mod_changes_preserve_other_shard_and_download_all_ids(self):
        self.runtime.mods.save("Caves", [{"id": "111", "enabled": True, "options": {}}])
        self.runtime.mods.save(
            "Master", [{"id": "222", "enabled": True, "options": {"value": "true"}}]
        )
        self.assertIn("workshop-111", self.runtime.mods.load("Caves"))
        self.assertNotIn("workshop-222", self.runtime.mods.load("Caves"))
        setup = self.runtime.mods.setup_path().read_text()
        self.assertIn('ServerModSetup("111")', setup)
        self.assertIn('ServerModSetup("222")', setup)

    def test_lua_roundtrip_and_dynamic_mod_options(self):
        path = self.root / "roundtrip.lua"
        value = {"中文": ["x", True, False, 2.5, 'line\nnext\t"\\'], "str": "true"}
        path.write_text(lua.dumps(value))
        self.assertEqual(lua.read(path), value)
        mod = self.config.install / "mods/workshop-123/modinfo.lua"
        mod.parent.mkdir(parents=True)
        mod.write_text("""local zh=locale=="zh"
name=zh and "测试模组" or "Test"
configuration_options={}
local options={}
for i=1,3 do table.insert(options,{description=tostring(i*10),data=i*10}) end
table.insert(configuration_options,{name="stack",label=ChooseTranslationTable({zh="堆叠数量",en="Stack"}),options=options,default=20})
for i=1,2 do table.insert(configuration_options,{name="",label="分组"..i,options={{description="",data=false}},default=false}) end""")
        info = self.runtime.mods.info("123")
        self.assertEqual(info["name"], "测试模组")
        self.assertEqual(info["status"], "ready")
        self.assertEqual(len(info["fields"]), 3)
        self.assertEqual(info["fields"][0]["label"], "堆叠数量")
        self.assertEqual(
            [x["data"] for x in info["fields"][0]["options"]], [10, 20, 30]
        )

    def test_lua_has_no_host_io_and_infinite_loop_is_bounded(self):
        path = self.root / "bad.lua"
        path.write_text('return {x=os.execute("touch /tmp/dst-must-not-exist")}')
        with self.assertRaises(Error):
            lua.read(path)
        path.write_text("while true do end")
        with self.assertRaises(Error):
            lua.read(path)


class ArchiveTests(Fixture):
    def test_same_second_backups_are_distinct_and_upload_roundtrips(self):
        store = self.runtime.archives
        a = store.create(self.config.cluster, "same")
        b = store.create(self.config.cluster, "same")
        self.assertNotEqual(a["name"], b["name"])
        archive = self.root / "world.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for path in self.config.cluster.rglob("*"):
                if path.is_file():
                    z.write(
                        path,
                        str(Path("MyWorld") / path.relative_to(self.config.cluster)),
                    )
        imported = store.import_file(archive, "上传测试")
        self.assertEqual(imported["source"], "upload")
        self.assertEqual(len(store.list()), 3)

    def test_empty_cluster_archive_rejected_before_restore(self):
        archive = self.config.backups / "empty.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            item = tarfile.TarInfo("Cluster_1")
            item.type = tarfile.DIRTYPE
            t.addfile(item)
        original = (self.config.cluster / "cluster.ini").read_bytes()
        result = self.finish("restore", name=archive.name)
        self.assertEqual(result["status"], "error")
        self.assertEqual((self.config.cluster / "cluster.ini").read_bytes(), original)

    def test_traversal_links_and_unpack_limit_rejected(self):
        store = self.runtime.archives
        for name, kind in [
            ("Cluster_1/../../escape", tarfile.REGTYPE),
            ("Cluster_1/link", tarfile.SYMTYPE),
            ("Cluster_1/hard", tarfile.LNKTYPE),
        ]:
            archive = self.root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as t:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.linkname = "/tmp/escape"
                t.addfile(item)
            with (
                tempfile.TemporaryDirectory(dir=self.root) as work,
                self.assertRaises(Error),
            ):
                store.extract(archive, Path(work))
        self.config.unpack_limit = 1
        archive = self.root / "large.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Cluster_1/cluster.ini", "too large")
        with (
            tempfile.TemporaryDirectory(dir=self.root) as work,
            self.assertRaises(Error),
        ):
            store.extract(archive, Path(work))

    def test_backup_waits_for_both_shards_then_restarts(self):
        self.fake_game()
        self.runtime.start_shards()
        result = self.finish("backup", label="manual")
        self.assertEqual(result["status"], "success")
        with tarfile.open(
            self.runtime.archives.path(result["result"]["name"])
        ) as archive:
            members = archive.getnames()
        self.assertIn("Cluster_1/Master/saved-marker", members)
        self.assertIn("Cluster_1/Caves/saved-marker", members)
        self.assertTrue(all(self.runtime.status()["shards"].values()))

    def test_failed_restore_start_rolls_back_original_world(self):
        self.fake_game()
        archive = self.runtime.archives.create(self.config.cluster)
        marker = self.config.cluster / "original-marker"
        marker.write_text("keep")
        original_start = self.runtime.start_shards
        calls = 0

        def start():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise Error("simulated start failure")
            original_start()

        with patch.object(self.runtime, "start_shards", side_effect=start):
            result = self.finish("restore", name=archive["name"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse((self.config.panel / "restore-previous").exists())


class AuthHTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.auth = Auth(self.config.panel)
        self.password = self.auth.initial_password
        self.server = Server(("127.0.0.1", 0), self.runtime, self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.csrf = ""

    def request(self, path, body=None, headers=None):
        h = dict(headers or {})
        if body is not None:
            body = json.dumps(body, ensure_ascii=False).encode()
            h.setdefault("Content-Type", "application/json")
            h.setdefault("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(
                urllib.request.Request(self.base + path, data=body, headers=h),
                timeout=10,
            ) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def login(self):
        code, result = self.request("/api/login", {"password": self.password})
        self.assertEqual(code, 200)
        self.csrf = result["csrf"]

    def test_random_password_persists_and_change_revokes_sessions(self):
        self.assertGreaterEqual(len(self.password), 20)
        self.assertNotIn(self.password, self.auth.path.read_text())
        self.assertIsNone(Auth(self.config.panel).initial_password)
        self.assertEqual(self.request("/api/status")[0], 401)
        self.login()
        self.assertEqual(self.request("/api/status")[0], 200)
        self.assertEqual(
            self.request(
                "/api/password",
                {"current": self.password, "password": "新的中文面板密码123"},
            )[0],
            200,
        )
        self.assertEqual(self.request("/api/status")[0], 401)
        self.password = "新的中文面板密码123"
        self.login()

    def test_cross_origin_plain_text_and_missing_csrf_are_rejected(self):
        self.login()
        body = {"action": "backup"}
        self.assertEqual(
            self.request("/api/task", body, {"Origin": "http://other.example"})[0], 403
        )
        self.assertEqual(self.request("/api/task", body, {"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(
            self.request("/api/task", body, {"Content-Type": "text/plain"})[0], 415
        )
        self.assertEqual(self.runtime.archives.list(), [])

    def test_concurrent_config_edit_returns_conflict_without_writing(self):
        self.login()
        before = self.runtime.mods.path("Master").read_bytes()
        with self.runtime.edit():
            code, _ = self.request("/api/mods", {"shard": "Master", "mods": []})
        self.assertEqual(code, 409)
        self.assertEqual(self.runtime.mods.path("Master").read_bytes(), before)

    def test_authenticated_upload_download_and_invalid_bodies(self):
        self.login()
        archive = self.runtime.archives.create(self.config.cluster)
        raw = self.runtime.archives.path(archive["name"]).read_bytes()
        req = urllib.request.Request(
            self.base + "/api/upload",
            data=raw,
            headers={
                "X-CSRF-Token": self.csrf,
                "Content-Type": "application/octet-stream",
            },
        )
        imported = json.load(self.opener.open(req, timeout=10))["archive"]
        download = self.opener.open(
            self.base + "/api/download?name=" + imported["name"], timeout=10
        )
        self.assertTrue(
            download.headers["Content-Disposition"].startswith("attachment")
        )
        self.assertEqual(
            download.read(), self.runtime.archives.path(imported["name"]).read_bytes()
        )
        self.assertEqual(
            self.request("/api/mods", {"shard": "Master", "mods": ["invalid"]})[0], 400
        )
        self.assertEqual(self.request("/api/world", [])[0], 400)
        _, world = self.request("/api/world")
        world.update(shard="Master", token="   ")
        self.assertEqual(self.request("/api/world", world)[0], 400)
        self.assertTrue(self.runtime.has_token())


if __name__ == "__main__":
    unittest.main()

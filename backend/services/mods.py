import re

from ..core.lua import codec as lua
from ..core.storage import Error, write_many

SHARDS = ("Master", "Caves")


def shard_name(value):
    if value not in SHARDS:
        raise Error("分片必须是 Master 或 Caves")
    return value


def workshop_id(value):
    value = str(value).removeprefix("workshop-")
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise Error("请输入有效的创意工坊数字 ID")
    return value


class Mods:
    def __init__(self, config):
        self.config = config
        self.cache = {}

    def path(self, shard):
        return self.config.cluster / shard_name(shard) / "modoverrides.lua"

    def load(self, shard):
        path = self.path(shard)
        table = lua.read(path) if path.exists() else {}
        if not isinstance(table, dict):
            raise Error("模组配置必须是键值表")
        for key, value in table.items():
            if key == "client_mods_disabled" and isinstance(value, bool):
                continue
            if not re.fullmatch(r"workshop-[0-9]{1,20}", key) or not isinstance(
                value, dict
            ):
                raise Error("模组配置包含无法编辑的项目，原文件已保留")
        return table

    def ids(self, tables=None):
        tables = tables or {s: self.load(s) for s in SHARDS}
        return sorted(
            {
                k.removeprefix("workshop-")
                for t in tables.values()
                for k in t
                if k.startswith("workshop-")
            }
        )

    def setup_text(self, tables=None, extra=()):
        ids = sorted(set(self.ids(tables)) | set(extra))
        return "".join(f'ServerModSetup("{workshop_id(i)}")\n' for i in ids)

    def setup_path(self):
        return self.config.install / "mods/dedicated_server_mods_setup.lua"

    def sync(self, extra=()):
        write_many({self.setup_path(): self.setup_text(extra=extra)})

    def info_path(self, mid):
        mid = workshop_id(mid)
        roots = [self.config.install / "mods", self.config.install / "ugc"]
        for root in roots:
            direct = root / ("workshop-" + mid) / "modinfo.lua"
            if (
                direct.is_file()
                and not direct.is_symlink()
                and direct.resolve().is_relative_to(root.resolve())
            ):
                return direct
            if root.is_dir():
                for path in root.glob("**/" + mid + "/modinfo.lua"):
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and path.resolve().is_relative_to(root.resolve())
                    ):
                        return path
        return None

    def info(self, mid):
        path = self.info_path(mid)
        if not path:
            return {
                "name": "workshop-" + mid,
                "status": "missing",
                "fields": [],
                "message": "模组文件尚未下载，点击下载配置后再编辑",
            }
        stamp = (str(path), path.stat().st_mtime_ns)
        if stamp in self.cache:
            return self.cache[stamp]
        try:
            raw = lua.read(path, "modinfo", "workshop-" + mid)
            fields = raw.get("configuration_options") or []
            if not isinstance(fields, list):
                raise Error("模组配置项格式不受支持")
            clean, seen = [], set()
            for field in fields:
                if not isinstance(field, dict) or not isinstance(
                    field.get("name"), str
                ):
                    raise Error("模组配置项缺少名称")
                options = field.get("options") or []
                if not isinstance(options, list) or not all(
                    isinstance(x, dict) and "data" in x for x in options
                ):
                    raise Error("模组选项格式不受支持")
                heading = len(options) == 1 and options[0].get("description") == ""
                if not heading:
                    if field["name"] in seen:
                        raise Error("模组包含重复配置项")
                    seen.add(field["name"])
                clean.append(
                    {
                        "name": field["name"],
                        "label": str(field.get("label") or field["name"]),
                        "hover": str(field.get("hover") or ""),
                        "default": field.get("default"),
                        "options": [
                            {
                                "description": str(x.get("description", x["data"])),
                                "data": x["data"],
                            }
                            for x in options
                        ],
                    }
                )
            info = {
                "name": str(raw.get("name") or "workshop-" + mid),
                "status": "ready",
                "version": str(raw.get("version") or ""),
                "fields": clean,
                "client_only": bool(raw.get("client_only_mod")),
            }
        except Error as exc:
            info = {
                "name": "workshop-" + mid,
                "status": "unsupported",
                "fields": [],
                "message": str(exc) + "；现有配置值会保留",
            }
        if len(self.cache) > 256:
            self.cache.clear()
        self.cache[stamp] = info
        return info

    def view(self, shard):
        table = self.load(shard)
        return [
            {
                "id": key.removeprefix("workshop-"),
                "enabled": value.get("enabled", False),
                "options": value.get("configuration_options") or {},
                "info": self.info(key.removeprefix("workshop-")),
            }
            for key, value in table.items()
            if key != "client_mods_disabled"
        ]

    def save(self, shard, mods, both=False):
        if not isinstance(mods, list) or len(mods) > 200:
            raise Error("模组列表不合法或超过 200 个")
        shard_name(shard)
        tables = {s: self.load(s) for s in SHARDS}
        table = {k: v for k, v in tables[shard].items() if k == "client_mods_disabled"}
        for mod in mods:
            if not isinstance(mod, dict):
                raise Error("模组项必须是对象")
            mid = workshop_id(mod.get("id", ""))
            key = "workshop-" + mid
            if key in table:
                raise Error("模组 ID 重复")
            if not isinstance(mod.get("enabled"), bool) or not isinstance(
                mod.get("options"), dict
            ):
                raise Error("模组启用状态或配置格式不正确")
            old = tables[shard].get(key, {}).get("configuration_options") or {}
            info = self.info(mid)
            for field in info["fields"]:
                name = field["name"]
                if name not in mod["options"] or (
                    type(mod["options"][name]) is type(old.get(name))
                    and mod["options"][name] == old.get(name)
                ):
                    continue
                choices = field["options"]
                if choices and not any(
                    type(x["data"]) is type(mod["options"][name])
                    and x["data"] == mod["options"][name]
                    for x in choices
                ):
                    raise Error(f"{field['label']} 的值不在模组允许范围内")
            table[key] = {
                "enabled": mod["enabled"],
                "configuration_options": mod["options"],
            }
        tables[shard] = table
        if both:
            tables = {s: table for s in SHARDS}
        contents = {
            self.path(s): lua.dumps(tables[s]) for s in SHARDS if both or s == shard
        }
        contents[self.setup_path()] = self.setup_text(tables)
        write_many(contents)

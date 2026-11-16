from ..core.lua import codec as lua
from ..core.storage import Error, write


def config_path(cluster, shard):
    root = cluster / shard
    worldgen = root / "worldgenoverride.lua"
    level = root / "leveldataoverride.lua"
    return worldgen if worldgen.exists() or not level.exists() else level


def validate(value, filename):
    if not isinstance(value, dict) or not isinstance(value.get("overrides", {}), dict):
        raise Error(f"{filename} 必须返回包含 overrides 的键值表")
    if filename == "leveldataoverride.lua":
        if not all(
            isinstance(value.get(k), str) for k in ("id", "name", "desc", "location")
        ):
            raise Error(
                "leveldataoverride.lua 必须包含 id、name、desc、location 和 overrides；预设配置请使用 worldgenoverride.lua"
            )
        if "overrides" not in value:
            raise Error("leveldataoverride.lua 缺少 overrides")
    elif not isinstance(value.get("preset", ""), str):
        raise Error("世界预设名称必须是字符串")


def repair_preset_filename(cluster):
    """修正曾将 worldgen 配置误存为 leveldata 的模板，保留用户参数。"""
    for shard in ("Master", "Caves"):
        old = cluster / shard / "leveldataoverride.lua"
        target = old.with_name("worldgenoverride.lua")
        if not old.is_file():
            continue
        value = lua.read(old)
        if (
            isinstance(value, dict)
            and set(value) <= {"preset", "override_enabled", "overrides"}
            and value.get("preset") in ("SURVIVAL_TOGETHER", "DST_CAVE")
            and isinstance(value.get("overrides"), dict)
        ):
            if target.exists() and lua.read(target) != value:
                raise Error(f"{shard} 同时存在冲突的世界配置，请检查两个 override 文件")
            write(target, lua.dumps(value))
            old.unlink()

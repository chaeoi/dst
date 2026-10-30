import json
import math
import subprocess
import sys
from pathlib import Path

from ..storage import Error


def read(path, mode="config", folder=""):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise Error("配置文件为链接或超过 2 MB")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("reader.py")), mode, folder],
            input=path.read_bytes(),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error("配置读取超时或 Lua 读取器不可用") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").splitlines()
        raise Error(
            "无法读取配置：" + (message[0][:250] if message else "计算超出限制")
        )
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise Error("配置读取结果不合法") from exc


def encode(value, depth=0):
    if depth > 40:
        raise Error("配置嵌套过深")
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise Error("配置值不能是无穷大或 NaN")
        return str(value)
    if isinstance(value, str):
        # Lua 5.1 没有 JSON 的 \u 转义；控制字符使用三位十进制转义。
        return (
            '"'
            + "".join(
                f"\\{ord(c):03d}" if ord(c) < 32 else "\\" + c if c in '\\"' else c
                for c in value
            )
            + '"'
        )
    pad, child = "  " * depth, "  " * (depth + 1)
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise Error("配置字典的键必须是字符串")
        items = [
            "[" + encode(k) + "]=" + encode(v, depth + 1) for k, v in value.items()
        ]
    elif isinstance(value, list):
        items = [encode(v, depth + 1) for v in value]
    else:
        raise Error("不支持的配置值类型")
    return (
        "{\n" + ",\n".join(child + x for x in items) + "\n" + pad + "}"
        if items
        else "{}"
    )


def dumps(value):
    if not isinstance(value, dict):
        raise Error("配置文件顶层必须是键值表")
    return "return " + encode(value) + "\n"

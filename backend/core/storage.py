import configparser
import io
import os
import tempfile
from pathlib import Path


class Error(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def write_many(contents):
    """调用者持有配置锁；所有文件先落盘，提交失败时还原已替换文件。"""
    staged, old, committed = {}, {}, []
    try:
        for path, content in contents.items():
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            old[path] = path.read_bytes() if path.exists() else None
            fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
            staged[path] = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(
                    content.encode("utf-8") if isinstance(content, str) else content
                )
                handle.flush()
                os.fsync(handle.fileno())
        for path, tmp in staged.items():
            os.replace(tmp, path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            if old[path] is None:
                path.unlink(missing_ok=True)
            else:
                # 临时文件仍在同一文件系统，不把未完成写入暴露给读取者。
                fd, name = tempfile.mkstemp(dir=path.parent)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(old[path])
                os.replace(name, path)
        raise
    finally:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)


def write(path, content):
    write_many({Path(path): content})


def read_ini(path):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, configparser.Error, UnicodeError) as exc:
        raise Error(f"{Path(path).name} 无法读取：{exc}") from exc
    return {section: dict(parser[section]) for section in parser.sections()}


def ini_text(sections):
    if not isinstance(sections, dict):
        raise Error("服务器设置必须是对象")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    import re

    for name, entries in sections.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or not isinstance(entries, dict)
        ):
            raise Error("配置段格式不正确")
        parser.add_section(name)
        for key, value in entries.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                raise Error("配置项名称不合法")
            value = str(value)
            if "\n" in value or "\r" in value or "\0" in value:
                raise Error(f"{key} 不能包含换行或空字符")
            parser.set(name, key, value)
    result = io.StringIO()
    parser.write(result)
    return result.getvalue()

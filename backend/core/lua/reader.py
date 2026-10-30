"""在独立进程内限制 Lua 读取器资源，不在 HTTP 线程里使用 preexec_fn。"""

import os
import resource
import sys
from pathlib import Path

resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
os.execvp(
    "lua5.1", ["lua5.1", str(Path(__file__).with_name("sandbox.lua")), *sys.argv[1:]]
)

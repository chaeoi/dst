import logging
import signal
import sys
import threading

from backend.api.server import Server
from backend.core.auth import Auth
from backend.core.config import Config
from backend.services.runtime import Runtime

logger = logging.getLogger(__name__)


def main():
    config = Config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.logs / "panel.log", encoding="utf-8"),
        ],
    )
    auth = Auth(config.panel)
    if auth.initial_password:
        # 随机初始密码只打印到容器输出，不写入可下载的面板日志。
        print(
            "\n首次启动面板密码："
            + auth.initial_password
            + "\n登录后可在右上角修改密码。\n",
            flush=True,
        )
        auth.initial_password = None
    runtime = Runtime(config)
    server = Server((config.host, config.port), runtime, auth)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=runtime.monitor, daemon=True).start()
    logger.info("面板已监听 %s:%s", config.host, config.port)
    runtime.submit("start")
    stopped.wait()
    logger.info("正在保存世界并停止服务器")
    runtime.close()
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()

"""HTTP 服务启动入口：python -m grid_commitment.server --port 8080 [--db path]"""

from __future__ import annotations

import argparse
import time

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="新能源接入承诺清算服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="grid_commitment.db", help="SQLite 文件路径，:memory: 仅测试")
    args = parser.parse_args()

    server, _service = serve(args.host, args.port, args.db)
    print(f"grid-commitment 服务已启动: http://{args.host}:{args.port} (db={args.db})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

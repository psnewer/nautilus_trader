#!/usr/bin/env python
"""
启动 Web Gateway 服务

用法:
    python scripts/run_web_gateway.py [--host HOST] [--port PORT] [--debug]

示例:
    python scripts/run_web_gateway.py
    python scripts/run_web_gateway.py --port 8000
    python scripts/run_web_gateway.py --host 0.0.0.0 --port 8080 --debug
"""

import argparse
import logging
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(
        description="Arbitrage Web Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python scripts/run_web_gateway.py
    python scripts/run_web_gateway.py --port 8000
    python scripts/run_web_gateway.py --host 0.0.0.0 --port 8080 --debug
        """,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="绑定的主机地址 (默认: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="绑定的端口 (默认: 8080)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启调试模式",
    )
    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # 启动服务
    from src.arbitrage.services.web_gateway import WebGatewayService
    from src.arbitrage.services.web_gateway.config import WebGatewayConfig

    config = WebGatewayConfig(
        host=args.host,
        port=args.port,
        debug=args.debug,
    )

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              Arbitrage Web Gateway                           ║
╠══════════════════════════════════════════════════════════════╣
║  URL: http://{args.host}:{args.port:<5}                              ║
║  Debug: {str(args.debug):<6}                                          ║
╚══════════════════════════════════════════════════════════════╝
    """)

    service = WebGatewayService(config)
    service.run()


if __name__ == "__main__":
    main()

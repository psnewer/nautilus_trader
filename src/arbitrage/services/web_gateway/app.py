"""
Web Gateway 应用入口

提供 FastAPI 应用和启动方法。
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import WebGatewayConfig
from .routes import config_router, discovery_router, matching_router, odds_router, strategy_router

# 模板目录
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: WebGatewayConfig | None = None) -> FastAPI:
    """
    创建 FastAPI 应用

    Args:
        config: Web Gateway 配置

    Returns:
        FastAPI 应用实例
    """
    if config is None:
        config = WebGatewayConfig()

    app = FastAPI(
        title=config.title,
        description="Arbitrage System Web Dashboard",
        version="1.0.0",
    )

    # 注册路由
    app.include_router(discovery_router)
    app.include_router(matching_router)
    app.include_router(config_router)
    app.include_router(odds_router)
    app.include_router(strategy_router)

    # 静态文件
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # 首页
    @app.get("/", response_class=HTMLResponse)
    async def index():
        """返回首页 HTML"""
        index_file = TEMPLATES_DIR / "index.html"
        if index_file.exists():
            return index_file.read_text()
        return "<h1>Arbitrage Dashboard</h1><p>Template not found.</p>"

    # 健康检查
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


class WebGatewayService:
    """
    Web Gateway 服务

    封装应用启动和管理。
    """

    def __init__(self, config: WebGatewayConfig | None = None):
        self.config = config or WebGatewayConfig()
        self.app = create_app(self.config)
        self._log = logging.getLogger(self.__class__.__name__)

    def run(self):
        """启动服务（阻塞）"""
        import uvicorn

        self._log.info(
            f"Starting Web Gateway at http://{self.config.host}:{self.config.port}"
        )
        uvicorn.run(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="info" if self.config.debug else "warning",
        )

    async def start_async(self):
        """异步启动服务"""
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="info" if self.config.debug else "warning",
        )
        server = uvicorn.Server(config)
        await server.serve()


# CLI 入口点
def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="Arbitrage Web Gateway")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    config = WebGatewayConfig(
        host=args.host,
        port=args.port,
        debug=args.debug,
    )
    service = WebGatewayService(config)
    service.run()


if __name__ == "__main__":
    main()

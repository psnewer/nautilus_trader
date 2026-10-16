"""启动本地 Polymarket paper execution 服务。"""

# ruff: noqa: E402 -- 直接运行 launcher 前需先把项目根加入 sys.path。

from __future__ import annotations

import argparse
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import validate_execution_config
from src.arbitrage.paper.polymarket import PaperServerConfig
from src.arbitrage.paper.polymarket import run_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket local paper execution server")
    parser.add_argument("--config", default=str(_PROJECT_ROOT / "arb_config.json"))
    args = parser.parse_args(argv)
    cfg = load_arb_config(args.config)
    validate_execution_config(cfg)
    if cfg.execution.mode != "paper":
        parser.error("execution.mode must be 'paper' to start the paper server")
    paper = cfg.execution.paper
    run_server(
        PaperServerConfig(
            host=paper.host,
            port=paper.port,
            database_path=paper.database_path,
            starting_balance=paper.starting_balance,
            reset=paper.reset,
            market_http_url=cfg.venues.polymarket.clob_url,
            market_ws_url=cfg.venues.polymarket.ws_url.rstrip("/") + "/market",
            market_proxy_url=cfg.venues.polymarket.proxy_url,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

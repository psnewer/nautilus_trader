"""
ScenarioRunner

负责一次测试的完整生命周期:
  1. 合并 debug_config + scenario.debug_overrides 进 debug_manager
  2. 启动 LogMonitor（注册 logging.Handler）
  3. 启动 web_gateway 异步服务
  4. 评估器循环：从队列取日志，喂给 success/failure 条件
  5. 任一条件满足 / 超时 -> 停服务、出报告
  6. 落盘 JSON 报告到 test_runs/

退出码:
  0 - PASS
  1 - FAIL
  2 - TIMEOUT
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .conditions import Condition, ConditionResult
from .monitor import LogEvent, LogMonitor
from .scenario import TestScenario


_log = logging.getLogger("ScenarioRunner")


@dataclass
class ScenarioReport:
    scenario_name: str
    outcome: str  # PASS / FAIL / TIMEOUT
    started_at: float
    ended_at: float
    duration_sec: float
    success_result: dict[str, Any] | None = None
    failure_result: dict[str, Any] | None = None
    captured_count: int = 0
    notable_events: list[dict[str, Any]] = field(default_factory=list)
    debug_overrides_applied: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScenarioRunner:
    def __init__(
        self,
        scenario: TestScenario,
        *,
        debug_config_path: str = "debug_config.json",
        host: str = "127.0.0.1",
        port: int = 8080,
        report_dir: str = "test_runs",
    ) -> None:
        self._scenario = scenario
        self._debug_config_path = debug_config_path
        self._host = host
        self._port = port
        self._report_dir = Path(report_dir)

    async def run(self) -> ScenarioReport:
        # 1. 加载 debug_config + 应用 scenario 部分覆盖
        from src.arbitrage.services.debug import debug_manager

        if Path(self._debug_config_path).exists():
            debug_manager.load(self._debug_config_path)
        debug_manager.enable()
        applied: dict[str, Any] = {}
        for name, (enabled, value) in self._scenario.effective_overrides.items():
            ok = debug_manager.set_override(name, enabled=enabled, value=value)
            if not ok:
                _log.warning(f"override {name} not in debug config, skipping")
                continue
            applied[name] = {"enabled": enabled, "value": value}

        # 2. 起 LogMonitor + web_gateway + 评估器
        success: Condition | None = self._scenario.get_success()
        failure: Condition | None = self._scenario.get_failure()
        timeout = self._scenario.timeout_sec

        monitor = LogMonitor()
        monitor.start()
        started_at = time.time()

        outcome = "TIMEOUT"
        try:
            server = await self._start_server()
            try:
                # 触发 setup（默认: 起 pipeline）
                await self._setup(self._scenario)
                outcome = await self._evaluate(monitor, success, failure, timeout)
            finally:
                await self._stop_server(server)
        finally:
            monitor.stop()

        ended_at = time.time()

        # 3. 整理报告
        report = ScenarioReport(
            scenario_name=self._scenario.name,
            outcome=outcome,
            started_at=started_at,
            ended_at=ended_at,
            duration_sec=ended_at - started_at,
            success_result=success.result().to_dict() if success else None,
            failure_result=failure.result().to_dict() if failure else None,
            captured_count=len(monitor.captured),
            notable_events=self._extract_notable(monitor.captured, success, failure),
            debug_overrides_applied=applied,
        )

        self._print_report(report)
        self._save_report(report)
        return report

    async def _start_server(self):
        """异步起 web_gateway 服务，返回 (server, task)"""
        from src.arbitrage.services.web_gateway.app import WebGatewayService
        from src.arbitrage.services.web_gateway.config import WebGatewayConfig

        config = WebGatewayConfig(host=self._host, port=self._port)
        service = WebGatewayService(config)

        import uvicorn

        u_config = uvicorn.Config(
            service.app,
            host=self._host,
            port=self._port,
            log_level="info",
        )
        server = uvicorn.Server(u_config)
        task = asyncio.create_task(server.serve())
        # 等待 startup
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)
        _log.info(f"WebGateway started at http://{self._host}:{self._port}")
        return (server, task)

    async def _setup(self, scenario: TestScenario) -> None:
        """
        执行 scenario 的 setup 钩子。

        默认行为：若 scenario.auto_start_pipeline 为 True，调用 POST /api/pipeline/start
        触发 Discovery → Matching → OddsSubscription 全链路。
        scenario 可覆盖 setup() 实现自定义启动序列。
        """
        custom = getattr(scenario, "setup", None)
        if custom is not None and callable(custom):
            try:
                result = custom(self)
                if asyncio.iscoroutine(result):
                    await result
                _log.info(f"Scenario setup hook completed")
                return
            except Exception as e:
                _log.error(f"Scenario setup hook failed: {e}")
                raise

        if getattr(scenario, "auto_start_pipeline", True):
            import urllib.request

            url = f"http://{self._host}:{self._port}/api/pipeline/start"
            try:
                # 简单的同步 POST（在 asyncio.to_thread 中跑避免阻塞）
                def _post() -> tuple[int, str]:
                    req = urllib.request.Request(url, method="POST", data=b"")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status, resp.read().decode()
                status, body = await asyncio.to_thread(_post)
                _log.info(f"Pipeline start: status={status}, body={body}")
            except Exception as e:
                _log.error(f"Failed to start pipeline: {e}")
                raise

    async def _stop_server(self, server_task) -> None:
        server, task = server_task
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except asyncio.TimeoutError:
            _log.warning("WebGateway shutdown timed out")
            task.cancel()

    async def _evaluate(
        self,
        monitor: LogMonitor,
        success: Condition | None,
        failure: Condition | None,
        timeout: float,
    ) -> str:
        deadline = time.time() + timeout if timeout else None
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return "TIMEOUT"
            event = await monitor.next_event(timeout=remaining)
            if event is None:
                return "TIMEOUT"
            if success is not None:
                success.feed(event)
            if failure is not None:
                failure.feed(event)
            if failure is not None and failure.met():
                return "FAIL"
            if success is not None and success.met():
                return "PASS"

    def _extract_notable(
        self,
        captured: list[LogEvent],
        success: Condition | None,
        failure: Condition | None,
    ) -> list[dict[str, Any]]:
        """关键事件: success/failure 命中的具体 LogEvent + 所有 ERROR/WARNING"""
        notable: list[dict[str, Any]] = []
        seen: set[int] = set()

        def flatten(result: ConditionResult) -> list[LogEvent]:
            out: list[LogEvent] = list(result.matched_events)
            for c in result.children:
                out.extend(flatten(c))
            return out

        for cond in (success, failure):
            if cond is None:
                continue
            for ev in flatten(cond.result()):
                key = id(ev)
                if key in seen:
                    continue
                seen.add(key)
                notable.append(self._event_to_dict(ev))

        for ev in captured:
            if ev.level_no >= logging.WARNING:
                key = id(ev)
                if key in seen:
                    continue
                seen.add(key)
                notable.append(self._event_to_dict(ev))

        return notable

    @staticmethod
    def _event_to_dict(ev: LogEvent) -> dict[str, Any]:
        return {
            "timestamp": ev.timestamp,
            "logger": ev.logger,
            "level": ev.level,
            "message": ev.message,
        }

    def _print_report(self, report: ScenarioReport) -> None:
        sep = "=" * 60
        print(f"\n{sep}\nSCENARIO REPORT: {report.scenario_name}\n{sep}")
        print(f"Outcome:        {report.outcome}")
        print(f"Duration:       {report.duration_sec:.2f}s")
        print(f"Captured logs:  {report.captured_count}")
        if report.debug_overrides_applied:
            print("Overrides applied:")
            for name, info in report.debug_overrides_applied.items():
                print(f"  - {name}: {info}")
        if report.success_result:
            print("\nSuccess condition:")
            self._print_condition_tree(report.success_result, indent=2)
        if report.failure_result:
            print("\nFailure condition:")
            self._print_condition_tree(report.failure_result, indent=2)
        if report.notable_events:
            print("\nNotable events:")
            for ev in report.notable_events[-50:]:
                ts = time.strftime("%H:%M:%S", time.localtime(ev["timestamp"]))
                print(f"  [{ts}] {ev['level']:7s} {ev['logger']}: {ev['message']}")
        print(sep + "\n")

    def _print_condition_tree(self, result: dict[str, Any], indent: int) -> None:
        prefix = " " * indent
        mark = "✓" if result["met"] else "✗"
        print(f"{prefix}{mark} {result['name']}")
        for ev in result.get("matched_events", []):
            ts = time.strftime("%H:%M:%S", time.localtime(ev["timestamp"]))
            print(f"{prefix}    [{ts}] {ev['level']} {ev['logger']}: {ev['message']}")
        for child in result.get("children", []):
            self._print_condition_tree(child, indent + 2)

    def _save_report(self, report: ScenarioReport) -> Path:
        self._report_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.started_at))
        path = self._report_dir / f"{ts}_{report.scenario_name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        _log.info(f"Report saved: {path}")
        return path


def load_scenario(name: str) -> TestScenario:
    """通过名字（模块短名）加载场景类"""
    module_name = f"src.arbitrage.testing.scenarios.{name}"
    module = importlib.import_module(module_name)
    # 找到模块里第一个 TestScenario 子类
    for attr in dir(module):
        obj = getattr(module, attr)
        if (
            isinstance(obj, type)
            and issubclass(obj, TestScenario)
            and obj is not TestScenario
        ):
            return obj()
    raise RuntimeError(f"No TestScenario subclass found in {module_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Arbitrage scenario test runner")
    parser.add_argument("--scenario", required=True, help="Scenario module name under scenarios/")
    parser.add_argument("--debug-config", default="debug_config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--report-dir", default="test_runs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    scenario = load_scenario(args.scenario)
    runner = ScenarioRunner(
        scenario,
        debug_config_path=args.debug_config,
        host=args.host,
        port=args.port,
        report_dir=args.report_dir,
    )
    report = asyncio.run(runner.run())

    code = {"PASS": 0, "FAIL": 1, "TIMEOUT": 2}.get(report.outcome, 1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

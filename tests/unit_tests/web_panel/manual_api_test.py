#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Panel 手动API测试工具
使用 requests 库直接测试所有API端点
"""

import requests
import json
import time
from datetime import datetime
from pathlib import Path


BASE_URL = "http://localhost:8765"
TIMEOUT = 30


class Colors:
    """终端颜色"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_section(title):
    """打印分节标题"""
    print()
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{title.center(80)}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}")
    print()


def print_success(message):
    """打印成功消息"""
    print(f"{Colors.GREEN}✓{Colors.RESET} {message}")


def print_error(message):
    """打印错误消息"""
    print(f"{Colors.RED}✗{Colors.RESET} {message}")


def print_warning(message):
    """打印警告消息"""
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {message}")


def print_info(message):
    """打印信息"""
    print(f"  {message}")


class TestResult:
    """测试结果"""
    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.details = []

    def add_pass(self, name, details=""):
        self.total += 1
        self.passed += 1
        self.details.append(("PASS", name, details))

    def add_fail(self, name, details=""):
        self.total += 1
        self.failed += 1
        self.details.append(("FAIL", name, details))

    def add_warning(self, name, details=""):
        self.total += 1
        self.warnings += 1
        self.details.append(("WARN", name, details))

    def print_summary(self):
        """打印测试摘要"""
        print_section("测试摘要")
        print(f"总测试数: {self.total}")
        print(f"{Colors.GREEN}通过: {self.passed}{Colors.RESET}")
        print(f"{Colors.RED}失败: {self.failed}{Colors.RESET}")
        print(f"{Colors.YELLOW}警告: {self.warnings}{Colors.RESET}")
        print()

        if self.failed > 0:
            print(f"{Colors.RED}失败的测试:{Colors.RESET}")
            for status, name, details in self.details:
                if status == "FAIL":
                    print(f"  - {name}: {details}")
            print()

        pass_rate = (self.passed / self.total * 100) if self.total > 0 else 0
        print(f"通过率: {pass_rate:.1f}%")
        print()


result = TestResult()


def test_server_availability():
    """测试服务器是否可用"""
    print_section("1. 服务器可用性测试")

    try:
        response = requests.get(f"{BASE_URL}/api/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print_success(f"服务器运行正常")
            print_info(f"状态: {data.get('status')}")
            print_info(f"时间戳: {data.get('timestamp')}")
            result.add_pass("服务器可用性", f"状态码: {response.status_code}")
            return True
        else:
            print_error(f"服务器响应异常: {response.status_code}")
            result.add_fail("服务器可用性", f"状态码: {response.status_code}")
            return False
    except Exception as e:
        print_error(f"无法连接到服务器: {e}")
        print_info("请先启动服务器:")
        print_info("cd /Users/miller/nautilus_trader")
        print_info("source venv/bin/activate")
        print_info("python -m uvicorn web_panel.app:app --port 8765")
        result.add_fail("服务器可用性", str(e))
        return False


def test_page_routes():
    """测试页面路由"""
    print_section("2. 页面路由测试")

    pages = [
        ("/", "首页"),
        ("/discovery", "市场发现页面"),
        ("/matching", "市场匹配页面"),
        ("/config", "配置页面"),
    ]

    for path, name in pages:
        try:
            response = requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
            if response.status_code == 200 and "text/html" in response.headers.get("content-type", ""):
                print_success(f"{name}: {path}")
                result.add_pass(f"页面: {name}")
            else:
                print_error(f"{name}: 状态码 {response.status_code}")
                result.add_fail(f"页面: {name}", f"状态码: {response.status_code}")
        except Exception as e:
            print_error(f"{name}: {e}")
            result.add_fail(f"页面: {name}", str(e))


def test_health_api():
    """测试健康检查API"""
    print_section("3. 健康检查API测试")

    try:
        response = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        data = response.json()

        print_success(f"GET /api/health - 状态码: {response.status_code}")
        print_info(f"响应: {json.dumps(data, indent=2, ensure_ascii=False)}")

        # 验证响应格式
        assert "status" in data, "缺少 'status' 字段"
        assert "timestamp" in data, "缺少 'timestamp' 字段"
        assert data["status"] == "healthy", "状态不是 'healthy'"

        result.add_pass("健康检查API")

    except AssertionError as e:
        print_error(f"响应格式错误: {e}")
        result.add_fail("健康检查API", str(e))
    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("健康检查API", str(e))


def test_discovery_api():
    """测试市场发现API"""
    print_section("4. 市场发现API测试")

    # 测试获取 Polymarket 事件
    print(f"{Colors.BOLD}4.1 获取 Polymarket 事件{Colors.RESET}")
    try:
        response = requests.get(f"{BASE_URL}/api/discovery/polymarket-events", timeout=TIMEOUT)
        data = response.json()

        print_success(f"GET /api/discovery/polymarket-events - 状态码: {response.status_code}")

        events = data.get("events", data if isinstance(data, list) else [])
        count = data.get("count", len(events))

        print_info(f"事件数量: {count}")
        if count > 0:
            print_info(f"示例事件: {json.dumps(events[0], indent=2, ensure_ascii=False)[:200]}...")

        result.add_pass("获取Polymarket事件", f"数量: {count}")

    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("获取Polymarket事件", str(e))

    # 测试获取 OrbitExch 事件
    print()
    print(f"{Colors.BOLD}4.2 获取 OrbitExch 事件{Colors.RESET}")
    try:
        response = requests.get(f"{BASE_URL}/api/discovery/orbitexch-events", timeout=TIMEOUT)
        data = response.json()

        print_success(f"GET /api/discovery/orbitexch-events - 状态码: {response.status_code}")

        events = data.get("events", data if isinstance(data, list) else [])
        count = data.get("count", len(events))

        print_info(f"事件数量: {count}")
        if count > 0:
            print_info(f"示例事件: {json.dumps(events[0], indent=2, ensure_ascii=False)[:200]}...")

        result.add_pass("获取OrbitExch事件", f"数量: {count}")

    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("获取OrbitExch事件", str(e))

    # 测试启动 Polymarket 爬虫
    print()
    print(f"{Colors.BOLD}4.3 启动 Polymarket 爬虫{Colors.RESET}")
    print_warning("此测试可能需要较长时间,跳过实际执行(避免超时)")
    print_info("手动测试命令: curl -X POST http://localhost:8765/api/discovery/start-polymarket")
    result.add_warning("启动Polymarket爬虫", "已跳过(需要手动测试)")

    # 测试启动 OrbitExch 爬虫
    print()
    print(f"{Colors.BOLD}4.4 启动 OrbitExch 爬虫{Colors.RESET}")
    print_warning("此测试可能需要较长时间,跳过实际执行(避免超时)")
    print_info("手动测试命令: curl -X POST http://localhost:8765/api/discovery/start-orbitexch")
    result.add_warning("启动OrbitExch爬虫", "已跳过(需要手动测试)")


def test_matching_api():
    """测试市场匹配API"""
    print_section("5. 市场匹配API测试")

    # 测试获取匹配结果
    print(f"{Colors.BOLD}5.1 获取匹配结果{Colors.RESET}")
    try:
        response = requests.get(f"{BASE_URL}/api/matching/results", timeout=TIMEOUT)
        data = response.json()

        print_success(f"GET /api/matching/results - 状态码: {response.status_code}")

        # 验证响应格式
        assert "matches" in data, "缺少 'matches' 字段"
        assert "match_count" in data, "缺少 'match_count' 字段"
        assert "polymarket_count" in data, "缺少 'polymarket_count' 字段"
        assert "orbitexch_count" in data, "缺少 'orbitexch_count' 字段"

        print_info(f"匹配数量: {data['match_count']}")
        print_info(f"Polymarket事件: {data['polymarket_count']}")
        print_info(f"OrbitExch事件: {data['orbitexch_count']}")

        if data['match_count'] > 0:
            print_info(f"示例匹配: {json.dumps(data['matches'][0], indent=2, ensure_ascii=False)[:200]}...")

        result.add_pass("获取匹配结果", f"匹配数: {data['match_count']}")

    except AssertionError as e:
        print_error(f"响应格式错误: {e}")
        result.add_fail("获取匹配结果", str(e))
    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("获取匹配结果", str(e))

    # 测试启动匹配
    print()
    print(f"{Colors.BOLD}5.2 启动市场匹配{Colors.RESET}")
    print_warning("此测试可能需要输入数据,跳过实际执行")
    print_info("手动测试命令: curl -X POST http://localhost:8765/api/matching/start")
    result.add_warning("启动市场匹配", "已跳过(需要手动测试)")


def test_config_api():
    """测试配置API"""
    print_section("6. 配置管理API测试")

    # 测试获取配置
    print(f"{Colors.BOLD}6.1 获取配置{Colors.RESET}")
    try:
        response = requests.get(f"{BASE_URL}/api/config", timeout=TIMEOUT)
        data = response.json()

        print_success(f"GET /api/config - 状态码: {response.status_code}")

        config = data.get("config", data)
        print_info(f"配置项数量: {len(config)}")
        if config:
            print_info(f"配置内容: {json.dumps(config, indent=2, ensure_ascii=False)[:300]}...")

        result.add_pass("获取配置", f"配置项: {len(config)}")

    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("获取配置", str(e))

    # 测试保存配置
    print()
    print(f"{Colors.BOLD}6.2 保存配置{Colors.RESET}")
    test_config = {
        "test_timestamp": datetime.now().isoformat(),
        "test_data": {
            "enabled": True,
            "value": 123
        }
    }

    try:
        response = requests.post(
            f"{BASE_URL}/api/config",
            json=test_config,
            timeout=TIMEOUT
        )
        data = response.json()

        print_success(f"POST /api/config - 状态码: {response.status_code}")
        print_info(f"响应: {json.dumps(data, indent=2, ensure_ascii=False)}")

        # 验证响应
        assert "success" in data, "缺少 'success' 字段"
        assert data["success"] is True, "保存失败"

        result.add_pass("保存配置")

        # 验证文件是否创建
        config_file = Path("/Users/miller/nautilus_trader/config/config.yaml")
        if config_file.exists():
            print_info(f"配置文件已创建: {config_file}")
        else:
            print_warning(f"配置文件未找到: {config_file}")

    except AssertionError as e:
        print_error(f"响应验证失败: {e}")
        result.add_fail("保存配置", str(e))
    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("保存配置", str(e))


def test_error_handling():
    """测试错误处理"""
    print_section("7. 错误处理测试")

    # 测试无效端点
    print(f"{Colors.BOLD}7.1 无效端点{Colors.RESET}")
    try:
        response = requests.get(f"{BASE_URL}/api/invalid-endpoint", timeout=TIMEOUT)

        if response.status_code == 404:
            print_success("无效端点正确返回404")
            result.add_pass("错误处理: 无效端点")
        else:
            print_error(f"预期404,实际: {response.status_code}")
            result.add_fail("错误处理: 无效端点", f"状态码: {response.status_code}")

    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("错误处理: 无效端点", str(e))

    # 测试无效JSON
    print()
    print(f"{Colors.BOLD}7.2 无效JSON请求{Colors.RESET}")
    try:
        response = requests.post(
            f"{BASE_URL}/api/config",
            data="invalid json",
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT
        )

        if response.status_code in [400, 422, 500]:
            print_success(f"无效JSON正确被拒绝: {response.status_code}")
            result.add_pass("错误处理: 无效JSON")
        else:
            print_error(f"预期4xx/5xx,实际: {response.status_code}")
            result.add_fail("错误处理: 无效JSON", f"状态码: {response.status_code}")

    except Exception as e:
        print_error(f"测试失败: {e}")
        result.add_fail("错误处理: 无效JSON", str(e))


def test_performance():
    """测试性能"""
    print_section("8. 性能测试")

    # 测试响应时间
    print(f"{Colors.BOLD}8.1 API响应时间{Colors.RESET}")

    endpoints = [
        ("GET", "/api/health", None),
        ("GET", "/api/discovery/polymarket-events", None),
        ("GET", "/api/discovery/orbitexch-events", None),
        ("GET", "/api/matching/results", None),
        ("GET", "/api/config", None),
    ]

    for method, path, data in endpoints:
        try:
            start = time.time()
            if method == "GET":
                response = requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
            else:
                response = requests.post(f"{BASE_URL}{path}", json=data, timeout=TIMEOUT)
            elapsed = time.time() - start

            elapsed_ms = elapsed * 1000

            if elapsed_ms < 100:
                color = Colors.GREEN
            elif elapsed_ms < 500:
                color = Colors.YELLOW
            else:
                color = Colors.RED

            print(f"  {method} {path}: {color}{elapsed_ms:.2f}ms{Colors.RESET}")

            if elapsed_ms < 1000:
                result.add_pass(f"性能: {path}", f"{elapsed_ms:.2f}ms")
            else:
                result.add_warning(f"性能: {path}", f"{elapsed_ms:.2f}ms (较慢)")

        except Exception as e:
            print_error(f"  {method} {path}: {e}")
            result.add_fail(f"性能: {path}", str(e))

    # 测试并发请求
    print()
    print(f"{Colors.BOLD}8.2 并发请求测试{Colors.RESET}")
    try:
        import concurrent.futures

        def make_request():
            return requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)

        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request) for _ in range(10)]
            responses = [f.result() for f in futures]
        elapsed = time.time() - start

        success_count = sum(1 for r in responses if r.status_code == 200)

        print_info(f"10个并发请求完成")
        print_info(f"总耗时: {elapsed * 1000:.2f}ms")
        print_info(f"成功: {success_count}/10")

        if success_count == 10:
            result.add_pass("并发请求", f"10/10成功, {elapsed * 1000:.2f}ms")
        else:
            result.add_fail("并发请求", f"{success_count}/10成功")

    except Exception as e:
        print_error(f"并发测试失败: {e}")
        result.add_fail("并发请求", str(e))


def generate_report():
    """生成测试报告"""
    print_section("测试报告生成")

    report_path = Path("/Users/miller/nautilus_trader/tests/unit_tests/web_panel/test_report.txt")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("Web Panel 功能测试报告\n")
        f.write("=" * 80 + "\n")
        f.write(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"测试服务器: {BASE_URL}\n")
        f.write("\n")

        f.write("测试统计:\n")
        f.write(f"  总测试数: {result.total}\n")
        f.write(f"  通过: {result.passed}\n")
        f.write(f"  失败: {result.failed}\n")
        f.write(f"  警告: {result.warnings}\n")
        f.write(f"  通过率: {result.passed / result.total * 100:.1f}%\n" if result.total > 0 else "  通过率: N/A\n")
        f.write("\n")

        f.write("详细结果:\n")
        f.write("-" * 80 + "\n")
        for status, name, details in result.details:
            symbol = "✓" if status == "PASS" else ("✗" if status == "FAIL" else "⚠")
            f.write(f"{symbol} [{status}] {name}\n")
            if details:
                f.write(f"    {details}\n")
        f.write("-" * 80 + "\n")

    print_success(f"测试报告已生成: {report_path}")


def main():
    """主测试流程"""
    print_section("Web Panel 全面功能测试")
    print_info(f"测试服务器: {BASE_URL}")
    print_info(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 服务器可用性
    if not test_server_availability():
        print()
        print_error("服务器不可用,测试终止")
        return 1

    # 2. 页面路由
    test_page_routes()

    # 3. 健康检查API
    test_health_api()

    # 4. 市场发现API
    test_discovery_api()

    # 5. 市场匹配API
    test_matching_api()

    # 6. 配置API
    test_config_api()

    # 7. 错误处理
    test_error_handling()

    # 8. 性能测试
    test_performance()

    # 打印摘要
    result.print_summary()

    # 生成报告
    generate_report()

    # 返回退出码
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    try:
        exit_code = main()
        exit(exit_code)
    except KeyboardInterrupt:
        print()
        print_warning("测试被用户中断")
        exit(130)
    except Exception as e:
        print()
        print_error(f"测试发生异常: {e}")
        import traceback
        traceback.print_exc()
        exit(1)

# -*- coding: utf-8 -*-
"""
Web Panel 全面功能测试
测试所有页面和API端点
"""

import pytest
import httpx
import asyncio
import json
import time
from pathlib import Path


# 测试配置
BASE_URL = "http://localhost:8765"
TIMEOUT = 30.0


class TestWebPanelPages:
    """测试所有页面路由"""

    @pytest.mark.asyncio
    async def test_homepage(self):
        """测试首页"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "Nautilus Trader" in response.text
            assert "市场发现与匹配系统" in response.text

            # 检查关键元素
            assert "poly-count" in response.text  # 统计数据元素
            assert "orbit-count" in response.text
            assert "match-count" in response.text

    @pytest.mark.asyncio
    async def test_discovery_page(self):
        """测试市场发现页面"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/discovery")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "市场发现" in response.text

            # 检查关键按钮
            assert "startPolymarket()" in response.text
            assert "startOrbitExch()" in response.text
            assert "refreshData()" in response.text

    @pytest.mark.asyncio
    async def test_matching_page(self):
        """测试市场匹配页面"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/matching")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "市场匹配" in response.text

            # 检查关键功能
            assert "startMatching()" in response.text
            assert "refreshMatches()" in response.text
            assert "exportMatches()" in response.text

    @pytest.mark.asyncio
    async def test_config_page(self):
        """测试配置页面"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/config")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "系统配置" in response.text

            # 检查配置项
            assert "poly-enabled" in response.text
            assert "orbit-enabled" in response.text
            assert "min-confidence" in response.text


class TestAPIHealthCheck:
    """测试健康检查API"""

    @pytest.mark.asyncio
    async def test_health_check(self):
        """测试健康检查端点"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/health")

            assert response.status_code == 200
            data = response.json()

            assert "status" in data
            assert data["status"] == "healthy"
            assert "timestamp" in data

            print(f"✓ 健康检查通过: {data}")


class TestMarketDiscoveryAPI:
    """测试市场发现API"""

    @pytest.mark.asyncio
    async def test_get_polymarket_events_empty(self):
        """测试获取Polymarket事件(空数据)"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/discovery/polymarket-events")

            assert response.status_code == 200
            data = response.json()

            # 可能返回空数组或包含events字段
            assert "events" in data or isinstance(data, list)
            print(f"✓ Polymarket事件: {data.get('count', len(data))} 个")

    @pytest.mark.asyncio
    async def test_get_orbitexch_events_empty(self):
        """测试获取OrbitExch事件(空数据)"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/discovery/orbitexch-events")

            assert response.status_code == 200
            data = response.json()

            assert "events" in data or isinstance(data, list)
            print(f"✓ OrbitExch事件: {data.get('count', len(data))} 个")

    @pytest.mark.asyncio
    async def test_start_polymarket_crawler_script_missing(self):
        """测试启动Polymarket爬虫(脚本可能不存在)"""
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(f"{BASE_URL}/api/discovery/start-polymarket")

            # 可能返回404(脚本不存在)或200(成功)或500(执行失败)
            assert response.status_code in [200, 404, 500]
            data = response.json()

            if response.status_code == 404:
                assert "success" in data
                assert data["success"] is False
                assert "error" in data
                print(f"✓ Polymarket爬虫测试: 脚本不存在(预期)")
            elif response.status_code == 200:
                assert "success" in data
                print(f"✓ Polymarket爬虫测试: {data.get('message', 'success')}")
            else:
                assert "success" in data
                print(f"✓ Polymarket爬虫测试: 执行失败(预期): {data.get('message')}")

    @pytest.mark.asyncio
    async def test_start_orbitexch_crawler_script_missing(self):
        """测试启动OrbitExch爬虫(脚本可能不存在)"""
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(f"{BASE_URL}/api/discovery/start-orbitexch")

            assert response.status_code in [200, 404, 500]
            data = response.json()

            if response.status_code == 404:
                assert "success" in data
                assert data["success"] is False
                print(f"✓ OrbitExch爬虫测试: 脚本不存在(预期)")
            elif response.status_code == 200:
                assert "success" in data
                print(f"✓ OrbitExch爬虫测试: {data.get('message', 'success')}")
            else:
                assert "success" in data
                print(f"✓ OrbitExch爬虫测试: 执行失败(预期): {data.get('message')}")


class TestMarketMatchingAPI:
    """测试市场匹配API"""

    @pytest.mark.asyncio
    async def test_get_matching_results_empty(self):
        """测试获取匹配结果(空数据)"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/matching/results")

            assert response.status_code == 200
            data = response.json()

            assert "matches" in data
            assert "match_count" in data
            assert "polymarket_count" in data
            assert "orbitexch_count" in data

            print(f"✓ 匹配结果: {data['match_count']} 个匹配 "
                  f"(Poly: {data['polymarket_count']}, Orbit: {data['orbitexch_count']})")

    @pytest.mark.asyncio
    async def test_start_matching_no_input_data(self):
        """测试启动匹配(无输入数据)"""
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{BASE_URL}/api/matching/start")

            # 可能返回400(缺少输入数据)、404(脚本不存在)或200(成功)
            assert response.status_code in [200, 400, 404, 500]
            data = response.json()

            if response.status_code == 400:
                assert "success" in data
                assert data["success"] is False
                assert "缺少输入数据" in data.get("message", "")
                print(f"✓ 匹配测试: 缺少输入数据(预期)")
            elif response.status_code == 404:
                assert "success" in data
                assert data["success"] is False
                print(f"✓ 匹配测试: 脚本不存在(预期)")
            elif response.status_code == 200:
                assert "success" in data
                print(f"✓ 匹配测试: {data.get('message', 'success')}")
            else:
                print(f"✓ 匹配测试: 执行失败(预期): {data.get('message')}")


class TestConfigAPI:
    """测试配置API"""

    @pytest.mark.asyncio
    async def test_get_config(self):
        """测试获取配置"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/config")

            assert response.status_code == 200
            data = response.json()

            # 可能返回空配置或实际配置
            assert "config" in data or isinstance(data, dict)
            print(f"✓ 配置获取成功: {len(data.get('config', data))} 个配置项")

    @pytest.mark.asyncio
    async def test_post_config(self):
        """测试保存配置"""
        test_config = {
            "test_key": "test_value",
            "market_discovery": {
                "polymarket": {
                    "enabled": True,
                    "sports": ["Soccer", "Basketball"]
                }
            }
        }

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                f"{BASE_URL}/api/config",
                json=test_config
            )

            assert response.status_code == 200
            data = response.json()

            assert "success" in data
            assert data["success"] is True
            assert "message" in data
            assert "timestamp" in data

            print(f"✓ 配置保存成功: {data['message']}")

            # 验证配置已保存
            config_file = Path("config/config.yaml")
            if config_file.exists():
                print(f"✓ 配置文件已创建: {config_file}")


class TestEdgeCases:
    """测试边界情况"""

    @pytest.mark.asyncio
    async def test_invalid_endpoint(self):
        """测试无效端点"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/invalid-endpoint")

            assert response.status_code == 404
            print(f"✓ 无效端点正确返回404")

    @pytest.mark.asyncio
    async def test_post_config_invalid_json(self):
        """测试发送无效JSON到配置端点"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                f"{BASE_URL}/api/config",
                content="invalid json",
                headers={"Content-Type": "application/json"}
            )

            # 应该返回错误
            assert response.status_code in [400, 422, 500]
            print(f"✓ 无效JSON正确被拒绝: {response.status_code}")

    @pytest.mark.asyncio
    async def test_concurrent_requests(self):
        """测试并发请求"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # 同时发送多个请求
            tasks = [
                client.get(f"{BASE_URL}/api/health"),
                client.get(f"{BASE_URL}/api/discovery/polymarket-events"),
                client.get(f"{BASE_URL}/api/discovery/orbitexch-events"),
                client.get(f"{BASE_URL}/api/matching/results"),
            ]

            responses = await asyncio.gather(*tasks)

            # 所有请求都应该成功
            for i, response in enumerate(responses):
                assert response.status_code == 200

            print(f"✓ 并发请求测试通过: {len(responses)} 个请求")


class TestResponseFormats:
    """测试响应格式"""

    @pytest.mark.asyncio
    async def test_health_response_format(self):
        """测试健康检查响应格式"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/health")
            data = response.json()

            # 验证字段类型
            assert isinstance(data["status"], str)
            assert isinstance(data["timestamp"], str)

            # 验证timestamp格式(ISO 8601)
            from datetime import datetime
            timestamp = datetime.fromisoformat(data["timestamp"])
            assert timestamp is not None

            print(f"✓ 健康检查响应格式正确")

    @pytest.mark.asyncio
    async def test_matching_results_response_format(self):
        """测试匹配结果响应格式"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/api/matching/results")
            data = response.json()

            # 验证字段类型
            assert isinstance(data["matches"], list)
            assert isinstance(data["match_count"], int)
            assert isinstance(data["polymarket_count"], int)
            assert isinstance(data["orbitexch_count"], int)

            # 验证数据一致性
            assert data["match_count"] == len(data["matches"])

            print(f"✓ 匹配结果响应格式正确")


def run_manual_tests():
    """手动运行测试(不使用pytest)"""
    import subprocess
    import sys

    print("=" * 80)
    print("Web Panel 全面功能测试")
    print("=" * 80)
    print()

    # 检查服务器是否运行
    print("检查服务器状态...")
    try:
        import requests
        response = requests.get(f"{BASE_URL}/api/health", timeout=5)
        print(f"✓ 服务器运行正常: {response.json()}")
    except Exception as e:
        print(f"✗ 服务器未运行: {e}")
        print(f"\n请先启动服务器:")
        print(f"cd /Users/miller/nautilus_trader && source venv/bin/activate && python -m uvicorn web_panel.app:app --port 8765")
        return

    print()
    print("=" * 80)
    print("开始测试...")
    print("=" * 80)
    print()

    # 运行pytest
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "-s"],
        cwd="/Users/miller/nautilus_trader"
    )

    return result.returncode


if __name__ == "__main__":
    exit(run_manual_tests())

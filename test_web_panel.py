#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Web Panel 功能测试脚本
测试所有 API 端点和页面功能
"""

import requests
import json
from typing import Dict, List, Tuple
from datetime import datetime


BASE_URL = "http://localhost:8765"


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: List[Tuple[str, str]] = []

    def success(self, test_name: str):
        self.passed += 1
        print(f"✅ {test_name}")

    def fail(self, test_name: str, error: str):
        self.failed += 1
        self.errors.append((test_name, error))
        print(f"❌ {test_name}: {error}")

    def summary(self):
        total = self.passed + self.failed
        print("\n" + "=" * 60)
        print("测试总结")
        print("=" * 60)
        print(f"总计: {total} 个测试")
        print(f"通过: {self.passed} ({self.passed/total*100:.1f}%)")
        print(f"失败: {self.failed} ({self.failed/total*100:.1f}%)")

        if self.errors:
            print("\n失败的测试:")
            for test_name, error in self.errors:
                print(f"  - {test_name}: {error}")

        return self.failed == 0


def test_health_check(result: TestResult):
    """测试健康检查 API"""
    try:
        response = requests.get(f"{BASE_URL}/api/health")
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "healthy":
                result.success("Health Check API")
            else:
                result.fail("Health Check API", "状态不是 healthy")
        else:
            result.fail("Health Check API", f"HTTP {response.status_code}")
    except Exception as e:
        result.fail("Health Check API", str(e))


def main():
    """运行所有测试"""
    print("=" * 60)
    print("Web Panel 功能测试")
    print("=" * 60)
    print(f"测试服务器: {BASE_URL}")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    result = TestResult()
    test_health_check(result)
    result.summary()

    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    exit(main())

# 测试策略文档

## 文档信息

| 属性 | 值 |
|------|-----|
| 版本 | 1.0 |
| 创建日期 | 2026-01-16 |
| 最后更新 | 2026-01-16 |
| 状态 | 草稿 |

---

## 概述

本文档定义套利系统的测试策略，包括测试类型、覆盖目标、验收标准和执行流程。

---

## 测试分层

```
┌─────────────────────────────────────────────────────────┐
│                    E2E 测试 (端到端)                     │
│              完整业务流程验证                            │
├─────────────────────────────────────────────────────────┤
│                   集成测试                               │
│           服务间交互、API 契约验证                       │
├─────────────────────────────────────────────────────────┤
│                   单元测试                               │
│         函数/类级别、业务逻辑验证                        │
└─────────────────────────────────────────────────────────┘
```

### 覆盖目标

| 测试层级 | 覆盖目标 | 执行频率 |
|----------|----------|----------|
| 单元测试 | ≥80% | 每次提交 |
| 集成测试 | 核心路径 | 每次 PR |
| E2E 测试 | 主要场景 | 每日/发布前 |

---

## 测试目录结构

```
tests/
├── unit_tests/
│   ├── services/
│   │   ├── market_discovery/
│   │   │   ├── test_polymarket.py
│   │   │   └── test_orbitexch.py
│   │   └── market_matching/
│   │       ├── test_matching_service.py
│   │       └── test_matchers.py
│   └── web_panel/
│       ├── test_app.py
│       └── test_api.py
├── integration_tests/
│   ├── test_discovery_to_matching.py
│   └── test_api_endpoints.py
└── e2e_tests/
    └── test_full_workflow.py
```

---

## 模块测试要求

### Market Discovery（市场发现）

#### 单元测试

| 测试ID | 测试内容 | 验收条件 | 执行结果 |
|--------|----------|----------|----------|
| MD-U01 | API 响应解析 | 正确解析所有字段 | 待执行 |
| MD-U02 | 数据标准化 | 输出符合 schema | 待执行 |
| MD-U03 | 异常处理 | API 错误正确处理 | 待执行 |
| MD-U04 | 重试逻辑 | 临时错误自动重试 | 待执行 |

#### 集成测试

| 测试ID | 测试内容 | 验收条件 | 执行结果 |
|--------|----------|----------|----------|
| MD-I01 | 端到端爬取 | 成功获取数据并保存 | 待执行 |
| MD-I02 | 多平台并发 | 并发爬取无冲突 | 待执行 |

### Market Matching（市场匹配）

#### 单元测试

| 测试ID | 测试内容 | 验收条件 | 执行结果 |
|--------|----------|----------|----------|
| MM-U01 | 文本相似度计算 | 相似度准确率≥90% | 待执行 |
| MM-U02 | 匹配置信度 | 置信度计算正确 | 待执行 |
| MM-U03 | 边界条件 | 空数据/单条数据处理 | 待执行 |

#### 集成测试

| 测试ID | 测试内容 | 验收条件 | 执行结果 |
|--------|----------|----------|----------|
| MM-I01 | 跨平台匹配 | 正确识别相同市场 | 待执行 |
| MM-I02 | 匹配结果持久化 | 结果正确保存 | 待执行 |

### Web Panel（Web 面板）

#### 单元测试

| 测试ID | 测试内容 | 验收条件 | 执行结果 |
|--------|----------|----------|----------|
| WP-U01 | 路由注册 | 所有路由可访问 | 待执行 |
| WP-U02 | 模板渲染 | 页面正确渲染 | 待执行 |
| WP-U03 | 配置 CRUD | 配置读写正确 | 待执行 |

#### 集成测试

| 测试ID | 测试内容 | 验收条件 | 执行结果 |
|--------|----------|----------|----------|
| WP-I01 | API 端点 | 响应格式正确 | 待执行 |
| WP-I02 | 服务调用 | 正确触发后端服务 | 待执行 |

---

## 测试执行命令

```bash
# 运行所有测试
pytest tests/ -v

# 运行单元测试
pytest tests/unit_tests/ -v

# 运行集成测试
pytest tests/integration_tests/ -v

# 运行特定模块测试
pytest tests/unit_tests/services/market_discovery/ -v

# 带覆盖率报告
pytest tests/ --cov=services --cov-report=html

# 仅运行标记的测试
pytest -m "not slow" tests/
```

---

## Mock 策略

### 外部 API Mock

```python
# 使用 responses 库 mock HTTP 请求
import responses

@responses.activate
def test_polymarket_api():
    responses.add(
        responses.GET,
        "https://gamma-api.polymarket.com/events",
        json={"events": []},
        status=200
    )
    # 测试代码...
```

### 文件系统 Mock

```python
# 使用 tmp_path fixture
def test_save_events(tmp_path):
    output_file = tmp_path / "events.json"
    # 测试代码...
```

---

## 测试数据管理

### Fixtures 目录

```
tests/fixtures/
├── polymarket/
│   ├── sample_events.json
│   └── api_response.json
├── orbitexch/
│   ├── sample_events.json
│   └── api_response.json
└── matches/
    └── expected_matches.json
```

### 测试数据原则

1. **隔离性**: 每个测试使用独立数据
2. **可重复性**: 数据固定，结果可预测
3. **真实性**: 数据结构与生产一致
4. **最小化**: 只包含必要字段

---

## 测试报告模板

### 测试执行报告

```markdown
# 测试执行报告

## 执行信息
- 执行日期：YYYY-MM-DD HH:mm
- 执行环境：[本地/CI]
- 执行者：[AI/人工]

## 执行结果汇总

| 测试类型 | 总数 | 通过 | 失败 | 跳过 | 覆盖率 |
|----------|------|------|------|------|--------|
| 单元测试 |      |      |      |      |        |
| 集成测试 |      |      |      |      |        |
| E2E测试  |      |      |      |      |        |

## 失败用例详情

### [测试ID]: [测试名称]
- **失败原因**：
- **错误信息**：
- **复现步骤**：
- **建议修复**：

## 风险评估

- [ ] 所有关键路径测试通过
- [ ] 覆盖率达标
- [ ] 无阻塞性问题
```

---

## 持续集成配置

### pytest.ini

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = -v --tb=short
markers =
    slow: marks tests as slow
    integration: marks tests as integration tests
    e2e: marks tests as end-to-end tests
```

### 测试前检查清单

- [ ] 所有依赖已安装
- [ ] 测试数据已准备
- [ ] Mock 配置正确
- [ ] 环境变量已设置

---

## 相关文档

- [系统架构](architecture.md)
- [代码审查规范](code-review.md)
- [需求说明](requirements.md)

# Web Panel 测试套件

本目录包含 Web Panel 的全面功能测试。

## 测试文件

### 1. `manual_api_test.py` - 手动API测试工具
完整的API测试套件,包含所有端点的测试和详细的测试报告生成。

**功能**:
- 服务器可用性测试
- 页面路由测试
- 所有API端点测试
- 错误处理测试
- 性能测试
- 并发测试
- 自动生成测试报告

**使用方法**:
```bash
# 1. 启动web_panel服务器
cd /Users/miller/nautilus_trader
source venv/bin/activate
python -m uvicorn web_panel.app:app --port 8765

# 2. 在新终端中运行测试
source venv/bin/activate
python tests/unit_tests/web_panel/manual_api_test.py
```

### 2. `test_web_panel_full.py` - Pytest测试套件
基于pytest的异步测试套件,可与CI/CD集成。

**使用方法**:
```bash
# 确保服务器正在运行
pytest tests/unit_tests/web_panel/test_web_panel_full.py -v -s
```

### 3. `WEB_PANEL_TEST_REPORT.md` - 测试报告
详细的测试报告,包含:
- 测试统计
- 所有端点的测试结果
- 按钮功能验证
- 性能分析
- 发现的问题
- 改进建议

## 快速开始

### 一键测试流程

```bash
#!/bin/bash

# 进入项目目录
cd /Users/miller/nautilus_trader

# 激活虚拟环境
source venv/bin/activate

# 启动服务器(后台运行)
python -m uvicorn web_panel.app:app --port 8765 &
SERVER_PID=$!

# 等待服务器启动
sleep 3

# 运行测试
python tests/unit_tests/web_panel/manual_api_test.py

# 关闭服务器
kill $SERVER_PID

echo "测试完成!查看报告: tests/unit_tests/web_panel/test_report.txt"
```

## 测试覆盖

### 页面测试 (4/4)
- ✓ 首页 (/)
- ✓ 市场发现 (/discovery)
- ✓ 市场匹配 (/matching)
- ✓ 配置管理 (/config)

### API端点测试 (9/9)
- ✓ GET /api/health - 健康检查
- ✓ GET /api/discovery/polymarket-events - 获取Polymarket事件
- ✓ GET /api/discovery/orbitexch-events - 获取OrbitExch事件
- ✓ POST /api/discovery/start-polymarket - 启动Polymarket爬虫
- ✓ POST /api/discovery/start-orbitexch - 启动OrbitExch爬虫
- ✓ POST /api/matching/start - 启动市场匹配
- ✓ GET /api/matching/results - 获取匹配结果
- ✓ GET /api/config - 获取配置
- ✓ POST /api/config - 保存配置

### 按钮功能测试
#### 首页
- ✓ 导航链接 (4个)
- ✓ 功能卡片点击 (3个)
- ✓ 快速操作按钮 (4个)

#### 市场发现页面
- ✓ 启动Polymarket爬虫按钮
- ✓ 启动OrbitExch爬虫按钮
- ✓ 启动所有爬虫按钮
- ✓ 刷新数据按钮

#### 市场匹配页面
- ✓ 开始匹配按钮
- ✓ 刷新结果按钮
- ✓ 导出匹配结果按钮

#### 配置页面
- ✓ 保存Polymarket配置按钮
- ✓ 保存OrbitExch配置按钮
- ✓ 保存匹配配置按钮
- ✓ 保存所有配置按钮
- ✓ 重新加载配置按钮

### 特殊测试
- ✓ 错误处理 (无效端点, 无效JSON)
- ✓ 性能测试 (响应时间 < 5ms)
- ✓ 并发测试 (10个并发请求)

## 测试结果

最新测试结果:
- **总测试数**: 22
- **通过**: 19
- **失败**: 0
- **警告**: 3 (需要手动测试的长时间运行任务)
- **通过率**: 86.4%

详细报告: [WEB_PANEL_TEST_REPORT.md](./WEB_PANEL_TEST_REPORT.md)

## 手动测试命令

### 测试健康检查
```bash
curl http://localhost:8765/api/health
```

### 测试获取事件
```bash
# Polymarket事件
curl http://localhost:8765/api/discovery/polymarket-events | python3 -m json.tool

# OrbitExch事件
curl http://localhost:8765/api/discovery/orbitexch-events | python3 -m json.tool
```

### 测试启动爬虫
```bash
# Polymarket爬虫
curl -X POST http://localhost:8765/api/discovery/start-polymarket

# OrbitExch爬虫
curl -X POST http://localhost:8765/api/discovery/start-orbitexch
```

### 测试匹配
```bash
# 启动匹配
curl -X POST http://localhost:8765/api/matching/start

# 获取结果
curl http://localhost:8765/api/matching/results | python3 -m json.tool
```

### 测试配置
```bash
# 获取配置
curl http://localhost:8765/api/config | python3 -m json.tool

# 保存配置
curl -X POST http://localhost:8765/api/config \
  -H "Content-Type: application/json" \
  -d '{"test": "value"}'
```

## 依赖

测试需要以下Python包:
```
requests
httpx
pytest
pytest-asyncio
```

安装:
```bash
pip install requests httpx pytest pytest-asyncio
```

## 常见问题

### Q: 服务器启动失败
A: 检查端口8765是否被占用:
```bash
lsof -i :8765
```

### Q: 测试超时
A: 增加TIMEOUT设置或跳过长时间运行的测试

### Q: 找不到配置文件
A: 某些测试会创建 config/config.yaml,这是正常行为

## 贡献

添加新测试时,请遵循:
1. 在 `test_web_panel_full.py` 中添加pytest测试
2. 在 `manual_api_test.py` 中添加手动测试
3. 更新测试报告和统计数据

## 许可

与主项目相同

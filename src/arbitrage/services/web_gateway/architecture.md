# Web Gateway 服务 - 架构设计

## 概述

Web Gateway 提供 Web 界面用于查看和管理套利系统的配置和数据。

## 模块结构

```
web_gateway/
├── __init__.py           # 模块导出
├── app.py                # FastAPI 应用入口
├── config.py             # 配置
├── state.py              # 状态管理（单例）
├── routes/               # API 路由
│   ├── __init__.py
│   ├── discovery.py      # 市场发现 API
│   ├── matching.py       # 市场匹配 API
│   └── config.py         # 配置管理 API
├── templates/
│   └── index.html        # 前端页面
└── static/               # 静态资源
```

## API 端点

### Discovery API (`/api/discovery`)

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | /config | 获取市场发现配置 |
| PUT | /config/venues/{venue}/sports | 更新平台 sports 配置 |
| GET | /results | 获取发现结果 |
| GET | /status | 获取任务状态 |
| POST | /run | 触发市场发现 |
| POST | /stop | 停止市场发现 |

### Matching API (`/api/matching`)

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | /config | 获取市场匹配配置 |
| PUT | /config | 更新市场匹配配置 |
| POST | /config/sport-aliases | 添加 sport 别名 |
| POST | /config/competition-aliases | 添加 competition 别名 |
| GET | /results | 获取匹配结果 |
| GET | /status | 获取任务状态 |
| POST | /run | 触发市场匹配 |

### Config API (`/api/config`)

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | / | 获取所有配置 |
| GET | /discovery | 获取发现配置 |
| PUT | /discovery | 更新发现配置 |
| GET | /matching | 获取匹配配置 |
| PUT | /matching | 更新匹配配置 |

## 状态管理

使用单例模式的 `AppState` 管理：
- 配置数据（自动保存到 `default_config.json`）
- 运行时数据（发现结果、匹配结果）

## 前端界面

三个标签页：
1. **Market Discovery** - 查看/触发市场发现
2. **Market Matching** - 查看/触发市场匹配
3. **Configuration** - 编辑配置

## 启动方式

```bash
# 方式1：直接运行
python -m src.arbitrage.services.web_gateway.app --host 127.0.0.1 --port 8080

# 方式2：使用脚本
python scripts/run_web_gateway.py
```

访问 http://127.0.0.1:8080 查看界面。

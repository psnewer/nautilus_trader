# Nautilus Trader Web Panel

一个基于 FastAPI 的 Web 管理面板，用于控制和监控套利交易系统。

## 快速开始

```bash
# 进入项目目录
cd /Users/miller/nautilus_trader

# 启动服务器
uvicorn web_panel.app:app --reload --port 8000

# 在浏览器中访问
# http://localhost:8000
```

## 功能特性

- 📊 **系统仪表板** - 查看系统状态和概览
- ⚙️ **配置管理** - 通过 Web 界面管理所有配置
- 🔍 **市场发现** - 控制 Polymarket 和 OrbitExch 爬虫
- 🎯 **市场匹配** - 运行和查看市场匹配结果

## 文档

- 📖 [使用指南](USAGE.md) - 详细的使用说明和配置教程
- 🔧 [修复报告](FIX_REPORT.md) - 最近的问题修复和技术细节
- 📋 [开发指南](CLAUDE.md) - 开发者文档和 API 参考

## 测试

运行自动化测试:

```bash
python test_web_panel.py
```

## 目录结构

```
web_panel/
├── app.py                  # 主应用程序
├── templates/              # HTML 模板
│   ├── index.html         # 首页
│   ├── config.html        # 配置页面
│   ├── discovery.html     # 市场发现页面
│   └── matching.html      # 市场匹配页面
├── static/                 # 静态资源
├── README.md              # 本文件
├── USAGE.md               # 使用指南
├── FIX_REPORT.md          # 修复报告
└── CLAUDE.md              # 开发文档
```

## 主要功能

### 1. 配置管理
- 实时修改系统配置
- 支持 Polymarket 和 OrbitExch 爬虫配置
- 市场匹配参数调整
- 配置持久化到 YAML 文件

### 2. 市场发现
- 一键启动 Polymarket 爬虫
- 一键启动 OrbitExch 爬虫
- 实时查看爬取的事件数据
- 爬取进度和状态监控

### 3. 市场匹配
- 运行市场匹配算法
- 查看详细匹配结果
- 匹配统计和分析
- 置信度和匹配质量展示

## API 端点

### 健康检查
```
GET /api/health
```

### 配置管理
```
GET  /api/config                # 获取配置
POST /api/config                # 更新整个配置
POST /api/config/update         # 更新单个配置项
POST /api/config/reload         # 重新加载配置
```

### 市场发现
```
POST /api/discovery/start-polymarket    # 启动 Polymarket 爬虫
POST /api/discovery/start-orbitexch     # 启动 OrbitExch 爬虫
GET  /api/discovery/polymarket-events   # 获取 Polymarket 事件
GET  /api/discovery/orbitexch-events    # 获取 OrbitExch 事件
```

### 市场匹配
```
POST /api/matching/start        # 启动市场匹配
GET  /api/matching/results      # 获取匹配结果
```

## 技术栈

- **后端**: FastAPI
- **模板引擎**: Jinja2
- **服务器**: Uvicorn
- **配置**: PyYAML
- **前端**: 原生 HTML/CSS/JavaScript

## 最近更新

### 2026-01-10 - v1.1
- ✅ 修复配置保存按钮错误
- ✅ 新增单个配置项更新 API (`/api/config/update`)
- ✅ 新增配置重载 API (`/api/config/reload`)
- ✅ 改进配置获取逻辑（支持默认配置回退）
- ✅ 添加完整的自动化测试套件
- ✅ 完善文档

## 常见问题

### 服务器启动失败？

确保安装了所有依赖:
```bash
pip install fastapi uvicorn jinja2 pyyaml
```

### 配置保存不生效？

检查配置文件权限:
```bash
ls -la config/config.yaml
```

### 端口被占用？

更换端口:
```bash
uvicorn web_panel.app:app --port 8001
```

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可

本项目是 Nautilus Trader 的一部分。

# Web Panel 使用指南

## 快速启动

### 1. 启动服务器

```bash
# 进入项目目录
cd /Users/miller/nautilus_trader

# 启动 Web Panel（开发模式，支持热重载）
uvicorn web_panel.app:app --reload --port 8000

# 或者生产模式
uvicorn web_panel.app:app --host 0.0.0.0 --port 8000
```

### 2. 访问界面

在浏览器中打开: http://localhost:8000

## 功能页面

### 首页 (/)
- 系统状态概览
- 快速访问各功能模块

### 配置管理 (/config)
- 配置市场发现爬虫
- 配置市场匹配参数
- 实时保存配置到文件

### 市场发现 (/discovery)
- 启动 Polymarket 爬虫
- 启动 OrbitExch 爬虫
- 查看爬取的事件数据

### 市场匹配 (/matching)
- 运行市场匹配算法
- 查看匹配结果
- 分析匹配统计数据

## 配置页面使用说明

### Polymarket 爬虫配置

**启用/禁用**:
- 勾选"启用 Polymarket 爬虫"复选框

**运动项目**:
- 输入要爬取的运动项目，用逗号分隔
- 例如: `Soccer, Basketball, Tennis`

**限制选项**:
- **每个运动最大事件数**: 每个运动类别最多爬取的事件数（0 = 不限制）
- **每个赛事最大事件数**: 每个具体赛事最多爬取的事件数
- **总共最大事件数**: 所有事件的总数限制

**保存配置**:
- 点击"保存 Polymarket 配置"按钮
- 成功后会显示绿色提示："Polymarket 配置已保存"

### OrbitExch 爬虫配置

**启用/禁用**:
- 勾选"启用 OrbitExch 爬虫"复选框

**运动项目**:
- 输入要爬取的运动项目，用逗号分隔
- 例如: `American Football, Soccer`

**限制选项**:
- **每个运动最多爬取几个赛事**: 每个运动类别最多爬取的赛事数量

**浏览器选项**:
- **无头模式**: 勾选后浏览器在后台运行，不显示窗口
  - 开发调试时建议关闭（可以看到浏览器操作）
  - 生产环境建议开启（节省资源）

**保存配置**:
- 点击"保存 OrbitExch 配置"按钮
- 成功后会显示绿色提示："OrbitExch 配置已保存"

### 市场匹配配置

**预处理器设置**:
- **启用预处理**: 是否在匹配前预处理数据
- **标准化运动名称**: 将不同写法的运动名称统一（如 Football → Soccer）
- **标准化赛事名称**: 将不同写法的赛事名称统一（如 EPL → English Premier League）
- **标准化队名**: 将不同写法的队名统一（如 Man Utd → Manchester United）

**匹配器设置**:
- **最低匹配置信度**: 设置 0.0 到 1.0 之间的值
  - 0.6-0.7: 标准匹配（推荐）
  - 0.8-0.9: 严格匹配（可能漏掉一些有效匹配）
  - 0.4-0.5: 宽松匹配（可能产生错误匹配）

- **允许主客队互换**: 是否允许主客队位置互换的匹配

**保存配置**:
- 点击"保存匹配配置"按钮
- 成功后会显示绿色提示："匹配配置已保存"

### 全局操作

**保存所有配置**:
- 一次性保存上述所有配置
- 点击绿色的"保存所有配置"按钮

**重新加载配置**:
- 从配置文件重新加载配置到界面
- 点击橙色的"重新加载配置"按钮
- 用于放弃当前修改，恢复到文件中的配置

## 配置文件位置

所有配置保存在:
```
/Users/miller/nautilus_trader/config/config.yaml
```

你也可以直接编辑这个文件，然后在 Web 界面点击"重新加载配置"。

## API 端点

### 配置相关

**获取当前配置**:
```bash
GET /api/config
```

**更新整个配置**:
```bash
POST /api/config
Content-Type: application/json

{配置对象}
```

**更新单个配置项**:
```bash
POST /api/config/update
Content-Type: application/json

{
  "path": "market_discovery.orbitexch.enabled",
  "value": true
}
```

**重新加载配置**:
```bash
POST /api/config/reload
```

### 市场发现

**启动 Polymarket 爬虫**:
```bash
POST /api/discovery/start-polymarket
```

**启动 OrbitExch 爬虫**:
```bash
POST /api/discovery/start-orbitexch
```

**获取 Polymarket 事件**:
```bash
GET /api/discovery/polymarket-events
```

**获取 OrbitExch 事件**:
```bash
GET /api/discovery/orbitexch-events
```

### 市场匹配

**启动市场匹配**:
```bash
POST /api/matching/start
```

**获取匹配结果**:
```bash
GET /api/matching/results
```

## 常见问题

### Q: 点击保存按钮没有反应？

**A**: 检查以下几点:
1. 服务器是否正常运行（查看终端输出）
2. 浏览器控制台是否有错误（按 F12 打开开发者工具）
3. 配置文件目录是否有写入权限

### Q: 配置保存后没有生效？

**A**:
1. 检查 `config/config.yaml` 文件是否已更新
2. 如果运行了爬虫或匹配器，需要重新启动它们才能使用新配置
3. 某些配置可能需要重启 Web Panel 服务器

### Q: 如何恢复默认配置？

**A**:
```bash
# 备份当前配置
cp config/config.yaml config/config.yaml.backup

# 恢复默认配置
cp config/defaults.yaml config/config.yaml

# 在 Web 界面点击"重新加载配置"
```

### Q: 如何查看详细的错误信息？

**A**:
1. 查看终端中的服务器日志
2. 打开浏览器开发者工具（F12）查看 Console 和 Network 标签
3. 检查 `config/config.yaml` 文件格式是否正确（YAML 格式）

## 测试配置

运行自动化测试:
```bash
python test_web_panel.py
```

这将测试所有 API 端点是否正常工作。

## 开发建议

### 调试模式

启动时使用 `--reload` 参数:
```bash
uvicorn web_panel.app:app --reload --port 8000
```

这样修改代码后服务器会自动重启。

### 日志级别

在 `config/config.yaml` 中设置:
```yaml
log_level: DEBUG  # INFO, WARNING, ERROR
```

### 端口占用

如果 8000 端口被占用，可以换一个端口:
```bash
uvicorn web_panel.app:app --port 8001
```

## 安全提示

1. **生产环境**: 不要使用 `--reload` 模式
2. **访问控制**: 建议添加认证机制（当前版本没有认证）
3. **HTTPS**: 生产环境建议使用 HTTPS
4. **防火墙**: 配置防火墙规则限制访问

## 性能优化

### 爬虫性能

- 调整 `max_events_per_sport` 限制爬取数量
- 启用 OrbitExch 的 `headless` 模式减少资源占用
- 合理设置爬取间隔避免被封禁

### 匹配性能

- 调整 `min_confidence` 参数平衡准确性和召回率
- 根据需要开启/关闭各种标准化选项
- 大数据量时考虑批量处理

## 更新日志

### v1.1 (2026-01-10)
- ✨ 新增单个配置项更新 API
- ✨ 新增配置重载功能
- 🐛 修复保存配置按钮错误
- 📝 完善文档和测试

### v1.0 (2026-01-09)
- 🎉 初始版本
- ✨ 基础配置管理
- ✨ 市场发现功能
- ✨ 市场匹配功能

## 技术支持

如遇到问题，请检查:
1. 本文档的常见问题部分
2. `/Users/miller/nautilus_trader/web_panel/FIX_REPORT.md` 修复报告
3. 服务器日志输出
4. 浏览器开发者工具的 Console 和 Network 标签

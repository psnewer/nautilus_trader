# Windows 部署指南

## 快速部署（3 步）

### 第一步：运行部署脚本

在您的 Nautilus Trader 项目根目录下，双击运行：

```
deploy_config_system.bat
```

或者在命令行中：

```cmd
cd your_nautilus_project
deploy_config_system.bat
```

这个脚本会自动创建所有必要的目录和文件。

---

### 第二步：复制配置文件

将我提供的文件复制到对应位置：

```cmd
REM 在项目根目录执行

copy config_schemas.py config\schemas.py
copy config.yaml config\defaults.yaml
copy event_preprocessor.py services\market_matching\event_preprocessor.py
```

或者手动复制：
- `config_schemas.py` → `config\schemas.py`
- `config.yaml` → `config\defaults.yaml`
- `event_preprocessor.py` → `services\market_matching\event_preprocessor.py`

---

### 第三步：启动 Web 面板

```cmd
python run_web_panel.py
```

然后在浏览器中打开：
```
http://localhost:8000/config
```

---

## 部署后的目录结构

```
your_nautilus_project\
├── config\
│   ├── __init__.py              ✓ 自动创建
│   ├── manager.py               ✓ 自动创建
│   ├── schemas.py               ← 需要复制
│   └── defaults.yaml            ← 需要复制
│
├── services\
│   └── market_matching\
│       ├── __init__.py          ✓ 自动创建
│       └── event_preprocessor.py ← 需要复制
│
├── web_panel\
│   ├── __init__.py              ✓ 自动创建
│   ├── app.py                   ✓ 自动创建
│   └── templates\
│       ├── base.html            ✓ 自动创建
│       ├── index.html           ✓ 自动创建
│       └── config.html          ✓ 自动创建
│
└── run_web_panel.py             ✓ 自动创建
```

---

## 验证部署

### 1. 测试配置加载

打开 Python：

```python
from config.manager import config_manager

# 查看配置
config = config_manager.get_config()
print(config.market_discovery.polymarket.sports)
```

### 2. 测试 Web 面板

启动面板：
```cmd
python run_web_panel.py
```

访问：
```
http://localhost:8000
http://localhost:8000/config
```

### 3. 测试 API

在另一个命令行窗口：

```cmd
REM 获取配置
curl http://localhost:8000/api/config

REM 或使用 PowerShell
Invoke-WebRequest -Uri http://localhost:8000/api/config
```

---

## 常见问题

### Q: 找不到 curl 命令？

**方案 1: 使用 PowerShell**
```powershell
Invoke-RestMethod -Uri http://localhost:8000/api/config
```

**方案 2: 直接浏览器访问**
```
http://localhost:8000/api/config
```

### Q: Python 找不到模块？

确保在项目根目录运行：
```cmd
cd C:\path\to\your_nautilus_project
python run_web_panel.py
```

### Q: 端口 8000 被占用？

修改 `run_web_panel.py` 中的端口：
```python
uvicorn.run(
    "web_panel.app:app",
    host="0.0.0.0",
    port=8080,  # 改为其他端口
    reload=True
)
```

### Q: 如何卸载？

删除这些目录和文件：
```cmd
rmdir /s /q config
rmdir /s /q web_panel
rmdir /s /q actors
del run_web_panel.py
```

---

## 使用配置系统

### 方法 1: 通过代码

```python
from config.manager import config_manager

# 读取配置
sports = config_manager.market_discovery.polymarket.sports
print(sports)  # ['Soccer', 'Basketball']

# 更新配置
config_manager.update_config(
    "market_discovery.polymarket.sports",
    ["Soccer", "Basketball", "Tennis"]
)

# 保存配置
config_manager.save_config()
```

### 方法 2: 通过 Web 面板

1. 启动面板: `python run_web_panel.py`
2. 访问: http://localhost:8000/config
3. 修改配置
4. 点击保存

### 方法 3: 直接编辑文件

```cmd
notepad config\config.yaml
```

---

## 下一步

- [ ] 部署爬虫服务
- [ ] 部署匹配服务  
- [ ] 集成到 Nautilus Trader
- [ ] 设置定时任务
- [ ] 添加监控和日志

---

## 获取帮助

如果遇到问题：

1. 查看日志输出
2. 检查文件是否正确复制
3. 确认 Python 环境正确
4. 查看详细部署指南: CONFIG_DEPLOYMENT_GUIDE.md

# 完整部署操作指南

## 前提：下载所需文件

首先，您需要从我这里获取以下文件并保存到您的项目根目录：

### 必需文件清单

1. **deploy.bat** - 部署脚本
2. **config_schemas.py** - 配置数据模型
3. **config.yaml** - 配置文件
4. **event_preprocessor.py** - 预处理器

---

## 第一步：下载文件到项目根目录

### 方法 1：从聊天界面下载

1. 我已经生成了这些文件，它们会显示在聊天界面中
2. 点击每个文件的下载按钮
3. 保存到您的 Nautilus Trader 项目根目录
   ```
   C:\Users\Administrator\nautilus_trader\
   ```

### 方法 2：手动创建文件

如果下载不便，在 VSCode 中手动创建：

**创建 config_schemas.py:**
1. 在项目根目录右键 → 新建文件
2. 命名为 `config_schemas.py`
3. 复制我提供的 `config_schemas.py` 内容
4. 粘贴并保存

**创建 config.yaml:**
1. 新建文件 `config.yaml`
2. 复制我提供的 `config.yaml` 内容
3. 粘贴并保存

**创建 event_preprocessor.py:**
1. 新建文件 `event_preprocessor.py`
2. 复制我提供的 `event_preprocessor.py` 内容
3. 粘贴并保存

**创建 deploy.bat:**
1. 新建文件 `deploy.bat`
2. 复制我提供的 `deploy.bat` 内容
3. 粘贴并保存

---

## 第二步：验证文件位置

在 VSCode 终端中检查文件是否存在：

```powershell
# 列出项目根目录的文件
ls

# 或者用这个命令
dir
```

您应该看到：
```
config_schemas.py
config.yaml
event_preprocessor.py
deploy.bat
```

---

## 第三步：运行部署脚本

在 VSCode 终端中：

```cmd
deploy.bat
```

---

## 第四步：复制文件到指定位置

部署脚本运行完成后，执行：

```cmd
copy config_schemas.py config\schemas.py
copy config.yaml config\defaults.yaml
copy event_preprocessor.py services\market_matching\
```

---

## 第五步：验证部署

检查文件是否正确复制：

```powershell
# 检查 config 目录
ls config\

# 应该看到：
# __init__.py
# manager.py
# schemas.py
# defaults.yaml

# 检查 services 目录
ls services\market_matching\

# 应该看到：
# __init__.py
# event_preprocessor.py
```

---

## 第六步：启动 Web 面板

```cmd
python run_web_panel.py
```

您应该看到：
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## 第七步：访问 Web 面板

打开浏览器访问：
```
http://localhost:8000/config
```

---

## 完整的文件结构（部署后）

```
C:\Users\Administrator\nautilus_trader\
│
├── config_schemas.py         ← 您下载的文件（原始）
├── config.yaml               ← 您下载的文件（原始）
├── event_preprocessor.py     ← 您下载的文件（原始）
├── deploy.bat                ← 您下载的文件（部署脚本）
├── run_web_panel.py          ← deploy.bat 自动生成
│
├── config\                   ← deploy.bat 自动创建
│   ├── __init__.py           ← deploy.bat 自动创建
│   ├── manager.py            ← deploy.bat 自动创建
│   ├── schemas.py            ← 从 config_schemas.py 复制
│   └── defaults.yaml         ← 从 config.yaml 复制
│
├── services\
│   └── market_matching\
│       ├── __init__.py       ← deploy.bat 自动创建
│       └── event_preprocessor.py ← 复制过来的
│
└── web_panel\                ← deploy.bat 自动创建
    ├── __init__.py
    ├── app.py
    └── templates\
        ├── index.html
        └── config.html
```

---

## 常见问题

### Q1: "找不到路径" 错误？

**原因：** 文件不在项目根目录

**解决：**
```powershell
# 检查当前目录
pwd

# 输出应该是：
# Path: C:\Users\Administrator\nautilus_trader

# 检查文件是否存在
ls config_schemas.py

# 如果显示"找不到"，说明文件不在这个目录
```

### Q2: 如何确认文件在正确位置？

```powershell
# 显示文件完整路径
Get-ChildItem config_schemas.py | Select-Object FullName

# 应该显示：
# C:\Users\Administrator\nautilus_trader\config_schemas.py
```

### Q3: PowerShell 和 CMD 的区别？

如果您使用 PowerShell（默认），复制命令应该是：
```powershell
Copy-Item config_schemas.py config\schemas.py
Copy-Item config.yaml config\defaults.yaml
Copy-Item event_preprocessor.py services\market_matching\
```

如果切换到 CMD，命令是：
```cmd
copy config_schemas.py config\schemas.py
copy config.yaml config\defaults.yaml
copy event_preprocessor.py services\market_matching\
```

---

## 快速检查清单

- [ ] 已下载 `deploy.bat` 到项目根目录
- [ ] 已下载 `config_schemas.py` 到项目根目录
- [ ] 已下载 `config.yaml` 到项目根目录
- [ ] 已下载 `event_preprocessor.py` 到项目根目录
- [ ] 在 VSCode 终端确认当前目录是项目根目录
- [ ] 运行 `deploy.bat` 成功
- [ ] 文件复制成功
- [ ] `python run_web_panel.py` 启动成功
- [ ] 浏览器可以访问 http://localhost:8000

---

## 一键复制命令（PowerShell）

```powershell
# 确保在项目根目录
cd C:\Users\Administrator\nautilus_trader

# 运行部署
.\deploy.bat

# 复制文件（PowerShell 语法）
Copy-Item config_schemas.py config\schemas.py
Copy-Item config.yaml config\defaults.yaml
Copy-Item event_preprocessor.py services\market_matching\

# 启动面板
python run_web_panel.py
```

---

## 需要帮助？

如果仍然遇到问题，请提供：
1. 当前目录：`pwd` 的输出
2. 文件列表：`ls` 的输出
3. 具体的错误信息

我会帮您诊断问题！

# Web Gateway 故障排查指南

## Polymarket 发现不执行

### 可能原因和解决方法

1. **配置未加载或 Sports 为空**

   运行诊断脚本检查配置：
   ```bash
   python scripts/check_web_gateway_config.py
   ```

   如果看到警告 "⚠️ Polymarket 已启用但未配置 sports"，则需要在 Configuration 页面添加配置。

2. **查看后台日志**

   启动服务时添加 `--debug` 参数查看详细日志：
   ```bash
   python scripts/run_web_gateway.py --debug
   ```

   日志会显示：
   - `Polymarket: Starting discovery for X sports` - 开始发现
   - `Polymarket discovery failed: ...` - 发现失败（查看具体错误）
   - `Polymarket discovery is disabled` - 被禁用
   - `Polymarket: No sports configured, skipping discovery` - 未配置

3. **检查浏览器是否安装**

   Polymarket 发现需要 Playwright 和浏览器：
   ```bash
   # 安装 Playwright 浏览器
   playwright install chromium
   ```

4. **网络问题**

   确保可以访问 Polymarket API：
   ```bash
   curl https://gamma-api.polymarket.com/sports
   ```

5. **配置文件损坏**

   删除配置文件重新生成：
   ```bash
   rm src/arbitrage/services/web_gateway/default_config.json
   # 重启服务会自动生成新配置
   ```

## 查看实时日志

在启动服务的终端窗口可以看到实时日志：

```
INFO:root:Starting discovery for venue: all
INFO:root:Polymarket: Starting discovery for 1 sports
INFO:root:Discovering Polymarket events...
INFO:root:Discovered 15 Polymarket events
INFO:root:OrbitExch: Starting discovery for 1 sports
INFO:root:Discovering OrbitExch events...
INFO:root:Discovered 8 OrbitExch events
INFO:root:Discovery task completed
```

如果只看到 OrbitExch 的日志，说明 Polymarket 部分失败或被跳过。

## 常见错误

### Error: Playwright browser not installed

**解决方法：**
```bash
playwright install chromium
```

### Error: Connection refused / Timeout

**原因：** 网络问题或 Polymarket API 不可访问

**解决方法：**
- 检查网络连接
- 使用代理（如需要）
- 稍后重试

### Error: No events found

**原因：** 可能配置的 sport/competition 当前没有比赛

**解决方法：**
- 尝试其他 sports（如 Basketball, Tennis）
- 检查 Polymarket 网站是否有相关比赛

## 手动测试

可以单独测试 Polymarket 发现：

```python
import asyncio
from src.arbitrage.services.market_discovery.polymarket_scraper import PolymarketScraper
from src.arbitrage.services.market_discovery.config import PolymarketVenueConfig, SportConfig

async def test():
    config = PolymarketVenueConfig(
        sports=[SportConfig(sport="Soccer", competitions=["English Premier League"])]
    )
    scraper = PolymarketScraper(config)
    events = await scraper.discover_events(
        target_sports=["Soccer"],
        target_competitions=["English Premier League"]
    )
    print(f"Found {len(events)} events")
    await scraper.close_browser()

asyncio.run(test())
```

## 联系支持

如果问题仍然存在，请提供以下信息：
1. 诊断脚本输出（`python scripts/check_web_gateway_config.py`）
2. 完整的错误日志（使用 `--debug` 启动）
3. 操作系统和 Python 版本

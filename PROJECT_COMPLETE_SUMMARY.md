# 市场发现服务 - 完整项目总结

## 会话时间
2026-01-06

## 项目总体目标
构建一个跨平台的体育博彩市场发现和匹配系统，从多个平台（Polymarket, OrbitExch）抓取市场数据，进行智能匹配，识别套利机会。

---

## 整体架构

\\\

                    市场发现服务                          

                                                          
                        
    Polymarket             OrbitExch                
     Crawler                Crawler                 
                        
                                                       
          MarketEvent             MarketEvent          
                                                       
             
           Market Matcher                              
    - 标准化队名                                       
    - 模糊匹配                                         
    - 时间窗口过滤                                     
             
                                                         
          MatchedPair                                   
                                                         
             
        价格获取 & 套利识别                           
             
                                                          

\\\

---

## 数据模型

### MarketEvent (统一事件格式)

\\\python
@dataclass
class MarketEvent:
    platform: str          # 'Polymarket' 或 'OrbitExch'
    event_id: str         # 平台内的事件 ID
    sport: str            # 运动类型: 'Football', 'Basketball', 'Tennis'
    competition: str      # 赛事: 'EPL', 'NBA', 'ATP'
    event: str            # 对阵: "Team A vs Team B"
    home_team: str        # 主队
    away_team: str        # 客队
    metadata: dict        # 平台特定数据
\\\

---

## 已完成部分

### 1. OrbitExch Crawler

**文件**: services/market_discovery/orbitexch_crawler.py

**数据来源**: 
- 本地 JSON 文件: data/orbitexch_markets.json
- 包含预加载的市场数据

**实现逻辑**:
\\\python
1. 加载 JSON 文件
2. 遍历每个 sport:
   - 遍历每个 competition
   - 遍历每个 market
   - 提取 home/away 队名
3. 返回 List[MarketEvent]
\\\

**输出示例**:
\\\json
{
  "platform": "OrbitExch",
  "event_id": "12345",
  "sport": "Football",
  "competition": "English Premier League",
  "event": "Arsenal vs Chelsea",
  "home_team": "Arsenal",
  "away_team": "Chelsea",
  "metadata": {...}
}
\\\

---

### 2. Polymarket Crawler (最新版本 V3)

**文件**: services/market_discovery/polymarket_final_v3.py

#### 完整流程

##### 步骤 1: 爬取网页获取映射

访问 https://polymarket.com/sports:
\\\html
<div class="group/sports-item">
  <div>
    <p>American Football</p>  <!-- sport -->
  </div>
  <div>
    <a class="block">
      <p>NFL</p>  <!-- competition -->
    </a>
    <a class="block">
      <p>CFB</p>  <!-- competition -->
    </a>
  </div>
</div>
\\\

建立映射: {'NFL': 'American Football', 'CFB': 'American Football', ...}

##### 步骤 2: 获取 API 配置

\\\
GET https://gamma-api.polymarket.com/sports

返回:
[
  {
    "sport": "nfl",
    "tags": "100149,100150,100151"
  },
  ...
]
\\\

统计所有 tags 的重复次数，选择 **重复次数  2** 的 tags。

##### 步骤 3: 获取事件

从小到大遍历 tags:
\\\
GET https://gamma-api.polymarket.com/events?tag_id={tag}&closed=false&limit=10000
\\\

**找到事件就停止**遍历该 sport 的其他 tags。

##### 步骤 4: 解析事件

\\\python
# 从 title 提取主客队
title = "Brisbane International: Daniil Medvedev vs Marton Fucsovics"

1. 按 "vs." 或 "vs" 拆分
   home = "Brisbane International: Daniil Medvedev"
   away = "Marton Fucsovics"

2. 清理队名:
   主队: 去掉第一个 : 或 - 之前的内容  "Daniil Medvedev"
   客队: 去掉第一个 : 或 - 之后的内容  "Marton Fucsovics"

# 获取 competition
1. 先从 event.tags 找 (直接匹配网页映射)
2. 找不到再从 series[0].title 获取
3. 如果 series 为空  跳过该事件

# 匹配 sport
使用 getSimilar 算法匹配 API competition 和网页 competition
找到网页 competition 后，从映射获取对应的 sport
\\\

---

## getSimilar 算法 (核心匹配逻辑)

### 目的
智能匹配不同来源的 competition 名称，例如:
- API: "League of Legends" ↔ 网页: "LoL"
- API: "EPL" ↔ 网页: "English Premier League"

### 拆分规则

\\\python
def _split_elements(text):
    """
    按非字母数字字符拆分，然后:
    - 如果部分有 2 个大写字母  拆成单字符
    - 否则保留整个部分
    """
    
示例:
  "EPL"  ['E', 'P', 'L']  (3个大写)
  "LoL"  ['L', 'o', 'L']  (2个大写)
  "League of Legends"  ['League', 'of', 'Legends']  (每个只1个大写)
  "English Premier League"  ['English', 'Premier', 'League']
\\\

### 匹配规则

\\\python
def _get_similar(str1, str2) -> (match_count, char_count):
    """
    返回 (匹配数, 匹配字符总数)
    """
    
    1. 检查所有单个大写字母是否全部匹配:
       - 单个大写字母可以匹配:
         a) 相同的单个大写字母: E == E
         b) 单词的首字母: E == English[0]
       - 如果任何一个大写字母没匹配上  返回 (0, 0)
    
    2. 计算匹配数和字符数:
       - 遍历 str1 的每个元素
       - 在 str2 中找匹配 (未被使用的)
       - 已匹配的元素标记为已使用，不再参与后续匹配
       - 累计匹配数和字符数
    
    3. 比较优先级:
       - 先比匹配数
       - 匹配数相同时比字符数
\\\

### 匹配示例

#### 示例 1: LoL vs League of Legends
\\\
str1: "LoL"  ['L', 'o', 'L']
str2: "League of Legends"  ['League', 'of', 'Legends']

检查大写字母:
  - L 匹配 League[0] ✅
  - L 匹配 Legends[0] ✅

计算匹配:
  - L 匹配 League[0]  匹配数+1, 字符数+1
  - o 匹配 of (子序列)  匹配数+1, 字符数+2
  - L 匹配 Legends[0]  匹配数+1, 字符数+1

返回: (3, 4)
\\\

#### 示例 2: EPL vs Premier League
\\\
str1: "EPL"  ['E', 'P', 'L']
str2: "Premier League"  ['Premier', 'League']

检查大写字母:
  - E 在 str2 中没有匹配 ❌

返回: (0, 0)
\\\

#### 示例 3: EPL vs English Premier League
\\\
str1: "EPL"  ['E', 'P', 'L']
str2: "English Premier League"  ['English', 'Premier', 'League']

检查大写字母:
  - E 匹配 English[0] ✅
  - P 匹配 Premier[0] ✅
  - L 匹配 League[0] ✅

计算匹配:
  - E 匹配 English[0]  匹配数+1, 字符数+1
  - P 匹配 Premier[0]  匹配数+1, 字符数+1
  - L 匹配 League[0]  匹配数+1, 字符数+1

返回: (3, 3)
\\\

#### 示例 4: League of Legends vs Saudi Professional League
\\\
str1: "League of Legends"  ['League', 'of', 'Legends']
str2: "Saudi Professional League"  ['Saudi', 'Professional', 'League']

没有单个大写字母，跳过检查

计算匹配:
  - League 匹配 League (相同)  匹配数+1, 字符数+6
  - of 匹配 Professional (子序列)  匹配数+1, 字符数+2
  - Legends 没有匹配 ❌

返回: (2, 8)
\\\

---

## 队名清理规则

\\\python
def _clean_team_name(team_name, is_home):
    """
    主队: 去掉第一个 : 或 - 之前的内容
    客队: 去掉第一个 : 或 - 之后的内容
    最后去掉左右空格
    """

示例:
  主队: "Brisbane International: Medvedev"  "Medvedev"
  客队: "Fucsovics - Extra Info"  "Fucsovics"
  主队: "Team A"  "Team A" (没有分隔符，保持不变)
\\\

---

## 关键决策记录

### Polymarket API 选择

**问题**: 最初尝试使用 CLOB API 和 NautilusTrader

**决策**: 使用 Gamma API
- CLOB API 的过滤参数不工作
- NautilusTrader 配置复杂且有错误
- Gamma API 简单、稳定、有文档

### Tag 选择策略演进

1. **初版**: 选择重复最少的单个 tag
2. **最终版**: 
   - 选择重复 2 的所有 tags
   - 从小到大遍历
   - 找到事件就停止

**原因**: 有些 sport 的最少 tag 可能没有活跃事件

### Competition 来源优先级

1. **先从 event.tags 找** (直接匹配网页)
2. **再从 series.title 找** (需要相似度匹配)

**原因**: tags 中的 competition 更准确，直接对应网页

### 相似度算法演进

1. **V1**: 简单子序列匹配
2. **V2**: 处理缩写词 (EPL, LoL)
3. **V3**: 大写字母必须全部匹配
4. **V4**: 已匹配元素不重复
5. **最终**: 加入字符数比较

**原因**: 避免错误匹配，如 "League of Legends" vs "Saudi Professional League"

---

## 待开发部分

### 3. Market Matcher

**目标**: 匹配 Polymarket 和 OrbitExch 的相同事件

**文件**: services/market_discovery/market_matcher.py

**核心功能**:
\\\python
class MarketMatcher:
    def match_events(
        self, 
        polymarket_events: List[MarketEvent],
        orbitexch_events: List[MarketEvent]
    ) -> List[MatchedPair]:
        """
        1. 按 sport + competition 分组
        2. 标准化队名
        3. 使用模糊匹配找相同对阵
        4. 检查时间窗口
        5. 返回匹配对
        """
\\\

**匹配策略**:
- Levenshtein 距离匹配队名
- 允许顺序不同 (A vs B = B vs A)
- 时间窗口: 24小时

### 4. 价格获取

**目标**: 获取匹配事件的实时赔率

**实现**:
- Polymarket: 使用 CLOB API 获取 order book
- OrbitExch: 从 API 获取赔率

### 5. 套利识别

**目标**: 计算并识别套利机会

**公式**:
\\\
套利机会 = (1/odds1 + 1/odds2) < 1
\\\

---

## 项目文件结构

\\\
services/market_discovery/
 polymarket_final_v3.py          # Polymarket 爬虫 (最新)
 orbitexch_crawler.py            # OrbitExch 爬虫
 market_matcher.py               # 匹配引擎 (待开发)
 README.md

data/
 orbitexch_markets.json          # OrbitExch 数据源

输出:
 polymarket_events.json          # Polymarket 事件
 orbitexch_events.json           # OrbitExch 事件
 market_matches.json             # 匹配结果 (待生成)
\\\

---

## 使用示例

### 运行 Polymarket 爬虫
\\\powershell
python services/market_discovery/polymarket_final_v3.py
\\\

### 运行 OrbitExch 爬虫
\\\powershell
python services/market_discovery/orbitexch_crawler.py
\\\

### 运行匹配器 (待开发)
\\\powershell
python services/market_discovery/market_matcher.py
\\\

---

## 输出格式示例

### Polymarket Event
\\\json
{
  "platform": "Polymarket",
  "event_id": "140070",
  "sport": "Tennis",
  "competition": "ATP",
  "event": "Daniil Medvedev vs Marton Fucsovics",
  "home_team": "Daniil Medvedev",
  "away_team": "Marton Fucsovics",
  "metadata": {
    "title": "Brisbane International: Daniil Medvedev vs Marton Fucsovics",
    "start_date": "2026-01-03T08:34:12.396961Z",
    "api_competition": "ATP"
  }
}
\\\

### OrbitExch Event
\\\json
{
  "platform": "OrbitExch",
  "event_id": "12345",
  "sport": "Football",
  "competition": "English Premier League",
  "event": "Arsenal vs Chelsea",
  "home_team": "Arsenal",
  "away_team": "Chelsea",
  "metadata": {
    "market_id": "67890",
    "start_time": "2026-01-06T15:00:00Z"
  }
}
\\\

---

## 下一步计划

1. **完成 Market Matcher**
   - 实现队名标准化
   - 实现模糊匹配
   - 添加时间窗口过滤

2. **价格获取模块**
   - Polymarket CLOB API 集成
   - OrbitExch 赔率 API

3. **套利计算**
   - 实时监控
   - 套利机会通知

---

## 在新会话中继续

1. 上传此文档 PROJECT_COMPLETE_SUMMARY.md
2. 上传代码文件 (如果需要):
   - polymarket_final_v3.py
   - orbitexch_crawler.py
3. 说明要继续的功能，例如:
   - "继续开发 Market Matcher"
   - "修复 Polymarket 爬虫的某个问题"
   - "添加新的平台支持"

我会立即理解上下文并继续工作！


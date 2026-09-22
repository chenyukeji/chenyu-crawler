# Amazon New Releases Collector

本项目负责在本机采集 Amazon New Releases、新品榜历史留存和确定性趋势分析，并向上层选品 Skill 提供只读查询能力。

核心原则：

- 爬虫负责采集事实。
- SQLite 负责保存短期榜单历史和轻量 ASIN 身份历史。
- Python 分析层负责计算“新出现、重复上榜、排名上升、同类集中出现”等确定性指标。
- Skill 负责基于这些结果做进一步选品判断。
- MCP 只作为可选的 AI 查询接口，不承担采集和业务计算。
- 遇到 Amazon 登录、验证码、Robot Check、CAPTCHA、HTTP 403/429/503 等访问控制时立即停止，不绕过。

## 当前目录

```text
amazon-new-release-collector/
│
├── crawler/
│   ├── __init__.py
│   ├── amazon.py              # Amazon 页面采集、解析、去重、风控检测
│   └── categories.py          # 数据源读取、验证、筛选
│
├── data/
│   └── new_releases.db        # 本地 SQLite
│
├── database/
│   ├── __init__.py
│   ├── schema.py              # observations / product_seen 当前表结构
│   └── repository.py          # 写入、清理、只读连接
│
├── analysis/
│   ├── __init__.py
│   ├── new_entries.py         # 新出现 ASIN
│   ├── repeat_products.py     # 重复上榜 / 连续快照
│   ├── rank_trends.py         # 排名历史 / 上升趋势
│   └── product_clusters.py    # 基于 product_type 的产品簇
│
├── services/
│   ├── __init__.py
│   └── selection_queries.py   # 给 Skill/MCP 使用的稳定查询接口
│
├── mcp/
│   └── server.py              # 可选、本地只读 MCP 工具层
│
├── config/
│   └── sources.json
│
├── env/
│   ├── browser.json
│   ├── requirements.txt
│   └── setup.ps1
│
├── outputs/
│   └── daily/
│
├── tests/
│   ├── test_crawler.py
│   └── test_analysis.py
│
├── run_daily.py               # 推荐的每日采集入口
├── TASK_BOARD.md
└── README.md
```

## 数据保留策略

详细榜单快照默认保留 **10 天**：

```text
observations
```

用于计算：

- 最近 10 天出现天数
- 连续采集快照上榜次数
- 当前排名
- 排名历史
- 排名上涨/下降
- product_type 聚类

另外保留轻量 ASIN 身份表：

```text
product_seen
```

默认保留 **90 天**，只用于识别产品以前是否出现过，避免 10 天详细快照清理后把历史 ASIN 误判为“全新出现”。

因此：

```text
详细榜单：10 天
ASIN 身份：90 天
```

## 数据库字段

### observations

每天、每个榜单记录事实快照：

```text
source_url
marketplace
category
snapshot_date
rank
asin
title
review_count
price
price_text
rating
product_type
product_url
image_url
selling_points_json
selling_points_source
created_at
```

不保存 `previous_rank`、`rank_velocity` 等衍生值；这些由分析层根据历史快照实时计算。

### product_seen

```text
marketplace
asin
first_seen
last_seen
title
product_type
product_url
image_url
```

## 选品查询接口

`services/selection_queries.py` 是选品 Skill 应优先调用的接口：

```python
get_new_entries(...)
get_repeat_products(...)
get_rising_products(...)
get_rank_history(...)
get_hot_clusters(...)
get_selection_candidates(...)
```

### NEW

最近首次进入本地新品榜历史的产品。

### REPEAT

过去 N 天多次出现在新品榜中的产品。

### RISING

根据实际历史排名自动计算，例如：

```text
43 → 17 → 8
```

程序计算：

```text
previous_rank = 17
current_rank = 8
period_rank_change = +35
rank_velocity = 17.5
```

这些值不需要额外存进数据库。

### TREND / 产品簇

当前第一版使用 `product_type` 做确定性聚类，用于发现多个不同 ASIN 是否集中属于同一种产品概念。后续可再升级语义聚类。

## MCP

MCP 是可选层。

数据库本身不是 MCP，结构是：

```text
new_releases.db
      ↓
analysis/
      ↓
services/selection_queries.py
      ↓
mcp/server.py
      ↓
chenyu-xuanpin Skill
```

MCP 不允许 AI 自由执行 SQL，而只暴露固定查询工具：

```text
get_new_entries
get_repeat_products
get_rising_products
get_rank_history
get_hot_clusters
get_selection_candidates
```

MCP 查询路径使用只读 SQLite 连接。

## 安装

```powershell
.\env\setup.ps1
```

依赖：

```text
playwright>=1.49
mcp[cli]>=2,<3
```

## 配置检查

不访问 Amazon：

```powershell
.\env\.venv\Scripts\python.exe .\run_daily.py --validate-only
```

## 每日采集

```powershell
.\env\.venv\Scripts\python.exe .\run_daily.py
```

只测试一个榜单：

```powershell
.\env\.venv\Scripts\python.exe .\run_daily.py --source-limit 1
```

程序会：

1. 打开可见浏览器。
2. 读取 `config/sources.json`。
3. 逐榜单采集。
4. 写入 `outputs/daily/YYYY-MM-DD/raw/`。
5. 写入 `data/new_releases.db`。
6. 自动清理超过 10 天的详细榜单。
7. 保留最近 90 天 ASIN 身份。
8. 生成 `run_summary.json`。

## 本地直接查询

例如：

```powershell
.\env\.venv\Scripts\python.exe -c "from services.selection_queries import get_selection_candidates; print(get_selection_candidates('DE')[:5])"
```

## MCP 启动

安装依赖后：

```powershell
.\env\.venv\Scripts\python.exe .\mcp\server.py
```

这是 stdio MCP Server，通常由 ChatGPT/Codex/AgentDock 等 MCP Host 启动，不需要员工手工长期保持窗口运行。

## 测试

```powershell
.\env\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试不会访问 Amazon。

## 边界

本项目负责：

```text
Amazon新品榜采集
→ 本地历史
→ 确定性趋势计算
→ 对 Skill 提供查询
```

本项目不负责：

```text
最终开品判断
卖家精灵市场验证
1688供应链验证
利润决策
Amazon Listing发布
```

这些由上层 `chenyu-kaifa / chenyu-xuanpin` 工作流继续执行。

# Amazon New Releases Collector

一个精简的 Amazon 新品榜采集器：每天自动采集一次，将最近 7 天快照保存在 SQLite，并通过一个网页配置运行时间和数据源。

## 结构

```text
config/sources.json            # 每日时间、时区和数据源
crawler/amazon.py              # 页面采集、解析、去重和风控检测
crawler/config.py              # 配置读取、验证和保存
database/schema.py             # observations 与 scheduler_state
database/repository.py         # 快照写入和 7 天清理
api/main.py                    # 唯一配置页面
web/templates/index.html       # 配置页面模板
run_daily.py                   # 单次采集入口
scheduler_loop.py              # 每日调度入口
data/new_releases.db           # 本地 SQLite
```

## 数据库

`observations` 保存榜单快照：

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
product_url
image_url
created_at
```

同一数据源、日期和 ASIN 只保存一条记录。同日重跑会替换当天快照。快照最多保留最近 7 个日历日。

`scheduler_state` 只保存最近一次每日任务的日期、状态和日志摘要，确保每天最多自动执行一次。

## 安装

```powershell
.\env\setup.ps1
```

## 配置页面

启动配置页和每日调度：

```powershell
.\env\.venv\Scripts\python.exe -m uvicorn api.main:app --host 0.0.0.0 --port 8000
```

打开：

```text
http://localhost:8000/
```

页面提供：

- 每日运行时间，默认 `06:00`。
- 启用或停用具体榜单数据源。
- 新增美国站或德国站 New Releases 类目。
- 删除不再采集的类目配置（历史快照仍按 7 天规则清理）。
- 点击“立即运行”异步采集全部已保存且已启用的数据源。
- 查看快照记录、唯一 ASIN、最近运行状态和近 7 天采集量。

默认时区为 `Asia/Shanghai`，固定保留期为 7 天。

## 运行

检查配置，不访问 Amazon：

```powershell
.\env\.venv\Scripts\python.exe .\run_daily.py --validate-only
```

测试一个榜单：

```powershell
.\env\.venv\Scripts\python.exe .\run_daily.py --source-limit 1
```

手工运行全部已启用榜单：

```powershell
.\env\.venv\Scripts\python.exe .\run_daily.py
```

网页服务启动时会同时启动后台调度器。调度器到达配置时间后执行一次 `run_daily.py`，并在当天剩余时间内不再重复自动执行。

## SQLite 只读访问

```python
import sqlite3
from pathlib import Path

db_path = Path("data/new_releases.db").resolve()
connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
```

## 测试

```powershell
.\env\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试不会访问 Amazon。

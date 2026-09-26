# Amazon New Releases Collector

一个精简的 Amazon 新品榜采集器：每天自动采集一次，将最近 7 天快照保存在 SQLite，并通过一个网页配置运行时间和数据源。

## 结构

```text
config/sources.json            # 每日时间、时区和数据源
crawler/amazon.py              # 页面采集、解析、去重和风控检测
crawler/config.py              # 配置读取、验证和保存
database/schema.py             # 三张采集事实表
database/repository.py         # 快照、首次出现、任务记录与 7 天清理
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

`product_seen` 按站点和 ASIN 保存历史最早、最近出现日期，不随 7 天快照清理，供插件准确判断“首次出现”。

`collection_runs` 按数据源保存每次任务的开始、结束、状态、采集数量和错误信息，用于区分空榜与采集失败。状态区分 `QUEUED`、`RUNNING`、`COMPLETE`、`PARTIAL`、`BLOCKED`、`SKIPPED`、`PARSE_ERROR`、`FAILED`、`INTERRUPTED`。

数据库不保存标题相似度、图片向量、商品分组、趋势结果或任何模型输出；这些分析由插件只读查询后临时计算。

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

网页服务启动时会同时启动后台调度器。调度器到达配置时间后执行一次 `run_daily.py`，同日已尝试的类目不会重复进入初始轮；未完成项按延迟补抓策略处理，手动排队任务独立处理。

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

## 失败诊断与自动重试

临时网络/页面错误会在同一轮任务中自动尝试最多 3 次，间隔 5 秒、10 秒。榜单部分完成时先保存有效结果，进入 15/60 分钟的持久化延迟补抓，不立即重刷整个榜单。同一轮重试保留已成功页面，并直接恢复失败页；失败页每次结果独立替换，不累加多个失败尝试的商品。跨轮不拼接旧排名。重试结果比当天已存快照更少时，不覆盖已有数据。

明确的登录、验证码、403/429 或 unauthorized AI agent 拦截不会立即反复重试；仅停止该类目的立即重试，其他类目仍独立请求并记录结果。当天调度仍只启动一轮，下一天重新执行；立即运行也会使用上述重试逻辑。

每次尝试的状态和原因保存在 `data/diagnostics/<运行时间>/<站点_类目>/attempts.json`，失败时同时保存当时 HTML。控制台和服务日志保留采集输出。页面日志标明站点、类目、缺失排名和分页统计。部分成功会单独显示，不能当作完整 Top 100 使用。

解析保留 Amazon 页面真实排名；滚动至每页 50 条，并在底部回滚触发遗漏的懒加载（最多 40 步），再解析，并检查 Top 100 排名是否完整。

## 可靠性更新：延迟补抓与异常恢复

Linux 服务使用操作系统 `flock` 锁，锁文件存在本身不表示任务运行，不能手工删除锁文件判断或解除并发。进程退出（包括强制结束）后锁自动释放；每分钟调度检查及采集启动前，在持有同一锁时将遗留 RUNNING 任务恢复为 INTERRUPTED，保留已有快照。子进程超时先终止进程组，10 秒未退出再强制结束并恢复状态。

数据库状态：QUEUED（等待中）、RUNNING（采集中）、COMPLETE（完整）、PARTIAL（部分）、BLOCKED（访问受限）、SKIPPED（未请求）、PARSE_ERROR（解析失败）、FAILED（其他采集失败）、INTERRUPTED（异常中断）。旧表约束会事务性迁移，保留历史记录、原因及商品数据；明确可识别的旧错误重新分类。

对 PARTIAL、PARSE_ERROR、FAILED、INTERRUPTED，在当前轮结束后分别等待 15 分钟和 60 分钟，最多追加两轮。`retry_round`、`next_retry_at` 持久化在 collection_runs；调度每分钟仅选择启用且到期的当天榜单。每轮内部仅临时错误最多尝试三次，部分完成直接结束当前轮，因此一个榜单每天自动采集最多三轮、九次尝试（不含主动手动重爬）。成功、达到轮次上限或跨日不再补抓；BLOCKED/SKIPPED 类目不加入自动补抓，其他类目不受影响；可按需手动重爬。手动运行也不会清零当天已有补抓轮次。旧库升级时仅为今天最新可重试结果安排升级后 15 分钟的补抓。

`run_daily.py --retry-due` 执行到期补抓；`--scheduled` 用于每日初始轮，在进程锁内再次确认当天未执行。页面按每个榜单最新记录显示具体状态、原因、轮次和下次补抓时间。

排名必须来自页面明确的排名徽章（含卡片外层），缺少/无效排名会记录为解析问题并排除该条目。去重和数据库写入均不允许用位置补造排名。历史快照不自动改写为“已验证真实排名”，需通过后续真实采集刷新。

## 按类目独立采集与今日卡片

不再由某一个类目受限推断整个站点都应跳过。每个类目分别请求、分别记录结果；明确受限的该类目仍不做无效自动重试，其他类目照常执行。

页面按卡片显示今天的已完成、等待中、采集中、部分完成、访问受限等状态，以及今日保存数、最近运行结果、原因和补抓时间。卡片每 5 秒更新，不刷新整页或覆盖未保存的配置。可按站点、类目和状态筛选。

“重新采集”仅提交该类目；其他任务正在运行时进入持久化 QUEUED 队列，不会被拒绝或误标为 RUNNING。重复提交同一类目不会重复排队。排队请求每分钟由调度检查，也可立即启动空闲的工作进程；重启后继续处理。只在开始请求此类目时将其改为 RUNNING。已停用、已删除或跨日的排队请求会取消并写明原因。

单类目入口为 POST /source/run（source_id），只接受已配置且启用的类目。GET /status 返回今日卡片状态。命令行 `--queued` 处理已排队请求，`--source-id US_beauty` 可只运行一个指定类目。当天先手动爬一个类目不会阻止其他类目的每日计划任务。

## 访问受限诊断与分页检查点

每次尝试记录开始时间、耗时、保留条数。明确受限时额外记录原因类型（AGENT_RESTRICTED、LOGIN_REQUIRED、VERIFICATION_REQUIRED、RATE_LIMITED、HTTP_FORBIDDEN）、检测阶段（首次打开/滚动后/翻页后）、页码、HTTP 状态以及允许列表内的响应头（包括 Retry-After）。这些是可观察事实，不代表已确定站点内部风控规则。明确限制仍停止当前类目，不自动绕过。

每个类目的 `environment.json` 记录实际浏览器版本、配置、User-Agent、webdriver 和语言信息；不记录 Cookie、Authorization 或完整请求头。`attempts.json` 和失败 HTML 留存用于离线对比。每页解析后原子保存 `best-snapshot.json`，翻页受限时保留已解析页面的数据并入库，最终仍标 BLOCKED；不会把部分数据标成完整榜单。异常退出时该 JSON 可供恢复核对，但不承诺自动导入进程被杀前尚未入库的检查点。

验证命令：`.venv/bin/python -m unittest discover -s tests -q`。浏览器回归测试以本地路由模拟 HTTP 200 限制页面，所有外部请求均拦截，不访问 Amazon。

## 不足上限的完整榜单

100 条是采集上限，不是最低完成数量。末页加载稳定、无下一页、实际排名从 1 连续到末项、无拒收卡片或重复商品导致的数据缺失时，少于上限也标记 COMPLETE，并取消延迟补抓。空页、排名断档、解析异常、未稳定或分页异常仍不算完成。卡片按已完成榜单的实际条数显示进度，例如 99/99。

翻页前额外检查 document.readyState=complete，且商品内容连续 3 秒稳定；最长等候 30 秒。未稳定时保留有效数据并在本轮重试当前页；本轮重试耗尽后交给延迟补抓，不继续向后翻页。每页诊断的 load_wait 记录完成状态及等待时长。此检查用于避免提前翻页，并不代表能解除服务端访问限制。

## 逐请求诊断脚本

`.venv/bin/python scripts/diagnose_collection.py --source-id US_kitchen` 使用当前正常浏览器配置执行一个类目的一次采集，支持重复传入 `--source-id`。有效商品按正常采集规则入库。诊断文件 `network-diagnostic.json` 保存同站主文档、XHR/fetch 的状态、耗时、正文限制标记及请求失败；请求完成时立即读取正文，避免翻页后浏览器释放响应。不会保存 Cookie、Authorization 或跟踪查询参数，不尝试隐藏自动化或绕过明确访问限制。退出码 2 表示未完整完成。

`env/browser.json` 的 `between_pages_seconds` 控制页面完成加载后额外等待多久再翻页；当前为 60 秒，范围 0–300 秒。每轮任务仍新建浏览器，分页保留同一会话；等待结束后若页面出现明确限制，则停止当前类目。

## 按页恢复（本轮内）

正常翻页使用页面真实分页链接：优先选中数字页码按钮，滚动至可见后点击并等待主文档加载；没有数字按钮时点击同一目标的“下一页”。不会通过拼接 URL 代替正常点击；临时失败恢复仍使用已记录的真实页面 URL。该操作不保证解除服务端访问限制。

第一页成功后，检查点保留商品、真实排名、已访问链接、分页诊断和采集时间。第二页发生超时、空白页面、加载未稳定或卡片解析异常时，下一次尝试直接打开页面实际提供的第二页 URL，不重新访问第一页。当前轮最多尝试 3 次，临时失败之间等待 5/10 秒；不同页的正常翻页间隔由 between_pages_seconds 控制。

`resume-checkpoint.json` 记录失败页地址及此前成功页面，`attempts.json` 的 retry_page_url 标明恢复位置。检查点用于当前 collect_with_retry 调用；下一轮、跨日或进程重启不会自动将旧检查点拼入新榜单。失败页重试不叠加多份结果；跨页排名冲突、断档或重复 ASIN 造成的不足仍保持部分完成。明确访问限制直接停止，保留当前已返回的有效商品。

回归测试覆盖第二页超时后恢复、空页后恢复、重试耗尽保留第一页、明确限制不继续重试，以及 99 条真实末页完成。部署环境为 Linux（进程锁使用 flock），仓库默认浏览器为 Playwright Chromium 无界面模式。

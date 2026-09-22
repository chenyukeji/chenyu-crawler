# Amazon New Releases Collector — Task Board

更新时间：2026-09-21

## 当前目标

把现有单文件 `collector.py` 重构成“采集 → SQLite → 分析 → Service → MCP → 选品 Skill”的可维护结构，同时保留现有真实历史数据。

## 已完成

- [x] T01 检查现有项目、数据库、配置和测试。
- [x] T02 建立 `crawler/`，把 Amazon 采集、解析、ASIN 去重和访问控制检测从主入口拆出。
- [x] T03 建立 `crawler/categories.py`，统一数据源读取、校验和 source id。
- [x] T04 将 `data/trends.db` 保留数据后重命名为 `data/new_releases.db`。
- [x] T05 建立 `database/schema.py`，增加 `category` 字段、查询索引和 `product_seen` 身份表。
- [x] T06 建立 `database/repository.py`，负责写入、10 天详细历史清理、90 天 ASIN 身份清理和历史 category 回填。
- [x] T07 建立 `analysis/new_entries.py`，识别真正首次出现的 ASIN。
- [x] T08 建立 `analysis/repeat_products.py`，统计出现天数、重复率和连续采集快照。
- [x] T09 建立 `analysis/rank_trends.py`，从历史自动计算 previous rank、排名变化和 rank velocity，不在原始表重复存字段。
- [x] T10 建立 `analysis/product_clusters.py`，第一版按归一化 `product_type` 识别同类新品集中出现。
- [x] T11 建立 `services/selection_queries.py`，给 Skill/MCP 提供固定查询接口。
- [x] T12 建立 `mcp/server.py`，只开放固定查询工具，不开放自由 SQL。
- [x] T13 建立 `run_daily.py` 作为新的每日采集入口。
- [x] T14 保留 `collector.py` 兼容入口，避免原有命令立即失效。
- [x] T15 更新配置：详细快照 10 天，ASIN 身份 90 天。
- [x] T16 新增分析层自动测试。
- [x] T17 现有真实数据库完成 schema 迁移和 9,730 条历史 category 回填。
- [x] T18 使用真实数据库验证 NEW / REPEAT / RISING / CLUSTER / CANDIDATES 查询均能返回结果。
- [x] T19 当前本地测试：11/11 通过。

## 当前进行

- [x] T20 MCP 运行环境安装与 smoke test：本机已安装 mcp 2.2.0，MCPServer 可加载并能读取候选结果。
- [ ] T21 用 `run_daily.py --source-limit 1` 做一次真实浏览器采集回归，确认 Amazon 当前页面结构仍与 selector 匹配。

## 下一阶段：接选品 Skill

- [ ] T22 新建/升级 `chenyu-xuanpin`，把 `get_selection_candidates` 作为新品发现入口。
- [ ] T23 Skill 分别读取 NEW / REPEAT / RISING 信号并解释“为什么进入候选池”。
- [ ] T24 把 hot clusters 加入候选解释，识别“多个不同 ASIN 同类集中出现”的趋势。
- [ ] T25 候选品进入卖家精灵二次验证：搜索量、销量、价格、评论、竞争度、垄断情况。
- [ ] T26 候选品进入 1688 供应链验证：现货、采购价、MOQ、重量尺寸和同款数量。
- [ ] T27 接利润计算，形成“发现 → 市场验证 → 供应链 → 利润 → 开品”的完整流程。

## 数据质量升级

- [ ] T28 评估 `product_type` 规则聚类误差；必要时增加更稳定的 product concept 归一化。
- [ ] T29 对重点候选才抓详情页高成本字段，避免每天对全部新品做详情页访问。
- [ ] T30 增加采集缺失日处理，区分“产品掉榜”和“当天爬虫未成功”。
- [ ] T31 增加异常监控：某数据源条数突然下降、全部 rank 变化异常、连续 blocked 等。

## 站点扩展

- [ ] T32 当前 US/DE 稳定后，再补 FR / IT / ES / UK New Releases 数据源。
- [ ] T33 按站点独立计算趋势，避免跨站点排名混用。

## 验收标准

第一阶段完成的最低标准：

1. `run_daily.py --validate-only` 成功。
2. 本地单元测试全部通过。
3. 现有数据库无数据丢失。
4. 新数据写入 `data/new_releases.db`。
5. 详细榜单只保留 10 天。
6. ASIN 身份可保留 90 天。
7. `get_new_entries` 能找首次出现。
8. `get_repeat_products` 能算重复和连续上榜。
9. `get_rising_products` 能从历史自动算排名趋势。
10. MCP 只读，不允许任意 SQL 或写数据库。

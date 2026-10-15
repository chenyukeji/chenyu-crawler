# 晨玙爬虫 · Chenyu Crawler

从美国站、德国站 Amazon New Releases 榜单采集 ASIN、排名、标题、价格、评分等公开信息，保存最近 7 个日历日的榜单快照，并保留 ASIN 首次及最近出现记录。

- 入口：`run_daily.py`；每日调度：`scheduler_loop.py`。
- 管理网页：`api/main.py`，提供采集总览、今日采集、来源配置和管理员登录。
- 数据库：`data/new_releases.db`；配置：`config/sources.json`。
- 详细配置、重试与恢复规则见 [新品榜使用说明](docs/NEW_RELEASES.md)。

## 安装和运行

需要 Python 3.10+；完整采集环境使用 Linux。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r env/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python run_daily.py --validate-only
.venv/bin/python run_daily.py --source-limit 1
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8101
```

管理页使用晨玙网站管理员账号登录，认证服务通过 `CHENYU_WEB_AUTH_BASE` 配置。每日时间和时区在管理页配置。来源失败、访问限制及不完整结果仍记录明确状态，不伪造完整数据。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

服务器目录与部署说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。历史运行资料保留在原数据目录，不提交 Git。

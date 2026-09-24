# 服务器运行目录

晨玙项目统一位于 `/home/ubuntu/chenyu`，本采集器目录为 `/home/ubuntu/chenyu/chenyu-crawler`。

- 服务：`chenyu-crawler.service`
- 工作目录：`/home/ubuntu/chenyu/chenyu-crawler`
- 启动命令：`.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8100`
- 数据库：`data/new_releases.db`
- 配置：`config/sources.json`
- 网站项目：`/home/ubuntu/chenyu/chenyu-web`
- AI 插件：`/home/ubuntu/chenyu/chenyu-ai`

2026-09-24 从旧 projects 目录迁移，保留原代码、未提交改动、配置与数据库；旧目标副本归档在 `/home/ubuntu/chenyu/.migration-backups/`。

`/home/ubuntu/projects` 仅保留配套软件，不再存放晨玙项目。账号密码、数据库及浏览器登录状态不提交 Git。

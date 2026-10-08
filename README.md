# 晨玙爬虫 · Chenyu Crawler

本项目包含两个 Amazon 数据采集功能：**新品榜采集**和**意大利站店铺税号采集**。两者共用 Python 依赖，分别使用独立入口、数据库和结果目录。

## 功能一：Amazon 新品榜采集

从配置的美国站、德国站 New Releases 榜单采集 ASIN、真实排名、标题、价格、评分等公开信息，保存最近 7 个日历日的榜单快照，并保留 ASIN 首次及最近出现记录。

- 入口：`run_daily.py`；数据库：`data/new_releases.db`。
- 支持管理网页、每日定时运行、按类目排队、失败诊断和延迟补抓。
- 现有新品榜采集及网页服务依赖 Linux 的进程锁，完整运行环境为 Linux。

详细配置、数据结构、重试与恢复规则见 [新品榜使用说明](docs/NEW_RELEASES.md)。

## 功能二：意大利站店铺税号采集

从卖家精灵「选产品」筛选**意大利站、商品上架时间近 30 天**，分页获取商品 ASIN 和 BuyBox 卖家，汇总店铺链接后访问 Amazon.it 公开商业信息，提取公司名称、公司地址和 VAT/IVA 税号。

- 入口：`run_seller_vat.py`；数据库：`data/seller_vat.sqlite3`。
- 结果：`outputs/seller-vat/YYYY-MM-DD/`，包含店铺税号 CSV、商品来源 CSV、运行状态和分页记录。
- 支持网页登录、店铺去重、税号阶段断点续跑、价格分区和完整性核对。
- 支持 Windows 和 Linux；Linux 服务器可通过独立后台服务每小时采集一轮，管理网页只读展示店铺税号和商品来源。服务器完成卖家精灵网页登录后自动开始；真实网页筛选及全量采集仍需实测。

「近 30 天上架」指商品上架时间，店铺范围为这些商品对应的 BuyBox 卖家，不表示店铺在近 30 天注册，也不表示包含全部跟卖商家。采集范围受当前卖家精灵账号权限及可查询数据限制；无法确认覆盖全部结果时标为部分完成。

登录、续跑、参数及数据口径见 [税号采集使用说明](SELLER_VAT.md)。

## 安装

需要 Python 3.10+。首次安装或更新代码后，重新安装依赖。

### Windows：税号采集

在项目根目录运行：

```powershell
.\env\setup.ps1
```

默认使用本机 Chrome，也可用 `--channel msedge`。使用 Playwright Chromium 时运行 `.\env\setup.ps1 -InstallChromium`。

### Linux：两个采集功能

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r env/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

Linux 首次登录卖家精灵需要桌面或远程桌面，并使用运行采集程序的同一服务器用户。

## 运行新品榜采集

在 Linux 项目根目录运行。先检查配置，再试跑一个榜单，最后运行全部已启用榜单：

```bash
.venv/bin/python run_daily.py --validate-only
.venv/bin/python run_daily.py --source-limit 1
.venv/bin/python run_daily.py
```

启动新品榜管理网页及后台每日调度：

```bash
.venv/bin/python -m uvicorn api.main:app --host 0.0.0.0 --port 8100
```

访问 `http://localhost:8100/`，使用晨玙网站的管理员账号登录。网页可配置采集时间和榜单来源，查看任务状态并手动运行。默认每日时间为 `06:00`，时区为 `Asia/Shanghai`；认证服务地址通过 `CHENYU_WEB_AUTH_BASE` 配置。

## 运行税号采集

### Windows

先登录卖家精灵，再小批试跑，确认正常后运行全部可访问结果：

```powershell
.\env\.venv\Scripts\python.exe run_seller_vat.py login
.\env\.venv\Scripts\python.exe run_seller_vat.py run --max-pages 1 --limit 5
.\env\.venv\Scripts\python.exe run_seller_vat.py run
```

也可依次双击 `env/login_sellersprite.cmd` 和 `env/run_seller_vat.cmd`。

### Linux

```bash
.venv/bin/python run_seller_vat.py login --channel chromium
.venv/bin/python run_seller_vat.py run --channel chromium --headless
```

税号阶段续跑时指定原运行日期，例如：

```bash
.venv/bin/python run_seller_vat.py run --resume --run-id 2026-10-08 --channel chromium --headless
```

店铺税号 CSV 可用 Excel 打开；导入时将税号列设为文本，保留前导零。

服务器常驻采集使用 `seller_vat_loop.py` 和 `deploy/chenyu-seller-vat.service`：每小时最多启动一轮，前一轮未结束时不并发。尚未登录或登录失效时等待服务器上的人工网页登录，状态显示在管理网页“店铺税号”页。安装和首次登录步骤见 [税号采集说明](SELLER_VAT.md)。

## 目录结构

```text
crawler/amazon.py          # 新品榜页面采集与解析
crawler/seller_vat.py      # 商品发现、店铺链接与公开税号采集
config/sources.json       # 新品榜来源、每日时间和时区
database/                 # 新品榜数据库与任务记录
api/                      # 新品榜管理接口与管理员认证
web/templates/            # 管理网页模板
env/                      # 共用依赖、安装及 Windows 税号启动文件
run_daily.py              # 新品榜命令入口
scheduler_loop.py         # 新品榜每日调度
run_seller_vat.py         # 税号采集命令入口
data/new_releases.db      # 新品榜数据
data/seller_vat.sqlite3    # 商品、店铺及税号数据
data/sellersprite-profile/ # 本机卖家精灵登录会话，不提交 Git
outputs/seller-vat/       # 税号 CSV、来源与运行状态，不提交 Git
docs/NEW_RELEASES.md      # 新品榜详细说明
SELLER_VAT.md             # 税号采集详细说明
tests/                    # 两个模块的离线回归测试
```

## 测试与数据保护

Linux 完整测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Windows 税号模块测试：

```powershell
.\env\.venv\Scripts\python.exe -m unittest discover -s tests -p test_seller_vat.py -v
```

GitHub Actions 在 Linux 执行离线回归测试，不使用卖家精灵账号进行真实采集。账号密码、登录会话、数据库和税号采集结果不提交 Git。遇到明确登录要求、验证码或访问限制时保存有效进度并停止，不绕过访问控制；缺失数据和部分完成不会标为完整结果。

服务器运行目录与现有服务说明见 [部署说明](DEPLOYMENT.md)。

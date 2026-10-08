# 意大利站近30天商品对应店铺税号自动采集

已新增独立程序 `run_seller_vat.py`，核心代码位于 `crawler/seller_vat.py`。复用项目安装环境，与新品榜独立保存数据；数据库为 `data/seller_vat.sqlite3`。

## 双击使用

1. 首次运行 `powershell -File env/setup.ps1` 安装依赖。Linux 使用 `python -m pip install -r env/requirements.txt`，Chromium 需再运行 `python -m playwright install chromium`。
2. 双击 `env/login_sellersprite.cmd`，在专用 Chrome 窗口登录卖家精灵。登录后回终端按 Enter，程序检查登录并关闭浏览器。
3. 双击 `env/run_seller_vat.cmd`。程序重置筛选，选择意大利站、统计周期最近30天、上架时间近30天，然后分页获取 ASIN 与 BuyBox 卖家链接。超过2,000条时自动按价格拆分查询，边界重叠并按ASIN去重。
4. 去重后访问卖家公开商业信息，提取公司名称、公司地址和所有公开税号。缺少卖家链接的ASIN尝试从当前Amazon商品页面补出BuyBox卖家。

程序结果位于 `outputs/seller-vat/YYYY-MM-DD/`：

- `店铺税号.csv`：一条税号一行，包含店铺链接；UTF-8 BOM，可用 Excel 打开。导入Excel时把税号列设为文本，避免丢失前导0。
- `商品来源.csv`：ASIN、对应卖家ID、商品链接、上架日期原值和采集来源。
- `manifest.json`：筛选口径、总数、完成状态、异常和缺失统计。
- `pages/`：每页原始提取记录，用于核查和恢复。

这里的“近30天上架”是**商品上架时间**，不是店铺注册时间。店铺范围为卖家精灵返回的BuyBox卖家，不等于每个ASIN全部跟卖商家。商品页回填反映采集时的BuyBox，并记录不同来源。

## 命令

首次小批测试（仅测试入口，结果明确标为部分完成）：

```powershell
.\env\.venv\Scripts\python.exe run_seller_vat.py run --max-pages 1 --limit 5
```

全部可访问结果（默认）：

```powershell
.\env\.venv\Scripts\python.exe run_seller_vat.py run
```

从已经入库的商品继续采集税号，跨日续跑需指定原日期：

```powershell
.\env\.venv\Scripts\python.exe run_seller_vat.py run --resume --run-id 2026-10-08
```

重新核查已完成店铺：增加 `--refresh`。仅导出本次记录：`run_seller_vat.py export --run-id 2026-10-08`。后台运行：增加 `--headless`。网页会员上限低于2000时可设置 `--partition-cap 1000`。Chrome不可用时用 `--channel msedge`。

## 完成与恢复

`complete` 只有在获取的唯一ASIN数等于网页原始结果数、没有无法解析的卖家和采集错误时才产生。验证码、登录失效、会员限制、翻页重复或界面更新会停止或标为部分结果，绝不把失败当作“无税号”。公开商业信息存在但没有VAT字段时状态为 `no_public_vat`。

价格拆分不能保证覆盖全部结果：同一价格集中超过上限、价格为空、隐藏结果、ASIN变体计数与网页总数不同、查询过程中数据变化等都会影响完整性。程序保留差额和警告；同价超限需要进一步按类目拆分。所有范围指卖家精灵当前账号可查询的数据。

重新查询同一天会重建当天商品集合，保留已完成店铺缓存；ASIN发现阶段中断后，可重新运行（会从首页重新查询，卖家不重复采集），或 `--resume` 先处理已保存ASIN。税号阶段支持断点续跑。避免同时运行多个进程；Linux 使用操作系统进程锁，进程退出后会自动释放。Windows 上异常断电后若残留锁文件，先确认没有采集进程再删除 `data/auto-collection.lock`。

## Linux 每小时自动采集

服务定义在 `deploy/chenyu-seller-vat.service`。安装后随系统启动，每小时最多启动一轮，使用独立 Chromium 登录资料；一轮超过一小时则等它结束后再开始下一轮。登录资料不存在或失效时保持等待，不会持续请求卖家精灵。状态写入 `data/seller-vat-scheduler.json`，并显示在管理网页“店铺税号”页。

```bash
sudo cp deploy/chenyu-seller-vat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chenyu-seller-vat.service
```

管理员先在主网站 `https://106.53.214.153:3000/dashboard?view=staff` 的“员工与权限”配置卖家精灵账号；密码只存于 Git 仓库外的私有配置文件，页面不会回显。然后在 HTTPS 税号管理页 `https://106.53.214.153:8100/seller-vat` 点击“使用已配置账号登录”。服务端读取最新私有配置，通过一次性进程管道交给无界面 Chromium 完成网页登录，不把密码写入数据库、命令行参数或日志；浏览器登录会话保存在采集服务使用的专用资料目录。登录成功后服务会在约 10 秒内开始首次采集，之后每小时自动运行；也可点击“立即启动采集”。按钮请求由现有服务执行，不会与登录或另一轮采集并发。卖家精灵要求额外验证码时，页面会提示登录未完成。

需要手工命令行登录时，可在有图形会话的服务器以同一个 `ubuntu` 用户运行：

```bash
.venv/bin/python run_seller_vat.py login --channel chromium --login-wait 600
```

可用 `systemctl status chenyu-seller-vat.service` 查看服务，`journalctl -u chenyu-seller-vat.service -n 100 --no-pager` 查看日志。停止自动采集使用 `sudo systemctl disable --now chenyu-seller-vat.service`。

每轮以开始时间创建独立批次，旧批次暂不自动删除；应监控 `data/seller_vat.sqlite3` 和 `outputs/seller-vat/` 的磁盘占用。明确受限、验证码或网站界面变化时，采集保留已有结果并记录原因，不尝试绕过验证。

## 登录与隐私

登录状态保存在本机 `data/sellersprite-profile/`，不读取或上传其他Chrome资料、不把密码写入结果。该目录含登录会话，不要分享。验证码由人工处理。税号只提取网站公开VAT/IVA字段，不把商业登记号、电话当税号；没有国家前缀时不根据意大利站推测税号国家。

## 可选 API

网页会员不是API授权。已有API密钥时，在本地环境变量 `SELLERSPRITE_API_KEY` 中配置后使用 `--source api`；程序不打印密钥。可用 `--partitions path.json` 传入筛选数组，但此模式不会自动证明全站分区覆盖，超过单分区2000条时标为部分结果。

官方参数依据：[选产品接口](https://open.sellersprite.com/api/2)、[站点与上架时间参数](https://open.sellersprite.com/appendix)、[网页筛选入口](https://cn.sellersprite.com/v3/)。浏览器使用 [Playwright专用持久化环境](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)。

## 验证

```powershell
.\env\.venv\Scripts\python.exe -m unittest discover -s tests -p test_seller_vat.py -v
```

离线测试验证意大利/西班牙页面样例、多税号、前导零、登记号排除、店铺去重及API参数。真实网页筛选和全量覆盖需登录后的试跑验证。

## Linux服务器

本模块在 Linux 上可通过 HTTPS 页面使用管理员已配置的账号密码登录，无需图形桌面。服务器上的公网 IP HTTPS 配置参考 `deploy/chenyu-crawler-https.nginx`，网页服务监听本地 `127.0.0.1:8101`；旧的 HTTP 8100 地址由 Nginx 自动跳转到 HTTPS。公网 IP 证书使用支持 `--ip-address` 的 Certbot 5.4+ 及 shortlived 配置申请，证书有效期短，必须安装 `deploy/chenyu-ip-cert-renew.service` 和 `deploy/chenyu-ip-cert-renew.timer` 定时续期。证书签发需要端口 80 可访问其 webroot 验证目录。

采集可通过独立命令或每小时后台服务运行；管理员可在“店铺税号”页面查看数据库记录并手动启动采集。不要在 GitHub Actions 运行带会员会话的全量采集。

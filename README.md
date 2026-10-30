# 观澜金融研究助手

面向银行与金融科技研究的本地 Agent 项目。打开看板即可筛选公司动态、查看银行财务趋势、检索带来源的答案，并导出 CSV。数据采集、图表和检索无需模型密钥；配置模型后可生成中文分析摘要，配置飞书应用后可在群中 @ 查询。

## 开始使用

本机已经安装依赖，并采集了真实公开资料。Mac 双击 **启动.command**，打开 <http://127.0.0.1:18765>。保持终端运行；按 Control-C 停止。本地启动默认每 6 小时检查来源，电脑休眠或进程关闭后不继续运行。若端口已被本应用占用，启动器会直接打开现有页面，不重复启动。

换一台电脑时，先安装 Python 3.11 或以上版本，再执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m finance_agent collect
python -m finance_agent serve
```

Windows 将激活命令替换为 `.venv\Scripts\activate`。也可双击 `启动.bat`（需先安装依赖）。首次下载依赖需要联网。

## 已实现的功能

- 公司与行业追踪：官方 HTML 公告页轻量采集、RSS/Atom、FDIC 公开财务接口；管理关注公司、别名和来源，按公司、行业、时间或文本筛选。
- 可靠采集：遵守 robots.txt，按主机限速；URL 去重、内容哈希、修订版本留存，单一来源失败不影响其他来源；可查看运行结果并重试。HTML 正文在同一天内复用缓存，下一天重新抓取。
- 研究助手：公司动态、行业政策及资产/存款/利润指标查询；每条结果附原文和披露时间。没有资料时说明不足，默认原文摘录，配置模型后增加中文整理。
- 可视化：银行财务趋势、事件分类分布、信息发布节奏；图表数据点可回查来源，新闻与指标均可导出 CSV。
- 飞书应用机器人：长连接、群 @ 查询、允许群清单、消息去重、持久化任务、失败退避重试；简报发送有明确命令，默认只预览。
- 使用记录：按实际请求记录最近 30 天查询量、访问身份、群数和成功推送数。网页以浏览器随机标识区分访问身份，不等同于经过核验的自然人数。

## 数据范围与口径

首批启用 7 个官方来源：中电金信公司动态，美联储、国际清算银行、欧洲央行订阅，以及摩根大通银行、美国银行、花旗银行的 FDIC 指标。中电金信页面为真实 HTML 爬虫链路，其余优先使用官方订阅或接口。

关注清单预置 9 家公司，用于匹配正文中的公司名称和别名；**新增关注不会自动获得该公司全部信息**，需要匹配已有来源或补充其官网来源。首期中国公司的独立公告来源是中电金信，财务时序覆盖三家美国银行，便于做国际同业观察。没有接入付费数据库，也没有宣称全网覆盖。

FDIC 数据是银行法人主体，不是上市控股集团。资产和存款是期末值；净利润是年初至今累计；ROA 为年初至今年化口径。接口的金额单位为千美元，图表换算为亿美元（除以 100000）。缺失值不替换为 0，不混合币种。利润曲线明确标识累计口径，不宜进行跨年环比解释。原始数据修订后再次采集更新指标，公告正文修订留存版本。

页面摘要来自已获取的正文或 RSS 描述；有些订阅只提供标题。AI 不能补造缺失正文，标题级证据只能支持标题范围内的结论。

## 接入 AI

支持 Chat Completions 兼容接口。使用自己的模型服务，或者本机提供兼容接口的模型；密钥仅通过环境传入，不写入代码或配置文件。不需要为运行基础功能购买模型服务。

```bash
export LLM_BASE_URL='https://你的模型服务/v1'
export LLM_MODEL='模型名称'
read -s LLM_API_KEY
export LLM_API_KEY
export LLM_DAILY_LIMIT=25
python -m finance_agent serve
```

在 `read -s` 后输入密钥并回车，终端不回显。本地模型可将地址设为 `http://127.0.0.1:端口/v1`，不需要密钥时省略 `LLM_API_KEY`。

每次调用只提交用户问题和最多 6 条公开资料，不上传简历或本地其他文件。每天最多 25 次模型调用，可调整上限。调用失败、预算用尽、引用编号错误、阿拉伯数值检查未通过时回退原文摘录。来源校验不能完全保证模型语义正确，关键业务结论需结合原文判断。具体费用取决于用户所选服务。

## 飞书群接入

采用官方 Python SDK 的长连接方式，**无需开放本机端口到公网**。

1. 在飞书开放平台创建自建应用，开启机器人能力。
2. 申请 `im:message.group_at_msg:readonly` 和 `im:message:send_as_bot` 权限。若需要私聊，再申请 `im:message.p2p_msg:readonly`。
3. 订阅 `im.message.receive_v1`，接收方式选择长连接；发布应用并加入指定群。
4. 在运行环境提供应用 ID、Secret 和允许的群 ID（`oc_...`）。私聊也需要把对应会话 ID 加入允许清单。不要提交到代码仓库。

```bash
export FEISHU_APP_ID='你的应用ID'
read -s FEISHU_APP_SECRET
export FEISHU_APP_SECRET
export FEISHU_ALLOWED_CHATS='oc_群1,oc_群2'
python -m finance_agent bot
```

在群里输入 `@机器人 中电金信最近30天动态` 或 `@机器人 摩根大通银行总资产`。不会读取全群聊天历史；群消息仅处理 @ 请求，机器人和其他应用发出的消息会被忽略。

事件先持久化入队，再由后台工作线程处理。以消息 ID 去重，并用稳定 UUID 请求飞书端去重。失败最多尝试 4 次，按退避时间重试，进程重启后可恢复未完成任务。成功任务清理消息内容，仅保留状态和匿名统计。

简报默认预览；发送时必须明确给出允许群 ID：

```bash
python -m finance_agent digest --days 7
python -m finance_agent digest --days 7 --send-to oc_目标群
```

同一天、同一群、同一时间范围的简报只成功发送一次。此项目交付时没有发送任何实际群消息；飞书真实联调需要她的应用凭据、授权和目标群。

## 采集与日常运行

```bash
# 只更新公司来源
python -m finance_agent collect --source gientech
# 更新所有启用来源
python -m finance_agent collect
# 持续运行，每 360 分钟采集一次
python -m finance_agent watch --minutes 360
# 看板运行期间自动每 360 分钟采集
COLLECT_INTERVAL_MINUTES=360 python -m finance_agent serve
# 命令行检索
python -m finance_agent ask '中电金信最近30天动态'
```

自动采集和简报发送是两个独立操作，采集不会自动发送群消息。不建议同时启动两个采集进程；数据库会去重，但会增加来源请求量。来源改变结构或网络异常时，在“关注与来源”中查看错误并重试或更新来源配置。

## 添加公司数据源

在“关注与来源”里先添加公司及别名，再添加官方 RSS 地址；HTML 来源需提供公告列表地址、链接特征与正文 CSS 选择器。例如中电金信采用 `/detail/` 匹配文章链接、`.m3nContent` 提取正文。每轮最多读取 12 篇 HTML 或 30 条 RSS，来源列表只覆盖其当前公开条目。

仅允许公开 HTTP/HTTPS 地址，禁止本机、内网、凭据嵌入 URL 和自定义端口。遇到跨站重定向需要将最终公开地址作为新来源配置。部分本地代理把域名映射到 `198.18.0.0/15`；程序仅对内置官方域名允许这种映射，其他内网和回环地址仍禁止。若自定义源因代理 DNS 被拒绝，应调整本机代理的 DNS 规则，不要关闭地址检查。

## 项目结构

```text
finance_agent/
  db.py          SQLite 存储、版本与使用记录、可靠任务队列
  collect.py     HTML / RSS / FDIC 采集及数值口径处理
  research.py    查询、引用、可选模型与调用预算
  feishu.py      飞书长连接机器人、简报发送
  app.py         本地 API 与运行边界
  static/        中文工作台、筛选、可视化、来源管理
tests/           核心规则与 API 验证
data/            本机数据（不提交代码仓库）
```

数据默认保存在 `data/research.sqlite`，可用 `FINANCE_DB` 修改。备份时先停止进程，再复制整个 `data` 目录。删除数据目录会清空资料与使用记录；重启会重建默认来源。本机工作目录已有一次真实采集结果；便携源码包不包含数据库或使用记录。换机后运行 collect 重新采集，实时性以页面披露日期和采集时间为准。

## 验证与演示

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用临时数据库，包含去重与修订、零值/缺失值、金额单位与累计期间、未知问题拒答、模型异常回退和调用额度、飞书群范围与事件队列、来源故障隔离、跨站请求与内网 URL 拦截、CSV 公式注入防护。真实网络采集与浏览器验收结果见 `验收记录.md`。

建议按这个顺序演示：更新资料 → 公司筛选 → 打开公告原文 → 查看银行财务图表与来源 → 问一个公司问题 → 问一个没有资料的问题 → 查看运行日志与使用记录。配置飞书后再演示群 @ 查询。

可据实描述为：“使用 AI 编程辅助完成公开金融信息采集、来源检索与可视化工具，支持飞书应用机器人接入，并记录实际使用情况。”本地原型完成不等于已经连续运营 7 天、覆盖真实受众或产生业务收益，这些指标需要实际试用后记录。

## 官方参考

- [飞书 Python SDK 与长连接示例](https://github.com/larksuite/oapi-sdk-python)
- [飞书事件订阅概述](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/event-subscriptions)
- [FDIC BankFind API](https://api.fdic.gov/banks/docs/)
- [FDIC 数据下载与说明](https://www.fdic.gov/bank-data-guide/data-downloads)
- [中电金信新闻](https://www.gientech.com/news.html)
- [美联储订阅源](https://www.federalreserve.gov/feeds/feeds.htm)
- [FastAPI 官方文档](https://fastapi.tiangolo.com/)

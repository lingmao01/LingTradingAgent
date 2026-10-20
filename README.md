# LingTradingAgent

个人自用的智能投研与交易辅助智能体：用自然语言完成行情研究、组合诊断、风险核查、回测与定时投研报告，并保证回答中的每个数字都能追溯到真实数据来源。

本项目在 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 基础上做个人化部署与可靠性加固，当前重点是把「ETF / 组合投顾」这条链路做到可长期信任：结论有据、数据可查、定时任务结果可回看。

> 免责声明：本项目仅用于研究与学习，输出不构成任何投资建议。据此操作，风险自负。

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 自然语言投研 | 对话式研究 A 股 / 港股 / 美股 / 加密资产，自动调用行情、基本面、资金流、新闻等工具 |
| 接地校验（Grounding） | 数值账本 + 证据链 + 发布前闸门，拒绝无来源、跨标的错配、与账本冲突的数字 |
| 组合与风险 | 持仓账本、风险规则、投资纪律检查、组合压力评估、ETF 持仓穿透 |
| 投顾工作流 | 投顾建议日志、预测登记与事后评估、独立评审、投前证据收集 |
| 定时研究 | 7 个内置研究剧本，按 cron 自动出报告，运行结局与结果会话可回看 |
| 研究与回测 | 策略回测、因子分析、期权定价与收益结构、策略发现 |
| 多端接入 | Web 前端、CLI、MCP Server（76 个工具，可接入 Claude Desktop / Cursor 等） |

内置定时研究剧本：

- `premarket-brief` 盘前简报（风险规则优先）
- `daily-risk-watch` 每日风险盯盘
- `portfolio-checkup` 组合体检
- `advice-review` 投顾建议复盘
- `a-share-money-flow` A 股资金流
- `earnings-season-tracker` 财报季跟踪
- `institutional-holdings-diff` 机构持仓变动

## 技术栈

- 后端：Python 3.11+、FastAPI、uv
- 前端：React 19、TypeScript、Vite、Tailwind CSS（Node.js >= 22.22）
- 数据：按标的类型分链路取数，A 股默认顺序 `tushare → tencent → mootdx → eastmoney → baostock → akshare → gildata → local`，可用 `MARKET_DATA_ORDER_A_SHARE` 覆盖
- 测试：pytest（`agent/tests`，700+ 测试文件）、Vitest（前端）

## 快速开始

### 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js 22.22+ 与 npm

### 一键启动

```bash
./start.sh          # 启动后端 + 前端，日志带 [backend] / [frontend] 前缀
./start.sh --open   # 启动完成后自动打开浏览器
```

macOS 下也可以直接双击 `start.command`。

启动后：

- 前端：http://127.0.0.1:5899
- 后端：http://127.0.0.1:8899

常用环境变量：`BACKEND_PORT`、`FRONTEND_PORT`、`VIBE_START_OPEN_BROWSER=1`、`PYPI_INDEX`（默认清华镜像）。

### 配置

```bash
cp agent/.env.example agent/.env
```

按需填写：

```dotenv
LANGCHAIN_PROVIDER=openrouter
LANGCHAIN_MODEL_NAME=deepseek/deepseek-v4-pro
OPENROUTER_API_KEY=sk-or-...
TUSHARE_TOKEN=your-tushare-token
```

`agent/.env` 已被 `.gitignore` 忽略，请勿提交真实密钥。

## MCP Server

```bash
uv run vibe-trading-mcp
```

在 Claude Desktop、Cursor 等 MCP 客户端中配置后即可使用研究、回测、行情、资金流等工具。

## 目录结构

```text
agent/                     后端服务
  src/agent/               智能体循环、上下文与 grounding 校验
  src/advisor/             投顾工作流（建议、预测、评审、证据）
  src/scheduled_research/  定时研究与剧本
  src/tools/               工具集（行情、基本面、期权、回测等）
  tests/                   后端测试
frontend/                  React 前端
docs/                      验收与设计文档
wiki/                      使用教程
scripts/                   辅助脚本
start.sh / start.command   一键启动
```

## 测试

```bash
uv run pytest agent/tests          # 后端
cd frontend && npm run test:run    # 前端
```

## 近期改动（2026-10）

- **定时任务结果可见**：任务新增运行结局（RunOutcome）字段，记录降级状态、运行摘要、运行时间与关联会话，前端卡片可直接跳转结果会话
- **grounding 闸门加固**：修复声明行尾部标注入导致整块内容被判畸形的问题；可用性声明改为就近归属，避免同一行其他标的被连坐误判；新增指标值按同字段核对，避免与价格区间比对导致误拒
- **数据口径统一**：收盘价统一取 `get_market_data` 的 `close`，均线只取技术指标工具，消除双源 as-of 冲突
- **投顾链路**：建议日志、持仓账本、风险规则、纪律检查、投顾评审等模块接入智能体循环

## 致谢与许可

- 上游项目：[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)，本仓库沿用其 MIT 许可，版权归 Vibe-Trading Contributors
- 第三方组件与公式来源见 [NOTICE](NOTICE) 及 `agent/src/factors/zoo/*/LICENSE.md`
- 上游原始说明文档保留在 [README_VIBETRADING.md](README_VIBETRADING.md)（含 README_zh.md 等多语言版本）

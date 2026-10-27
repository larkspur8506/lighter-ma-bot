# lighter-ma-bot

在 **Lighter（zkLighter）永续合约** 上跑 BTC / ETH / SOL 的 **4h 双均线密集突破**策略的
**纯自动交易系统**。

> **没有任何人工下单环节。** 没有确认票、没有签名链接、没有「待人工确认」状态。
> 信号由策略直接产生，执行器直接下单，交易所确认成交后直接关账。

---

## ⚠️ 证据基础（先读这一节）

本系统是旧系统 `larkspur8506/crypto-ma-alert`（Hyperliquid 现役）的策略口径移植。
但**执行场所变了，历史证据的覆盖范围也随之缩小**。用 Lighter 自己的 4h K 线、
复刻线上口径（含「密集门槛」）实算，得到：

| 标的 | 4h bar 数 | 折合年数 | 突破信号 | 过密集门槛（实际入场） | 笔/年 |
|---|---|---|---|---|---|
| BTC | 3,888 | 1.775 | 1,278 | 87 | 49.0 |
| ETH | 3,886 | 1.774 | 1,241 | 118 | 66.5 |
| SOL | 3,884 | 1.774 | 1,392 | 94 | 53.0 |
| 合计 | 11,658 | 1.775（按最长的 BTC 取） | 3,911 | 299 笔 | 168 |

口径说明：**笔/年 = 笔数 ÷ 该标的自己的 bar 数 ÷ 2,190**（4h bar/年 = 365×6）。
三个标的的历史长度并不相同（3,888 / 3,886 / 3,884 根），所以分母必须逐标的算，
不能统一写一个「1.77 年」——那会让反算对不上。合计行按最长分母 299 ÷ 1.775 = **168.4**
取整为 **168**；三格四舍五入后相加是 168.5，0.1 的差来自各自取整，不是口径差异。

旧回测（HL / Binance 4h 全历史）：**700 笔 / 6.10 年**（年化 115 笔，均 R +0.2442，
CAGR +39.9%，最大回撤 23.3%）。
（以上统计量来自旧系统 `crypto-ma-alert` 的回测，本仓库尚未复现 —— 见 T11。）

**结论，不要含糊：**

- 迁到 Lighter 后，历史证据从 **700 笔缩到 299 笔，覆盖率 43%**（299 / 700）。
- 这 299 笔是 **Lighter 自己的 K 线**产生的，不是旧回测的平移。
- Lighter 的 4h K 线总深度只有 **1.775 年**（BTC 3,888 根 / ETH 3,886 根 / SOL 3,884 根，
  `candles` 接口 500 根/次翻页换来），
  这是硬上限，换不到更长的历史。
- 所以：**旧回测只能证明「策略有效」，不能证明「在 Lighter 执行场所同样有效」。**
  两者不是同一个命题，别把 700 笔的统计显著性借给 299 笔。

因此新系统的回测必须用 Lighter K 线重跑（`scripts/backtest.py`，T11），
并且**把 Lighter（299 笔）与旧系统（700 笔）两个结果并列报告**，
不允许只报旧结果、也不允许把两者合并成一个数字。

---

## 策略口径来源

`app/strategy.py` 由 **T2 逐字复制**自旧仓库 `larkspur8506/crypto-ma-alert` 的 `app/strategy.py`
（528 行，**一字节不改**）。这是 700 笔回测 + 实盘验证过的口径，任何「优化」都视为
口径漂移，禁止在本仓库内改写。改造只发生在信号之后：sizing、下单、对账、风控。

三条硬边界（沿用旧系统已验证的设计）：

1. **信号与执行解耦** —— `strategy.py` 只产信号，`executor.py` 只把信号变成订单。
2. **只读与写路径分离** —— `info.py` 无凭据；`exchange.py` 是唯一持私钥、唯一能下单的模块。
3. **单一写者** —— `positions` / `orders` / `fills` 只由 `executor.py` 写。

---

## 技术栈

- Python 3.14
- [`lighter-sdk==1.1.8`](https://pypi.org/project/lighter-sdk/)（Apache-2.0，官方 SDK）
  - `lighter.SignerClient` — 交易签名
  - `lighter.ApiClient` + 各 `*Api` — 只读 REST
  - `lighter.create_api_key()` — 本地生成密钥对
  - **WASM 签名器是二进制依赖**：`lighter/signers/lighter-signer-linux-amd64.so`（约 11.8 MB，
    随 wheel 打包）→ 容器基础镜像必须 `linux/amd64`
- `fastapi==0.162.0` / `starlette==1.10.0` / `uvicorn==0.54.0`（显式钉版，见 `requirements.txt`；
  starlette 1.10 的 `TestClient` 走 `httpx2`，`httpx` 只作 <0.29 的兼容回落）
- `apscheduler`（4h bar tick 调度）
- SQLite（stdlib `sqlite3`）

## 目录结构

```
app/           策略、引擎、执行器、只读客户端、HTTP 服务（T1 只有 main.py 的 /health）
scripts/       回测 / 交叉验证 / 一次性密钥与账户设置（T2、T11 陆续落地，当前为空）
tests/         stdlib unittest（不引入 pytest）
docs/          当前只有 PLAN.md；DESIGN.md / RUNBOOK.md 由 T11 创建
Dockerfile     单容器部署，暴露 8100，非 root 运行，仅 linux/amd64
```

## 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

CI（GitHub Actions）跑同一套命令；本地机器资源有限，全量测试以 CI 为准。

配置走 `.env`（`.gitignore` 已排除，模板见 `.env.example`）：

```bash
cp .env.example .env && chmod 600 .env    # 填值；.env 永不入库
```

## 容器

```bash
docker build --platform linux/amd64 -t lighter-ma-bot:dev .
docker run --rm -p 8100:8100 --env-file .env lighter-ma-bot:dev
```

镜像只支持 **linux/amd64**（签名器二进制所限）。`/health` 由 `app/main.py` 提供 —— T1 起
容器默认 CMD（`uvicorn app.main:app`）就能起并返回 `{"ok": true, ...}`，CI 会真跑默认 CMD
并等 HEALTHCHECK 转 healthy；T10 用真面板替换同一个模块，`/health` 路径不变。

## 部署形态

独立域名 + 1Panel 反代到 `127.0.0.1:8100`。面板以只读监控为主，只有三个写操作
（`kill` / `flatten` / `dd reset`）需要 admin token。旧系统（HL）**并行运行，全程不动**。

## License

自有代码；依赖 `lighter-sdk` 为 Apache-2.0（作为依赖引用，不传染）。

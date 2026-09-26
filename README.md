# 生育待遇统一结算核心

面向灵活就业人员生育保险的服务端核心：以**唯一案件**归并医院重试、经办人补正与
异地乱序回执；按**有效期规则**分别结算基础服务包与并发症费用；住院分娩政策范围内
零自付，政策范围外项目一律个人负担；津贴审批与医疗费用结算关联但可独立推进；
资金指令只追加、只可冲正；规则追溯只生成差额、不改旧账；账户变更与高额人工调整
须双人授权。提供 HTTP API、确定性批量日结命令与逐案可解释账本。

金额一律使用**最小货币单位的非负整数**，全部计算为整数运算，结果确定。

## 设计原则

| 主题 | 机制 |
| --- | --- |
| 唯一案件 | 自然键 = `参保人 + 参保关系 + 就医地 + 分娩日期`（归一化大小写/空白），生成确定性 `CASE-xxxx` |
| 单一事实源 | 事件日志（只追加）；案件、费用、结算、账本都是事件的投影，可随时重建 |
| 重试/补正/乱序 | 同 `(claim_id,item_code)` 上传天然折叠；补正覆盖；回执按 `batch_seq` 取新，先到后到结果一致 |
| 规则选择 | 服务包按**参保地 + 分娩日期**取生效最高版本；就医地目录按**就医地 + 分娩日期**取版本 |
| 零自付边界 | 分类级定额只享一次，超额个人负担；目录外项目全额个人负担，医院自报分类无效 |
| 津贴 | 参保月数、灵活就业是否纳入、津贴天数均由服务包版本决定；审批为终局操作 |
| 资金安全 | 拨付为负向 `disbursement` 分录；只能追加指向原指令的 `reversal` 冲正链 |
| 规则追溯 | 新批次按现行规则全额重算入账，再冲回旧批次，净额即差额；旧账不变 |
| 双人授权 | 账户变更一律双人；人工调整达阈值（默认 50,000 元）双人；票据绑定载荷哈希、一次性 |
| 并发 | 全程 `BEGIN IMMEDIATE` 串行写事务 + 案件版本乐观锁；终局操作并发只一个成功 |
| 日结 | 案件号排序、逐案认领、确定性幂等键；崩溃重跑只恢复未完成案件，同日重跑为空操作 |
| 可解释 | 每条应付/差额/冲正都带 `结算批次 + 费用行 + 规则版本 + 冲正父链 + 授权票据` 指针 |

## 模块结构

```
src/benefits/
  contracts.py   稳定契约：费用分类、账本动作、金额约束
  identity.py    案件自然键
  models.py      不可变值对象（参保关系、费用行、逐行结算结果）
  policy.py      有效期规则包/就医地目录、版本选择、逐行结算、津贴资格与计发
  events.py      事件类型与确定性折叠（重试/补正/乱序回执归并）
  ledger.py      逐案追加式账本、冲正链、逐笔解释
  authz.py       双人授权票据与高额阈值策略
  store.py       SQLite：事件日志 + 同事务投影 + 幂等凭证 + 审计 + 日结检查点 + 重建
  service.py     用例编排（开案/上报/回执/结算/津贴/拨付/冲正/追溯/日结/解释）
  api.py         标准库 HTTP/JSON API（Idempotency-Key、X-Actor、expected_version）
  cli.py         serve / settle / retro / rebuild / explain / batch-status
```

无任何第三方运行时依赖，仅需 Python ≥ 3.11 与标准库 SQLite。

## 快速开始

```bash
# 运行全部自动化验证（72 个用例）
python3 -m unittest discover -s tests -v

python3 -m compileall -q src

# 启动 HTTP 服务
python3 -m src.benefits.cli serve --db data/benefits.sqlite --port 8080

# 批量日结（可安全重复执行）
python3 -m src.benefits.cli settle --db data/benefits.sqlite --date 2026-08-31

# 规则追溯重算（先发布新版本规则，再逐案生成差额）
python3 -m src.benefits.cli retro --db data/benefits.sqlite --case CASE-XXXX --reason 调标

# 从事件日志重建全部投影（进程恢复/一致性校验）
python3 -m src.benefits.cli rebuild --db data/benefits.sqlite

# 逐案可解释账本
python3 -m src.benefits.cli explain --db data/benefits.sqlite --case CASE-XXXX
```

## HTTP API 摘要

所有写接口接受 `Idempotency-Key`（重放首次响应；同键不同载荷返回 409），
经办人通过 `X-Actor` 标识，并发修改可携带 `expected_version`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/cases` | 按自然键开案（重复开案返回同一案件） |
| POST | `/v1/cases/{id}/reports` | 医院上传费用（重试折叠） |
| POST | `/v1/cases/{id}/corrections` | 经办人补正明细 |
| POST | `/v1/cases/{id}/withdrawals` | 撤回错误明细 |
| POST | `/v1/cases/{id}/remote-receipts` | 异地回执（可乱序、重复） |
| POST | `/v1/cases/{id}/settlements` | 医疗费用结算（一案一次，变更走追溯） |
| POST | `/v1/cases/{id}/allowance-decisions` | 津贴审批（终局，独立推进） |
| POST | `/v1/cases/{id}/payments` | 按待遇类型拨付当前正余额 |
| POST | `/v1/payments/{pid}/reversal` | 冲正已发出的资金指令 |
| POST | `/v1/cases/{id}/account-change/proposals` | 发起账户变更授权 |
| POST | `/v1/auth-tickets/{tid}/decision` | 第二人批准/驳回 |
| POST | `/v1/auth-tickets/{tid}/execute` | 执行已批准账户变更 |
| POST | `/v1/cases/{id}/adjustments/proposals` | 发起高额人工调整授权 |
| POST | `/v1/cases/{id}/adjustments` | 登记人工调整（高额须带授权票据） |
| POST | `/v1/cases/{id}/retro` | 规则追溯重算，生成差额 |
| GET  | `/v1/cases/{id}/explain` | 逐案事实、结算、账本、冲正链、事件链 |
| POST | `/v1/admin/enrollments` `/v1/admin/packages` `/v1/admin/catalogs` | 主数据/有效期规则 |
| GET  | `/v1/audit` | 审计记录 |

### 典型流程

```bash
# 1. 医院开案并上传（重试时务必复用同一 Idempotency-Key）
curl -X POST localhost:8080/v1/cases -H 'X-Actor: hosp' -H 'Idempotency-Key: o1' \
  -H 'Content-Type: application/json' \
  -d '{"person_id":"P1","enrollment_id":"E1","care_region":"B市","delivery_date":"2026-08-01",
       "account":{"account_name":"张某","bank_code":"ICBC","account_no":"6222"}}'

curl -X POST localhost:8080/v1/cases/CASE-XXXX/reports -H 'X-Actor: hosp' \
  -H 'Idempotency-Key: r1' -H 'Content-Type: application/json' \
  -d '{"claim_id":"CL1","lines":[
       {"item_code":"D001","category":"basic_delivery","amount":450000,"service_date":"2026-08-01"},
       {"item_code":"C001","category":"complication","amount":100000,"service_date":"2026-08-01"},
       {"item_code":"ZZZ","category":"basic_delivery","amount":7000,"service_date":"2026-08-01"}]}'

# 2. 异地回执（可在上传前或后到达，可乱序重发）
curl -X POST localhost:8080/v1/cases/CASE-XXXX/remote-receipts -H 'X-Actor: remote' \
  -H 'Content-Type: application/json' \
  -d '{"claim_id":"CL1","receipts":[{"item_code":"C001","accepted_amount":80000,"batch_seq":1}]}'

# 3. 结算：D001 定额 400000 + C001 核定 80000×80%=64000；ZZZ 目录外全自付
curl -X POST localhost:8080/v1/cases/CASE-XXXX/settlements -H 'X-Actor: agent'
# 4. 津贴可独立审批 / 拨付 / 冲正 / 追溯，再用 explain 回溯全链
```

## 规则版本数据格式

服务包（参保地）关键字段：`effective_from`（含）、`effective_to`（不含）、
`include_flexible`、`min_insured_months`、`allowance_days`、分类规则
（`zero_copay` 带 `cap`；`ratio` 带 `ratio_num/ratio_denom`；`none` 不支付）。
就医地目录为 `item_code -> 费用分类`。目录中不存在的项目一律判定为范围外。

发布新版本而不是修改旧版本：系统在分娩日期对应的生效版本上结算，
历史案件日后通过 `retro` 重算产生差额。

## 持久化与恢复

- SQLite（WAL）；事件表对 `(case_id,seq)` 与 `(case_id,idempotency_key)` 有唯一约束。
- 每个写用例在一个 `BEGIN IMMEDIATE` 事务内完成校验、追加事件、更新投影、写审计。
- `rebuild` 清空投影后按 `(case_id,seq)` 重放全部事件；投影确定性、幂等，
  重建前后的逐案账本逐字节一致（自动化验证覆盖）。
- 日结批次表记录 `claimed/done/failed`；崩溃后重跑只认领未完成与失败案件，
  各步骤幂等键重放，绝不重复拨付。

## 自动化验证覆盖

- 跨统筹区：参保地选服务包、就医地选目录，两地目录差异可见；
- 跨政策日期：生效边界、旧版本不覆盖灵活就业、版本选择；
- 重复回执/乱序：回执先于/后于上传、旧批次不覆盖新批次、重试折叠；
- 并发审批/结算/拨付：8 线程竞争，终局操作恰有一个成功，余额不重复；
- 冲正重算：冲正链完整、禁止二次冲正、冲正后可重新发起新指令；
- 规则追溯：旧批次原样保留，新批次 + 冲回分录净差额可回溯到费用与规则；
- 进程恢复：重开数据库事实一致、投影重建稳定、幂等凭证跨进程有效；
- 双人授权：自批拒绝、驳回不可执行、载荷篡改拒绝、票据一次性；
- 日结：确定性全量、同日空操作、崩溃续跑、失败案重试、追溯差额次日拨付。

运行：

```bash
python3 -m unittest discover -s tests -v
```

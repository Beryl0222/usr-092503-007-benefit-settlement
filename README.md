# 生育待遇统一结算核心

面向"灵活就业人员纳入生育保险 + 住院分娩政策范围内零自付 + 津贴直发个人 +
异地直接结算"的服务端核心。金额一律以**最小货币单位整数**表示，
所有关键业务事实追加保存、不可改写。

## 领域设计

- **唯一案件**：案件号由 `(参保人, 分娩日期)` 经 SHA-256 确定派生
  （`ids.derive_case_id`）。医院重试上传、经办人补正、异地回执乱序到达
  都归并到同一案件。
- **有效期规则**：服务包按**参保地**发布、带版本与生效区间，按**分娩日期**
  选定并永久保存；就医地**目录**决定项目类别与是否在政策范围内。
- **结算规则**（`engine.py`，纯函数确定性计算）：
  - 住院分娩基础包范围内零自付（可配比例/封顶）；
  - 镇痛按比例且类别封顶；并发症先扣起付线再按比例；产前检查限额；
  - **范围外项目（含目录未收录）全额个人负担，零自付不掩盖**；
  - 每行产出 `payable / personal / rule_ref`，任何应付与个人负担都可回溯到
    具体费用行与规则版本。
- **逐案账本**（`ledger.py`）：`payable / adjustment / disbursement /
  reversal` 四类追加分录，按 `medical / allowance` 两科目记带符号金额，
  运行余额可逐行解释。账本、结算版本、回执、审计表由数据库触发器禁止
  UPDATE/DELETE。
- **补正与追溯只产生差额**：补正以新单取代旧单并重结算；规则追溯生成新
  结算版本，账本上只追加 `adjustment` 差额，旧版本旧账原样保留。
- **资金指令与冲正链**（`payments.py`）：`pending → sent → acked`；
  指令一旦发出不可修改/作废，只能生成 `reversal` 指令，账本 reversal 分录
  以 `reversal_of` 指向原拨付分录。
- **津贴独立推进**（`allowance.py`）：独立状态机与科目，医疗未结也可审批；
  审批采用版本号乐观并发控制，并发审批仅一方成功，提交人不能自审。
- **双人授权**（`approvals.py`）：收款账户首登直接生效、变更必须四眼审批；
  人工调整达到服务包高额门槛时强制双人工单。
- **确定性批量日结**（`batch.py`）：`retro → finalize → disburse` 固定阶段；
  `batch_id` 由结账日派生，进度逐案持久化，崩溃后重跑只处理未完成部分，
  资金指令带确定性幂等键，报告按案件号排序可复算。

## HTTP API

- 医院端 `/api/v1/hospital/...`：费用上传、补正、案件查询；
- 经办端 `/api/v1/agency/...`：异地回执、津贴审批、账户与授权工单、
  人工调整、资金指令/冲正、追溯排期、日结、逐案 explain。
- 请求头：`X-Actor-Id`、`X-Actor-Role`（hospital/agency）；
  写接口支持 `Idempotency-Key`（同键同体重放返回首响应，同键异体返回 409）。

## 快速开始

```bash
pip install -e .   # src 布局；未安装时下列命令前加 PYTHONPATH=src
python3 -m benefits.cli --db benefits.db init-db
python3 -m benefits.cli --db benefits.db seed
python3 -m benefits.cli --db benefits.db serve --port 8080
python3 -m benefits.cli --db benefits.db daily --as-of 2026-09-26
python3 -m benefits.cli --db benefits.db explain --case C<derived>
```

作为库使用：

```python
from benefits.app import BenefitsApp
app = BenefitsApp.open("benefits.db")
res = app.ingest_bill(hospital_id="H-B01", upload_id="U1",
                      person_id="P-1001", delivery_date="2026-09-20",
                      lines=[...], actor="hosp-b01")
app.explain(res["case_id"])          # 逐案可解释账本
```

## 开发与检查

```bash
python3 -m unittest discover -s tests -v   # 49 项自动化验证
python3 -m compileall -q src
```

测试覆盖：跨统筹区就医、跨政策日期选包、医院重试去重、补正取代、
异地回执重复/乱序、范围外零自付不掩盖、追溯差额不改旧账、资金冲正链、
并发审批、双人授权、批量日结幂等与进程崩溃恢复、HTTP 端到端。

## 代码地图

| 文件 | 职责 |
| --- | --- |
| `contracts.py` | 费用类别、账本动作、状态枚举与金额契约 |
| `db.py` | SQLite schema、追加只读触发器、事务管理 |
| `ids.py` / `clock.py` | 案件号确定派生 / 可冻结时钟 |
| `policy.py` | 服务包版本、就医地目录、参保关系 |
| `engine.py` | 纯函数结算引擎与津贴计发 |
| `cases.py` | 案件聚合、上传/补正/回执归并、重结算 |
| `ledger.py` | 追加账本与逐行解释 |
| `payments.py` | 资金指令生命周期与冲正链 |
| `allowance.py` | 津贴状态机与乐观并发审批 |
| `approvals.py` | 双人授权工单与收款账户 |
| `retro.py` | 规则追溯作业排队与处理 |
| `batch.py` | 确定性批量日结与崩溃恢复 |
| `api.py` / `cli.py` | HTTP API / 命令行 |
| `seed.py` | 双统筹区演示数据 |

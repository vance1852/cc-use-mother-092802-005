# 对齐白酒渠道进销存与真实动销基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/channel_sellthrough/`：渠道动销对账服务（库存事件账本、期间关账、离线补传、可复算查询）；
- `tests/`：基础规则、事务边界、接口路由、对账规则与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
PYTHONPATH=src python3 -m channel_sellthrough.acceptance
```

两条验收命令都会在临时 SQLite 数据库中跑通各自业务的完整链路，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m channel_sellthrough.api --database sellthrough.sqlite3 --host 127.0.0.1 --port 8080
```

对账服务的路由会回退到基础服务路由，一个进程即可同时提供两套接口；基础服务也可以按原方式用 `beverage_ops_foundation.api` 单独启动。健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 渠道动销对账服务

针对"财务报表被前期压货、跨仓调拨和晚到退货干扰，月末库存无法证明产品真正到达消费者"的问题，把渠道流转全程记录为**带来源的库存事件日志**，并从日志重建数量与货权。事件日志是唯一事实来源：当前库存、期间快照和对账指标都由同一套折叠规则（`projection.py`）按落库顺序重放得到，任何结果都可复算。

### 库存事件与库存桶

| 事件 `kind` | 含义 | 库存桶变化 |
| --- | --- | --- |
| `outbound` | 企业出库（带发货单号 `ref_no`） | 在途 += 数量（货权：企业） |
| `receipt` | 经销商签收（可分批，`final=true` 终收） | 在途 → 可销售；终收短收部分核销并记差异 |
| `transfer` | 仓间调拨（`phase=shipped/received`） | 可销售 → 在途 → 目的库位可销售（货权不变） |
| `sale` | 终端售出 | 可销售 -= 数量 |
| `return` | 退货（`phase=shipped/received`） | 可销售 → 待退 → 企业接收后离账 |
| `stocktake` | 盘点（`quantity` 为实盘数） | 可销售按差额调整并记差异 |
| `dispute_freeze/release` | 争议冻结/解除（系统事件） | 可销售 ↔ 争议 |

差异来源（`st_variances`）：`in_transit_shortage`、`transfer_shortage`、`return_shortage`、`stocktake_surplus`、`stocktake_shortage`，都挂在触发事件上，可按渠道与期间汇总。

### 期间关账与迟到凭证

- 关账按 `渠道 × 期间（YYYY-MM）` 进行，要求更早有事件的期间已关账；关账时把期初、期间出入、期末按 `产品 × 批次 × 库存桶 × 货权` 落成快照并记录快照哈希。
- 关账后到达的凭证**只能追加到其后的首个开放期间**（`is_adjustment=true`，保留原业务日期与原期间），绝不回写已关期间的记录；已关期间用当前事件日志复算的结果与快照哈希比对（`verified`）。
- 过账期间随落库顺序单调不减，保证"按期间切分重建"与"按落库顺序折叠"永远一致。

### 离线补传

`POST /backfill` 按来源逐条处理：同一 `(source_id, source_seq)` 同内容视为**重放**（返回原事件），同序号不同内容视为**序列分叉**（拒绝、记异常、写审计），序号跳号记 `gap`、迟到的补号记 `late_fill`。单项失败不影响批次内其他项，响应给出逐项状态与汇总。

### 争议冻结

争议按 `渠道 × 产品 × 批次` 冻结：冻结期间该批次的所有库存动作被拒绝，其他批次与其他渠道不受影响；争议**不阻断关账**，争议数量在快照中单独列示。解除后库存回到可销售。

### 查询接口

- `GET /inventory?channel_id=`：可销售、在途、待退、争议四个库存桶 + 货权拆分 + 库位明细；
- `GET /metrics/sell-through?channel_id=&period=`：sell-in（签收量）、sell-out（终端售出）、净 sell-in、库存天数（含窗口、窗口内销量、日均、期末可销售等全部入参）、差异来源汇总、调整凭证计数；
- `GET /periods/snapshot?channel_id=&period=`：关账快照与复算校验；
- `GET /variances`、`GET /inventory-events`、`GET /disputes`、`GET /source-anomalies`、`GET /periods`：差异、事件、争议、来源异常与关账记录。

写入接口：`POST /products`、`/batches`、`/channels`、`/locations`、`/inventory-events`、`/backfill`、`/disputes`、`/disputes/resolve`、`/periods/close`。所有写入都走请求级幂等（`request_id`）并追加到基础服务的哈希审计链。

### 角色分工

- `admin` / `operator`：主数据登记与库存事件写入；
- `reviewer`：盘点事件、争议登记/解除、期间关账；
- `auditor`：只读查询。

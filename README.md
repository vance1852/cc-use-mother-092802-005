# 对齐白酒渠道进销存与真实动销基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - 基础模块：`service.py` / `storage.py` / `audit.py`；
  - 渠道动销对账：`channel_service.py`（入账/冻结/关账/查询）、`channel_ledger.py`（事件重建）、
    `channel_domain.py`（事件与库存状态口径）、`channel_models.py`；
- `tests/`：基础规则、事务边界、接口路由、渠道重建和端到端验收测试。

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
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 渠道动销对账

在基础模块之上新增 `ChannelReconciliationService`，把渠道库存做成**只增不改的事件台账**，
数量与货权都由事件重建，任何余额都可由同一批事件复算。

- **带来源的库存事件**：`ship_out`（企业出库，在途/企业货权）、`receipt`（经销商签收，确认
  sell-in 并转移货权）、`transfer`（仓间调拨，经在途中转、货权不变，`payload.phase=dispatch|arrive`）、
  `terminal_sale`（终端售出，sell-out）、`return`/`return_receipt`（退货，待退池）、
  `stocktake`（盘点差异，按账面与实盘自动算 `variance`）、`dispute_open`/`dispute_resolve`。
  每条事件必须带 `source_type` 与 `source_ref` 凭证。
- **离线补传**：来源流通过 `stream_key`+`stream_seq` 去重。同序号同内容是重放（返回原回执，不重复入账）；
  同序号不同内容判定为**序列分叉**，写入 `stream_anomalies` 隔离登记并返回 `409 sequence_fork`，分叉内容绝不入账。
- **关账不可逆**：期间按渠道关账（`periods/close`），固化事件列表与余额的 SHA-256 快照。
  关账后到达的凭证不会覆盖原期间，而是标记 `is_adjustment=true` 追加到下一个开放期间，
  原期间报表数字与快照保持不变。
- **查询口径**：库存按可销售（sellable）、在途（in_transit）、待退（pending_return）、争议（disputed）
  和货权方（enterprise/distributor）分列；对账报表给出 sell-in、sell-out（含退货净值）、库存天数
  （可销售 ÷ 期间日均净售出）以及每条差异的来源凭证。库存被透支、盘亏、核销都会记录带来源的差异。
- **争议冻结粒度**：争议只冻结相关产品+批次+渠道（可指定冻结来源池与货权），未决争议阻止该渠道关账，
  其他渠道与批次不受影响、照常关账。`resolve` 支持 `release`（转回原池）与 `writeoff`（核销并记差异）。

### 渠道接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /channel/products`、`POST /channel/channels` | 登记产品与经销商渠道 |
| `POST /channel/events` | 登记一条带来源凭证的库存事件（支持 `stream_key/stream_seq` 离线补传） |
| `POST /channel/disputes/open`、`/channel/disputes/resolve` | 开启/解除批次争议 |
| `POST /channel/periods/close` | 按渠道关账并固化快照 |
| `GET /channel/events?channel_id=&period_id=&product_id=&batch_no=` | 查询事件（含入账期间与来源） |
| `GET /channel/inventory?channel_id=&period_id=` | 重建分状态/分货权库存 |
| `GET /channel/reconciliation?channel_id=&period_id=` | sell-in/sell-out/库存天数/差异可复算报表 |
| `GET /channel/anomalies?stream_key=` | 查询序列分叉隔离记录 |

### 渠道离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.channel_acceptance
```

覆盖出库→签收→售出→盘点、重放与序列分叉隔离、关账后迟到凭证追加到后续期间、
争议只冻结相关批次且不影响其他渠道关账，以及审计链与关账快照校验，成功时输出一行
`status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

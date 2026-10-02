# 微孔板样本谱系追踪 API

基于 **Python · FastAPI · SQLite** 的微孔板谱系台账服务：

- 登记孔位中的初始样本与体积；
- 一批移液操作**按提交顺序在单个事务中原子执行**：每次吸取按来源孔的组成占比带走各初始样本，目标孔按现有液体与新增液体重新混合；**任一步体积不足，整批回滚**；
- 孔位可设**可选最大容量**（默认不限，既有数据自动视为不限）：补液与批次移液在**每一步**按目标孔当时体积校验上限，超限即 409（批次整批回滚）；撤销前同样核对快照恢复后的各孔体积，任一孔超过当前上限即整次拒绝；容量设置/取消与补液、批次、撤销串行；
- 按孔查询当前体积、各初始样本占比，以及涉及该孔的完整批次记录；
- **误操作批次撤销**：成功提交的批次在同一事务保存其首次触及各孔的批前快照，可原子恢复；仅当批次已提交、尚未撤销且之后没有补液或后续批次触及这些孔时允许撤销，原批次保留并追加审计记录；
- **孔位异常隔离**：操作员提交一组已登记孔、案件号与原因，服务在同一事务确认这些孔尚未隔离后原子建立隔离记录（任一孔已被隔离即整次拒绝，不留部分状态）；隔离中的孔禁止补液、作为批次来源/目标、预留体积、修改容量或撤销触及它的批次；解除隔离须携带对应案件号，批量解除任一不匹配即整体拒绝；申请与解除审计永久保留，孔位及相关批次查询返回当前隔离信息；孔上存在生效预留时隔离申请整次拒绝并逐孔列出阻塞预留；
- **移液体积预留**：操作员用 `request_key` 为未来批次一次预留多个已登记来源孔的 `Decimal` 体积；服务在同一事务扣除各孔其他生效预留后检查可用量，任一孔可用不足、孔已隔离或未登记即整次失败、不留部分预留；同键同目标（逐孔体积集合）重放、同键换目标 `409`。普通补液和未绑定预留的批次不能动用被预留体积（来源孔可吸取量 = 当前体积 − 生效预留量）；提交批次可携带 `reservation_id`，只有逐孔逐量完全兑现（预留的每个来源孔吸取量之和严格等于预留体积）才在同一事务执行移液并把预留标记为 `consumed`，任何失败（兑现不符、体积不足、容量超限）都整批回滚、不消耗预留。未消费预留可用独立 `request_key` 整体取消（`cancelled`），已消费预留不可取消；取消/兑现/建立与隔离申请在同一把写锁上串行。查询返回预留明细与状态；
- 所有体积运算使用 `Decimal`（统一量化到小数点后 9 位），不使用 float；预留体积同样以 TEXT 存 Decimal 字符串、按 Decimal 比较；
- 通过 `request_key` 幂等去重：相同键重复提交不会重复执行，直接返回首个结果（失败批次同样被记录并消耗该键）；批次撤销同样以 `request_key` 幂等：同键同目标返回首次结果，同键换目标返回 `409`。

## 快速启动（Docker，唯一需要的命令）

```bash
docker compose up --build
```

启动后在本机访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger 交互式文档（可直接试调） |
| http://localhost:8000/redoc | ReDoc 文档 |

SQLite 数据落在命名卷 `lineage-data` 中，容器重启不丢失；`docker compose down -v` 可清空。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 同样监听本机 8000 端口
pytest                                       # 运行测试
```

数据库文件默认在 `./data/lineage.db`，可用环境变量 `LINEAGE_DB` 指定其他路径。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/wells` | 登记孔位初始样本与体积（同孔重复登记视为补液混匀） |
| `PUT` | `/wells/{well}/capacity` | 设置或取消孔位最大容量（`null` 为不限；新上限低于当前体积返回 409） |
| `GET` | `/wells/{well}` | 孔位当前体积 + 容量上限 + 各初始样本的量与占比 |
| `GET` | `/wells/{well}/batches` | 涉及该孔的完整批次记录 |
| `POST` | `/batches` | 原子执行一批移液（`request_key` 幂等；可选 `reservation_id` 绑定预留兑现） |
| `GET` | `/batches` / `/batches/{id}` | 全部 / 单个批次记录（含失败批次与绑定的预留编号） |
| `POST` | `/batches/{id}/reversals` | 撤销已提交批次：原子恢复批前快照（`request_key` 幂等） |
| `POST` | `/reservations` | 一次预留多个已登记来源孔的 Decimal 体积（`request_key` 幂等） |
| `GET` | `/reservations` / `/reservations/{id}` | 全部 / 单个预留（明细 + active/consumed/cancelled 状态） |
| `POST` | `/reservations/{id}/cancellations` | 整体取消生效预留，已消费不可取消（独立 `request_key` 幂等） |
| `GET` | `/reservations/cancellations/all` | 全部预留取消审计记录 |
| `POST` | `/quarantines` | 孔位隔离申请：一组已登记孔 + 案件号 + 原因（`request_key` 幂等） |
| `POST` | `/quarantines/releases` | 解除隔离：须携带对应 `case_id`，批量解除任一不匹配整体拒绝（`request_key` 幂等） |
| `GET` | `/quarantines` / `/quarantines/releases` | 全部隔离申请（含各孔状态）/ 解除审计记录 |

孔位标签为「字母 + 数字」（如 `A1`、`B12`），不区分大小写；本服务按单块板建模。

## 调用示例

```bash
# 1. 登记初始样本：A1 加入 100 µL 样本 S1，A2 加入 60 µL 样本 S2
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A1", "sample": "S1", "volume": "100"}'
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A2", "sample": "S2", "volume": "60"}'

# 2. 原子批量移液：A1 -> B1 40，A2 -> B1 30（B1 混合为 S1:S2 = 4:3）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-001",
        "operations": [
          {"source": "A1", "destination": "B1", "volume": "40"},
          {"source": "A2", "destination": "B1", "volume": "30"}
        ]
      }'

# 3. 查询 B1 的体积与谱系占比
curl -s http://localhost:8000/wells/B1
# => {"well":"B1","volume":"70","composition":[
#      {"sample":"S1","amount":"40","fraction":"0.571428571"},
#      {"sample":"S2","amount":"30","fraction":"0.428571429"}]}

# 4. 体积不足 -> 整批回滚，返回 409，状态不变
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-002",
        "operations": [
          {"source": "B1", "destination": "C1", "volume": "50"},
          {"source": "B1", "destination": "D1", "volume": "50"}
        ]
      }'
# => 409 {"detail":{"message":"孔位 B1 体积不足：可用 20，请求吸取 50", ...}}

# 5. 重复提交同一 request_key -> 200 且 replayed=true，不会重复执行
# 6. 查询涉及 B1 的全部批次
curl -s http://localhost:8000/wells/B1/batches

# 7. 撤销误操作批次（假设批次 id 为 1）：原子恢复批前快照，原批次保留
curl -s -X POST http://localhost:8000/batches/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "undo-run-2026-09-22-001"}'
# 之后再有补液或后续批次触及该批孔位 -> 409 且 blocking_wells 列出后续操作
```

### 容量上限与冲突

```bash
# 8. 给 B1（当前 70 µL）设置上限 100；未设置过的孔位 max_capacity 为 null（不限）
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"max_capacity": "100"}'
# => {"well":"B1","volume":"70","max_capacity":"100","composition":[...]}

# 9. 补液超限 -> 409，返回超限孔位/当时体积/拟加入量/上限，液体不变
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "B1", "sample": "S1", "volume": "50"}'
# => 409 {"detail":{"reason":"capacity_exceeded","well":"B1",
#         "volume":"70","adding":"50","max_capacity":"100", ...}}

# 10. 批次中某一步使目标孔超限 -> 整批回滚，同样返回上述四元组（失败批次落库并消耗 request_key）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-30-001",
        "operations": [
          {"source": "A1", "destination": "B1", "volume": "40"}
        ]
      }'
# => 409 {"detail":{"reason":"capacity_exceeded","well":"B1",
#         "volume":"70","adding":"40","max_capacity":"100","batch_id":...}}
# 同一批次「先移出再移入」按每步当时体积校验：先 B1->C1 移出 40，再 A1->B1 移入 40 即可通过

# 11. 把上限调到当前体积（70）以下 -> 409 capacity_below_volume，配置不变
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"max_capacity": "10"}'
# => 409 {"detail":{"reason":"capacity_below_volume","well":"B1",
#         "volume":"70","max_capacity":"10", ...}}

# 12. 取消上限（恢复不限）
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"max_capacity": null}'

# 13. 撤销也核对容量：若某快照孔恢复后的体积超过其当前上限
#     -> 409 capacity_exceeded，exceeding_wells 逐孔列出恢复后体积与上限，
#        液体与审计记录不变；调大/取消上限后同 request_key 可重试
```

### 孔位隔离与冲突

```bash
# 14. 隔离申请：A1、A2 涉及案件 CASE-2026-101（同一事务确认两孔均未隔离后原子建立）
curl -s -X POST http://localhost:8000/quarantines \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "quar-2026-10-01-001",
        "case_id": "CASE-2026-101",
        "reason": "疑似交叉污染，等待复检",
        "wells": ["A1", "A2"]
      }'
# => 201 {"quarantine_id":1,"case_id":"CASE-2026-101",...,
#         "wells":[{"well":"A1","status":"quarantined",...},...]}
# 孔位与相关批次查询返回当前隔离信息：
curl -s http://localhost:8000/wells/A1
# => {"well":"A1",...,"quarantine":{"case_id":"CASE-2026-101",
#      "reason":"疑似交叉污染，等待复检","request_key":"quar-2026-10-01-001",...}}

# 15. 隔离中的孔禁止补液 -> 409 well_quarantined，液体不变
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A1", "sample": "S1", "volume": "10"}'
# => 409 {"detail":{"reason":"well_quarantined",
#         "quarantined_wells":[{"well":"A1","case_id":"CASE-2026-101"}], ...}}
# 作为批次来源/目标、修改容量、撤销触及它的批次同理（批次冲突不落失败批次、
# 不消耗 request_key，解除隔离后同键可重试）

# 16. 任一孔已被隔离 -> 整次申请拒绝（409 already_quarantined），不留部分状态
curl -s -X POST http://localhost:8000/quarantines \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "quar-2026-10-01-002",
        "case_id": "CASE-2026-102",
        "reason": "另一案件",
        "wells": ["A1", "B1"]
      }'
# => 409 {"detail":{"reason":"already_quarantined",
#         "conflicting_wells":[{"well":"A1","case_id":"CASE-2026-101"}], ...}}
# B1 不会被隔离，quar-2026-10-01-002 键未消耗，修正后可重试

# 17. 同键同目标重放 -> 200 且 replayed=true；同键换目标（换孔位或换案件）
#     -> 409 request_key_conflict

# 18. 解除隔离：须携带对应 case_id；任一孔未隔离或案件不符
#     -> 409 release_mismatch，整次解除拒绝
curl -s -X POST http://localhost:8000/quarantines/releases \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "rel-2026-10-01-001", "case_id": "CASE-2026-102", "wells": ["A1"]}'
# => 409 {"detail":{"reason":"release_mismatch","mismatched_wells":[
#         {"well":"A1","reason":"case_mismatch","case_id":"CASE-2026-101"}], ...}}

# 19. 正确的解除：A1、A2 同时解除，孔位恢复可用；申请与解除审计均保留
curl -s -X POST http://localhost:8000/quarantines/releases \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "rel-2026-10-01-002", "case_id": "CASE-2026-101", "wells": ["A1", "A2"]}'
# => 201 {"release_id":1,"wells":[{"well":"A1","quarantine_id":1},...]}
curl -s http://localhost:8000/quarantines           # 申请记录保留，各孔标记 released
curl -s http://localhost:8000/quarantines/releases  # 解除审计记录
```

### 移液体积预留与兑现

```bash
# 20. 为未来批次一次预留多个来源孔的 Decimal 体积：
#     A1 预留 40、A2 预留 30（同一事务扣除其他生效预留后校验可用量）
curl -s -X POST http://localhost:8000/reservations \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "rv-2026-10-02-001",
        "items": [
          {"well": "A1", "volume": "40"},
          {"well": "A2", "volume": "30"}
        ]
      }'
# => 201 {"reservation_id":1,"request_key":"rv-2026-10-02-001",
#         "status":"active","items":[{"well":"A1","volume":"40"},{"well":"A2","volume":"30"}],...}

# 21. 孔位查询返回预留占用量、可动用体积与逐预留明细
curl -s http://localhost:8000/wells/A1
# => {"well":"A1","volume":"100",...,"reserved_volume":"40",
#     "available_volume":"60",
#     "reservations":[{"reservation_id":1,"request_key":"rv-2026-10-02-001",
#                      "volume":"40","reserved_at":"..."}]}

# 22. 未绑定预留的普通批次不能动用被预留体积：
#     A1 当前 100、被预留 40，请求吸取 70 -> 409（可用按 60 计）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{"request_key":"b-free","operations":[
        {"source":"A1","destination":"B1","volume":"70"}]}'
# => 409 {"detail":{"reason":...,"well":"A1","available":"60","requested":"70",
#                   "batch_id":...}}   # 失败批次落库并消耗批次 request_key，预留不受影响

# 23. 可用量不足的预留整次失败，不留部分预留（A2 也不会被预留）
curl -s -X POST http://localhost:8000/reservations \
  -H 'Content-Type: application/json' \
  -d '{"request_key":"rv-too-big",
       "items":[{"well":"A1","volume":"70"},{"well":"A2","volume":"5"}]}'
# => 409 {"detail":{"reason":"reservation_unavailable","conflicting_wells":[
#         {"well":"A1","available":"60","already_reserved":"40","requested":"70"}]}}
# 同键同目标重放 -> 200 replayed=true；同键换孔/换量 -> 409 request_key_conflict

# 24. 隔离申请与预留串行：A1 上有生效预留 -> 409 并逐孔列出阻塞预留
curl -s -X POST http://localhost:8000/quarantines \
  -H 'Content-Type: application/json' \
  -d '{"request_key":"q-blocked","case_id":"CASE-9","reason":"x","wells":["A1"]}'
# => 409 {"detail":{"reason":"reservations_active","blocking_wells":[
#         {"well":"A1","reserved_volume":"40",
#          "reservations":[{"reservation_id":1,"request_key":"rv-2026-10-02-001",
#                           "volume":"40"}]}]}}

# 25. 绑定预留提交批次：逐孔逐量完全兑现（A1 可拆多步，合计量须恰好 40；
#     A2 须恰好 30）。同一事务执行移液并把预留标记为 consumed
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "b-fulfill-1",
        "reservation_id": 1,
        "operations": [
          {"source": "A1", "destination": "C1", "volume": "10"},
          {"source": "A2", "destination": "C1", "volume": "30"},
          {"source": "A1", "destination": "C2", "volume": "30"}
        ]
      }'
# => 201 {"batch_id":...,"reservation_id":1,...}
curl -s http://localhost:8000/reservations/1
# => {"reservation_id":1,"status":"consumed","consumed_batch_id":...,"consumed_at":"...",...}

# 26. 未逐孔逐量兑现 -> 409 reservation_fulfillment_mismatch，整批回滚、
#     预留保持 active、不落失败批次、不消耗 request_key
#     （少取/多取/漏掉某个预留孔/动用了未预留的孔都会逐孔列出 reserved vs requested）

# 27. 未消费预留用独立 request_key 整体取消；已消费不可取消
curl -s -X POST http://localhost:8000/reservations \
  -H 'Content-Type: application/json' \
  -d '{"request_key":"rv-2026-10-02-002","items":[{"well":"A2","volume":"5"}]}'
curl -s -X POST http://localhost:8000/reservations/2/cancellations \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "cancel-rv-2026-10-02-002"}'
# => 201 {"cancellation_id":1,"reservation_id":2,...}
# 已消费的预留 1 再取消 -> 409 already_consumed；重复取消 -> 409 already_cancelled；
# 取消键同键同预留重放 -> 200 replayed=true，同键换预留 -> 409 request_key_conflict
curl -s http://localhost:8000/reservations                    # 全部预留（含状态与明细）
curl -s http://localhost:8000/reservations/cancellations/all  # 取消审计
```

## 设计要点

- **Decimal 精度**：体积与组分量在库中以 TEXT 存 Decimal 字符串，运算全部走 `decimal.Decimal`，结果统一量化到 9 位小数（`ROUND_HALF_UP`），接口返回值中的数字均为字符串以避免 JSON 精度损失。
- **按比例吸取（整数单位最大余量法）**：从孔中吸取体积 `v`（孔总量 `V`，均以 9 位小数的最小计量单位 QUANTUM 计）时，每个初始样本先分得比例份额 `amount × v/V` 的整数单位，剩余单位按小数余量从大到小分配；余量相同按样本 ID 升序。任何样本分得的单位数不超过其原有量，且各样本带走量之和恰好等于 `v`——保证每一步之后来源孔与目标孔都满足「组分量之和 = 体积」；目标孔将各组分量累加，即视为立即完美混合。
- **原子性**：一个批次的所有操作在单个 SQLite 事务中按序执行，任一步 `InsufficientVolume` 或 `CapacityExceeded` 即回滚；失败批次以 `status="failed"` 落库（操作明细随回滚清空），方便审计。
- **容量上限**：`wells.max_capacity` 以 TEXT 存 Decimal 字符串，`NULL` 表示不限——老库启动时经 `ALTER TABLE` 就地补列，既有数据一律为 `NULL`，行为完全保留。`PUT /wells/{well}/capacity` 设置（正数）或取消（`null`）上限；新上限低于当前体积返回 `409 capacity_below_volume` 且配置不变。补液与批次移液在**每一步**按目标孔当时体积校验上限：同一批次内「先移出再移入」因此可行，任一步超限整批回滚，409 载荷含超限孔位、当时体积、拟加入量与上限（批次场景失败批次同样落库并消耗 `request_key`）。撤销在恢复快照前核对各快照孔恢复后的体积，任一孔超过其**当前**上限即整次拒绝（`409 capacity_exceeded`，`exceeding_wells` 逐孔列出恢复后体积与上限），液体与审计记录不变；容量是孔位配置而非快照内容，恢复快照不改变上限。
- **并发串行（`BEGIN IMMEDIATE`）**：补液、批次提交、撤销、容量变更、隔离申请与解除、预留建立/兑现/取消等变更端点从事务起点（任何校验读之前）即取 SQLite 保留写锁，使「校验 → 写入 → 提交」全程互斥：后到的变更在取锁处等待，先到者提交后才读库，因此必然看到已提交的最新状态（先提交者决定后续校验看到的状态）。这消除了撤销的丢失更新——撤销完成阻塞检查后、恢复快照前若有补液先提交，撤销在锁内重检到该补液事件并返回 `409 blocked`（列出阻塞孔位与补液操作），不会以旧快照覆盖；反之撤销先提交时，补液在锁上等待，随后叠加在恢复后的状态上（含撤销中被删除、随后补液重建的孔）。两种顺序均不丢失组分。容量变更同理：补液先提交则容量校验看到补液后的体积，容量先提交则补液/批次/撤销看到新上限。隔离亦同理：隔离先提交则批次/补液/预留在锁后读到隔离状态并返回 `409 well_quarantined`，批次/预留先提交则隔离在锁后正常建立或因生效预留返回 `409 reservations_active`。预留可用量因此基于锁内最新液体与预留状态计算，并发预留不会超扣。
- **幂等**：`batches.request_key` 有唯一约束。重复提交返回首个批次记录（`replayed=true`，HTTP 200）；并发下同键请求先在锁内复核、再由唯一约束兜底，不会执行两次。
- **批次撤销**：每个成功提交的批次在**同一事务**内对其首次触及的每个孔（来源与目标去重）保存批前快照——孔体积与各初始样本量，孔不存在则记 `NULL`。撤销时（`POST /batches/{id}/reversals`，请求体携带独立的 `request_key`）：
  - 仅当批次 `committed`、尚未撤销、存在快照，该批之后没有补液或已提交批次触及任一快照孔，且快照恢复后的各孔体积不超过其当前容量上限时才允许；条件不符返回 `409`，`reason` 取 `not_committed` / `already_reversed` / `no_snapshot` / `blocked` / `capacity_exceeded` / `request_key_conflict`；`blocked` 时在 `blocking_wells` 中逐孔列出后续操作（`top_up` 含样本与体积，`batch` 含后续批次 id 与 request_key），`capacity_exceeded` 时在 `exceeding_wells` 中逐孔列出恢复后体积与当前上限，**液体状态保持不变**；
  - 撤销在单事务内把所有快照孔恢复到批前状态（批次中新建的孔整体删除，先删组成再删孔以满足外键），并向 `reversals` 追加一条关联原批次的审计记录；**原批次与其操作明细不删除**，批次记录新增 `reversed` / `reversed_at` 字段；
  - 撤销 `request_key` 幂等：同键同目标返回首次撤销结果（`replayed=true`，HTTP 200），同键换目标返回 `409`；
  - 上线前已提交、没有快照的批次明确返回 `409 no_snapshot`，不可撤销。
  - 「后续操作」的判定基于孔位事件表 `well_events`（补液与批次提交各记一条，自增 id 即发生顺序）：以**本批次在该孔上的事件 id** 为基准，只统计之后的事件。
- **孔位隔离**：`quarantine_requests`（申请，`request_key` 唯一）+ `quarantine_wells`（每孔一行，`released_at IS NULL` 上的部分唯一索引保证同一孔同一时刻只有一条生效隔离）+ `quarantine_releases` / `quarantine_release_wells`（解除审计，记录每个孔解除的是哪次申请）。申请在同一事务内先确认所有孔已登记且尚未隔离再原子写入：任一孔未登记（`404`）或已隔离（`409 already_quarantined`，`conflicting_wells` 逐孔列出当前案件）即整体回滚，不留部分状态、不消耗 `request_key`。孔上存在生效预留时申请同样整次回滚（`409 reservations_active`，`blocking_wells` 逐孔列出预留总量与各条预留编号/键/体积），避免隔离后兑现批次无法执行。隔离中的孔：补液、批次（来源或目标）、预留体积、容量变更、撤销触及它的批次一律 `409 well_quarantined`（逐孔列出案件号），**液体、批次与审计均不变**——批次冲突不落失败批次、不消耗 `request_key`，解除隔离后同键可重试。解除须携带对应 `case_id`：任一孔未隔离（`not_quarantined`）或隔离案件不符（`case_mismatch`，附当前案件号）即 `409 release_mismatch` 整体拒绝；成功后申请记录保留（孔位行标记 `released_at`），解除记录单独追加，孔位查询（`GET /wells/{well}` 与 `GET /wells/{well}/batches`）返回当前隔离信息。两类 `request_key` 各自独立幂等：同键同目标（案件 + 排序去重后的孔位集合）重放返回首次结果（`replayed=true`），同键换目标返回 `409 request_key_conflict`；重放判定先于当前状态校验，因此解除后重新隔离的孔上重放旧解除键仍按重放处理。
- **移液体积预留**：`reservations`（`status` 为 `active` / `consumed` / `cancelled`，建立 `request_key` 唯一）+ `reservation_items`（每孔一行 Decimal 体积字符串）+ `reservation_cancellations`（取消审计，独立 `request_key` 唯一），`batches.reservation_id` 记录兑现批次（老库经 `ALTER TABLE` 就地补列，历史批次一律 `NULL`）。建立预留时在同一写事务内先确认所有孔已登记、未隔离，再按「孔当前体积 − 其他 active 预留量之和」逐孔校验可预留量：任一不足（`409 reservation_unavailable`，`conflicting_wells` 逐孔列出可用量/已预留量/请求量）即整体回滚，不留部分预留、不消耗 `request_key`。生效预留只约束**可吸取量下限**：未绑定预留的批次从来源孔吸液时可用量按 `当前体积 − 全部 active 预留量` 计，不足即 `InsufficientVolume`；补液是加入液体，不受预留阻塞，补液后可用量相应增加。批次携带 `reservation_id` 时，预留必须存在（`404 reservation_not_found`）且为 `active`（`409 reservation_not_active`），且批次从每个预留来源孔吸取的体积之和与预留体积**逐孔严格相等**——预留的每个孔都必须作为来源出现，批次也不得对未预留的来源孔吸液，否则 `409 reservation_fulfillment_mismatch`（`mismatched_wells` 逐孔列出 `reserved` 与 `requested`，未出现的一列为 `null`），该检查先于任何写入，故整批不留痕迹、不落失败批次、不消耗 `request_key`、预留保持 `active`；兑现匹配时本批次有权动用该预留自身的体积，但仍不得动用同孔其他 active 预留的体积（执行中体积不足/容量超限仍按普通失败处理：失败批次落库并消耗批次键，随事务回滚的 `consumed` 标记不会生效）。只有逐孔逐量完全兑现并成功提交时，才在同一事务把预留置为 `consumed`（记 `consumed_batch_id`/`consumed_at`），保证兑现与消费原子。取消用**独立** `request_key`（`POST /reservations/{id}/cancellations`）：仅 `active` 可取消（`consumed` → `409 already_consumed`；`cancelled` → `409 already_cancelled`），取消审计与预留状态同事务更新；取消键同键同预留重放返回首次结果，同键换预留返回 `409 request_key_conflict`。撤销批次在恢复快照前同样核对恢复后体积不低于各孔 active 预留量（批次新建、撤销即删除的孔上若有生效预留会以 `409 reservations_active` 阻塞并逐孔列出恢复后体积/预留量），预留一旦 `consumed` 不因批次撤销而退回 `active`（无「退回预留」语义，避免复活已归属的体积）。`GET /wells/{well}` 返回 `reserved_volume`（active 预留总量）、`available_volume`（当前体积 − active 预留量）与逐预留明细；`GET /reservations[/{id}]` 返回明细与状态（含消费批次与取消审计）。
- **状态码**：登记/批次成功/撤销成功/隔离申请/解除隔离/预留建立/预留取消 `201`；容量设置/取消成功 `200`；幂等重放 `200`；体积不足、容量超限、撤销冲突、隔离冲突、预留冲突（可用不足/换目标/预留未生效/未完全兑现/已消费或重复取消）`409`；孔位/批次/预留不存在 `404`；参数非法 `422`。

## 项目结构

```
app/
  main.py     # FastAPI 路由
  ledger.py   # 谱系台账核心逻辑（Decimal 运算、事务内批量执行）
  db.py       # SQLite 连接与建表
  schemas.py  # 请求体验证
tests/        # pytest 端到端测试
Dockerfile / compose.yaml
```

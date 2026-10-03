"""移液计划：先核对（预演固定快照）、后确认（快照未变才原子执行）。

覆盖：预演不落批次/不动液体、预演失败不落计划、确认重放/错键/已失效不重复
扣量、状态变化 409 可重试、与预留/隔离/容量/撤销的集成、并发串行。
"""

import os
import sqlite3
import threading
from decimal import Decimal

from app import ledger


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def plan(client, operations, request_key="plan-1", reservation_id=None):
    payload = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        payload["reservation_id"] = reservation_id
    return client.post("/plans", json=payload)


def confirm(client, plan_id, confirm_key="confirm-1"):
    return client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": confirm_key}
    )


def reverse(client, batch_id, request_key):
    return client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": request_key}
    )


def reserve(client, items, request_key="rv-1"):
    return client.post("/reservations", json={"request_key": request_key, "items": items})


def quarantine(client, wells, case_id="CASE-1", request_key="q-1"):
    return client.post(
        "/quarantines",
        json={"request_key": request_key, "case_id": case_id, "reason": "排查", "wells": wells},
    )


def release(client, wells, case_id="CASE-1", request_key="rel-1"):
    return client.post(
        "/quarantines/releases",
        json={"request_key": request_key, "case_id": case_id, "wells": wells},
    )


def db_query(sql):
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


class _Gate:
    """线程门闩：工作线程进入临界区后置位 entered，等待 release。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_inside(self, timeout: float = 10.0) -> None:
        assert self.entered.wait(timeout), "工作线程未在超时内进入门闩"


def _run_in_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, result


def _join(thread, result, timeout: float = 30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时未结束（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


# ------------------------------------------------ 预演：固定快照、逐步校验、不动液体


def test_plan_create_snapshots_and_validates_without_touching_liquid(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = plan(
        client,
        [
            {"source": "A1", "destination": "B1", "volume": "40"},
            {"source": "A2", "destination": "B1", "volume": "30"},
        ],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "planned"
    assert body["replayed"] is False
    assert body["confirm_key"] is None
    assert body["committed_at"] is None
    assert body["committed_batch_id"] is None
    assert body["batch"] is None
    plan_id = body["plan_id"]

    # 逐步校验结果：按提交顺序给出每步之后两孔的体积
    assert body["steps"] == [
        {
            "step": 1,
            "source": "A1",
            "destination": "B1",
            "volume": "40",
            "source_volume_after": "60",
            "destination_volume_after": "40",
        },
        {
            "step": 2,
            "source": "A2",
            "destination": "B1",
            "volume": "30",
            "source_volume_after": "30",
            "destination_volume_after": "70",
        },
    ]

    # 规范快照：体积/组成/容量/隔离/生效预留；B1 计划时尚未登记
    wells = {w["well"]: w for w in body["snapshot"]["wells"]}
    assert set(wells) == {"A1", "A2", "B1"}
    assert wells["A1"]["volume"] == "100"
    assert wells["A1"]["composition"] == [{"sample": "S1", "amount": "100"}]
    assert wells["A1"]["max_capacity"] is None
    assert wells["A1"]["quarantine_request_id"] is None
    assert wells["A1"]["reserved_volume"] == "0"
    assert wells["B1"]["exists"] is False
    assert wells["B1"]["volume"] is None

    # 不落批次、不动液体
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "60"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/batches").json()["batches"] == []
    assert db_query("SELECT COUNT(*) FROM batches") == 0
    # 孔位事件只有登记时的两条 top_up：计划不产生任何台账事件
    assert db_query("SELECT COUNT(*) FROM well_events WHERE type = 'batch'") == 0
    assert db_query("SELECT COUNT(*) FROM well_events") == 2

    # 单个与列表查询返回同样的计划
    got = client.get(f"/plans/{plan_id}").json()
    assert got["steps"] == body["steps"]
    assert got["snapshot"] == body["snapshot"]
    listing = client.get("/plans").json()["plans"]
    assert [p["plan_id"] for p in listing] == [plan_id]


def test_plan_unknown_is_404(client):
    assert client.get("/plans/999").status_code == 404
    assert confirm(client, 999).status_code == 404


def test_plan_validation_failure_persists_nothing_and_key_reusable(client):
    register(client, "A1", "S1", "100")
    resp = plan(
        client,
        [
            {"source": "A1", "destination": "B1", "volume": "60"},
            {"source": "A1", "destination": "B2", "volume": "50"},  # 只剩 40，失败
        ],
        request_key="plan-fail",
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "insufficient_volume"
    assert detail["step"] == 2
    assert detail["well"] == "A1"
    assert detail["available"] == "40"
    assert detail["requested"] == "50"

    # 预演失败不落计划、不消耗 request_key、不动液体
    assert client.get("/plans").json()["plans"] == []
    assert db_query("SELECT COUNT(*) FROM plans") == 0
    assert db_query("SELECT COUNT(*) FROM plan_operations") == 0
    assert db_query("SELECT COUNT(*) FROM plan_well_snapshots") == 0
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404

    # 修正后同键重试成功
    ok = plan(
        client,
        [
            {"source": "A1", "destination": "B1", "volume": "60"},
            {"source": "A1", "destination": "B2", "volume": "40"},
        ],
        request_key="plan-fail",
    )
    assert ok.status_code == 201, ok.text


def test_plan_capacity_validation_matches_batch_semantics(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "70")
    assert client.put("/wells/B1/capacity", json={"max_capacity": "100"}).status_code == 200

    # 70 + 40 > 100 -> 预演失败，不落计划
    resp = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "40"}],
        request_key="plan-cap",
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["step"] == 1
    assert detail["well"] == "B1"
    assert detail["volume"] == "70"
    assert detail["adding"] == "40"
    assert detail["max_capacity"] == "100"
    assert client.get("/plans").json()["plans"] == []

    # 与批次一致的「先移出再移入」逐步语义：先 B1->C1 移出 40，再 A1->B1 移入 40
    ok = plan(
        client,
        [
            {"source": "B1", "destination": "C1", "volume": "40"},
            {"source": "A1", "destination": "B1", "volume": "40"},
        ],
        request_key="plan-cap",
    )
    assert ok.status_code == 201, ok.text
    steps = ok.json()["steps"]
    assert steps[0]["source_volume_after"] == "30"
    assert steps[1]["destination_volume_after"] == "70"


def test_plan_quarantined_well_rejected_and_key_reusable_after_release(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, ["A1"]).status_code == 201

    resp = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        request_key="plan-q",
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert detail["quarantined_wells"] == [{"well": "A1", "case_id": "CASE-1"}]
    assert client.get("/plans").json()["plans"] == []

    # 冲突不消耗 request_key：解除隔离后同键成功
    assert release(client, ["A1"]).status_code == 201
    assert plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        request_key="plan-q",
    ).status_code == 201


def test_plan_unknown_source_fails_validation(client):
    resp = plan(
        client,
        [{"source": "Z9", "destination": "B1", "volume": "1"}],
        request_key="plan-ghost",
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["available"] == "0"
    assert client.get("/plans").json()["plans"] == []


# ------------------------------------------------ 计划幂等：同键重放 / 同键换目标


def test_plan_idempotent_replay_and_same_key_conflict(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    operations = [{"source": "A1", "destination": "B1", "volume": "40"}]
    first = plan(client, operations, request_key="plan-idem")
    assert first.status_code == 201
    plan_id = first.json()["plan_id"]

    # 同键同目标重放 -> 200，不重复落计划
    replay = plan(client, operations, request_key="plan-idem")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["plan_id"] == plan_id
    assert len(client.get("/plans").json()["plans"]) == 1

    # 同键换操作（顺序不同也算换目标：执行顺序是计划的一部分）
    resp = plan(
        client,
        [
            {"source": "A1", "destination": "B1", "volume": "40"},
            {"source": "A2", "destination": "B1", "volume": "10"},
        ],
        request_key="plan-idem",
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_operations"] == [
        {"source": "A1", "destination": "B1", "volume": "40"}
    ]

    # 同键同操作换绑定预留也算换目标
    reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-for-plan")
    resp = plan(client, operations, request_key="plan-idem", reservation_id=1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"
    assert len(client.get("/plans").json()["plans"]) == 1


# ------------------------------------------------ 确认：快照未变 -> 原子执行


def test_confirm_executes_atomically_and_marks_committed(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    plan_id = plan(
        client,
        [
            {"source": "A1", "destination": "B1", "volume": "40"},
            {"source": "A2", "destination": "B1", "volume": "30"},
        ],
    ).json()["plan_id"]

    resp = confirm(client, plan_id, "confirm-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "committed"
    assert body["replayed"] is False
    assert body["confirm_key"] == "confirm-1"
    assert body["committed_at"]
    batch_id = body["committed_batch_id"]
    assert batch_id is not None

    # 生成正式批次：request_key 即 confirm_key，操作按原顺序落库
    batch = client.get(f"/batches/{batch_id}").json()
    assert batch["request_key"] == "confirm-1"
    assert batch["status"] == "committed"
    assert [op["step"] for op in batch["operations"]] == [1, 2]
    assert body["batch"]["batch_id"] == batch_id

    # 液体按预演结果移动，目标孔按比例混合
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/A2").json()["volume"] == "30"
    b1 = client.get("/wells/B1").json()
    assert b1["volume"] == "70"
    assert amounts(b1) == {"S1": Decimal("40"), "S2": Decimal("30")}

    # 批次进入常规查询与孔位历史
    assert [b["batch_id"] for b in client.get("/batches").json()["batches"]] == [batch_id]
    history = client.get("/wells/B1/batches").json()["batches"]
    assert [b["batch_id"] for b in history] == [batch_id]

    # 确认生成的批次与正式批次一致：保存批前快照、可撤销
    rev = reverse(client, batch_id, "undo-confirm-1")
    assert rev.status_code == 201, rev.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    # 批次撤销不影响计划状态：计划保持 committed，不会再次执行
    assert client.get(f"/plans/{plan_id}").json()["status"] == "committed"


def test_confirm_replay_does_not_double_execute(client):
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]
    first = confirm(client, plan_id, "confirm-replay")
    assert first.status_code == 201
    batch_id = first.json()["committed_batch_id"]

    replay = confirm(client, plan_id, "confirm-replay")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["committed_batch_id"] == batch_id
    # 不重复扣量：体积不变、只有一批
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert len(client.get("/batches").json()["batches"]) == 1


def test_committed_plan_other_confirm_key_rejected(client):
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]
    assert confirm(client, plan_id, "confirm-a").status_code == 201

    # 已失效（committed）计划换键确认 -> 409，不重复扣量
    resp = confirm(client, plan_id, "confirm-b")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_committed"
    assert detail["confirm_key"] == "confirm-a"
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert len(client.get("/batches").json()["batches"]) == 1


def test_confirm_key_conflict_with_other_plan(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    p1 = plan(client, [{"source": "A1", "destination": "B1", "volume": "10"}], "p1").json()[
        "plan_id"
    ]
    p2 = plan(client, [{"source": "A2", "destination": "B2", "volume": "10"}], "p2").json()[
        "plan_id"
    ]
    assert confirm(client, p1, "shared-confirm").status_code == 201

    # 同键换计划 -> 409，第二个计划不执行
    resp = confirm(client, p2, "shared-confirm")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_plan_id"] == p1
    assert client.get("/wells/A2").json()["volume"] == "60"
    assert client.get("/wells/B2").status_code == 404
    assert client.get(f"/plans/{p2}").json()["status"] == "planned"

    # 错键不消耗第二个计划：换键确认成功
    assert confirm(client, p2, "confirm-p2").status_code == 201
    assert client.get("/wells/B2").json()["volume"] == "10"


def test_confirm_key_conflict_with_batch_request_key(client):
    register(client, "A1", "S1", "100")
    resp = client.post(
        "/batches",
        json={
            "request_key": "taken-key",
            "operations": [{"source": "A1", "destination": "C1", "volume": "5"}],
        },
    )
    assert resp.status_code == 201
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]

    # confirm_key 已在批次命名空间被占用 -> 409，计划不执行
    resp = confirm(client, plan_id, "taken-key")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_batch_id"] is not None
    assert client.get("/wells/A1").json()["volume"] == "95"
    assert client.get(f"/plans/{plan_id}").json()["status"] == "planned"

    # 换个 confirm_key 即可确认
    assert confirm(client, plan_id, "free-key").status_code == 201
    assert client.get("/wells/B1").json()["volume"] == "10"


# ------------------------------------------------ 确认：状态变化 -> 409 可重试


def test_confirm_blocked_by_volume_change_and_retryable_after_restore(client):
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]

    # 干扰批次改动涉及孔：A1 100 -> 90
    interferer = client.post(
        "/batches",
        json={
            "request_key": "interfere-1",
            "operations": [{"source": "A1", "destination": "C1", "volume": "10"}],
        },
    ).json()["batch_id"]

    resp = confirm(client, plan_id, "confirm-retry")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_state_changed"
    assert detail["plan_id"] == plan_id
    changed = {w["well"]: w["changes"] for w in detail["changed_wells"]}
    assert set(changed) == {"A1"}
    fields = {c["field"]: c for c in changed["A1"]}
    assert fields["volume"] == {"field": "volume", "planned": "100", "current": "90"}
    assert "composition" in fields  # 组成随体积一起变化

    # 未执行：液体不变、不落批次、计划保持 planned、confirm_key 未消耗
    assert client.get("/wells/A1").json()["volume"] == "90"
    assert client.get("/wells/B1").status_code == 404
    assert client.get(f"/plans/{plan_id}").json()["status"] == "planned"
    assert db_query("SELECT COUNT(*) FROM batches WHERE request_key = 'confirm-retry'") == 0

    # 撤销干扰批次恢复原状后，同一 confirm_key 重试成功
    assert reverse(client, interferer, "undo-interfere").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"
    ok = confirm(client, plan_id, "confirm-retry")
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/B1").json()["volume"] == "40"


def test_confirm_blocked_by_capacity_change(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "10")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]

    assert client.put("/wells/B1/capacity", json={"max_capacity": "80"}).status_code == 200
    resp = confirm(client, plan_id, "confirm-cap")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_state_changed"
    changes = detail["changed_wells"][0]["changes"]
    assert changes == [{"field": "capacity", "planned": None, "current": "80"}]
    assert client.get("/wells/B1").json()["volume"] == "10"

    # 取消上限后同键重试成功
    assert client.put("/wells/B1/capacity", json={"max_capacity": None}).status_code == 200
    assert confirm(client, plan_id, "confirm-cap").status_code == 201


def test_confirm_blocked_by_quarantine(client):
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]

    assert quarantine(client, ["A1"], case_id="CASE-9").status_code == 201
    resp = confirm(client, plan_id, "confirm-quar")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_state_changed"
    changes = detail["changed_wells"][0]["changes"]
    assert changes[0]["field"] == "quarantine"
    assert changes[0]["planned"] is None
    assert changes[0]["current"] is not None
    assert client.get("/wells/B1").status_code == 404

    # 解除隔离后同键重试成功
    assert release(client, ["A1"], case_id="CASE-9").status_code == 201
    assert confirm(client, plan_id, "confirm-quar").status_code == 201


def test_confirm_blocked_by_reservation_change(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-block").json()[
        "reservation_id"
    ]
    # 未绑定预留的计划：快照记录 A1 上有 40 生效预留（可吸取量按 60 计）
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]
    snap = {w["well"]: w for w in client.get(f"/plans/{plan_id}").json()["snapshot"]["wells"]}
    assert snap["A1"]["reserved_volume"] == "40"
    assert snap["A1"]["reservations"] == [{"reservation_id": rid, "volume": "40"}]

    # 取消预留 -> 生效预留集合变化 -> 409
    assert client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-block"}
    ).status_code == 201
    resp = confirm(client, plan_id, "confirm-rv")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_state_changed"
    changes = detail["changed_wells"][0]["changes"]
    assert changes[0]["field"] == "reservations"
    assert changes[0]["planned"] == [{"reservation_id": rid, "volume": "40"}]
    assert changes[0]["current"] == []
    assert client.get(f"/plans/{plan_id}").json()["status"] == "planned"
    assert client.get("/wells/B1").status_code == 404


def test_confirm_blocked_when_bound_reservation_cancelled(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-bound").json()[
        "reservation_id"
    ]
    plan_id = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "40"}],
        reservation_id=rid,
    ).json()["plan_id"]

    assert client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-bound"}
    ).status_code == 201
    resp = confirm(client, plan_id, "confirm-bound")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_state_changed"
    assert detail["reservation"] == {
        "reservation_id": rid,
        "planned": "active",
        "current": "cancelled",
    }
    # 计划保持 planned，液体不变
    assert client.get(f"/plans/{plan_id}").json()["status"] == "planned"
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_uninvolved_well_change_does_not_block_confirm(client):
    register(client, "A1", "S1", "100")
    register(client, "C1", "S9", "50")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]

    # 快照之外的孔变化不影响确认
    register(client, "C1", "S9", "10")
    register(client, "C2", "S8", "5")
    assert confirm(client, plan_id, "confirm-iso").status_code == 201
    assert client.get("/wells/B1").json()["volume"] == "40"


# ------------------------------------------------ 计划与预留兑现


def test_plan_with_reservation_fulfillment_and_confirm_consumes(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "30"}],
        request_key="rv-fulfill",
    ).json()["reservation_id"]

    # 未逐孔逐量兑现 -> 预演 409，不落计划
    resp = plan(
        client,
        [{"source": "A1", "destination": "C1", "volume": "40"}],
        request_key="plan-mis",
        reservation_id=rid,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "reservation_fulfillment_mismatch"
    assert client.get("/plans").json()["plans"] == []

    # 逐孔逐量完全兑现（A1 拆两步）-> 预演成功
    ok = plan(
        client,
        [
            {"source": "A1", "destination": "C1", "volume": "10"},
            {"source": "A2", "destination": "C1", "volume": "30"},
            {"source": "A1", "destination": "C2", "volume": "30"},
        ],
        request_key="plan-fulfill",
        reservation_id=rid,
    )
    assert ok.status_code == 201, ok.text
    plan_id = ok.json()["plan_id"]
    assert ok.json()["reservation_id"] == rid

    # 确认成功：同一事务执行并把预留标记 consumed
    assert confirm(client, plan_id, "confirm-fulfill").status_code == 201
    stored = client.get(f"/reservations/{rid}").json()
    assert stored["status"] == "consumed"
    assert stored["consumed_batch_id"] is not None
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/C1").json()["volume"] == "40"
    assert client.get("/wells/C2").json()["volume"] == "30"


def test_plan_reservation_not_active_or_missing(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "10"}], request_key="rv-x").json()[
        "reservation_id"
    ]
    assert client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-x"}
    ).status_code == 201

    resp = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        request_key="plan-inactive",
        reservation_id=rid,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "reservation_not_active"
    resp = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        request_key="plan-missing",
        reservation_id=999,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "reservation_not_found"
    assert client.get("/plans").json()["plans"] == []


def test_unbound_plan_cannot_use_reserved_volume(client):
    register(client, "A1", "S1", "100")
    reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-hold")
    # A1 可用量 = 100 - 40 = 60：预演按 60 校验
    resp = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "70"}],
        request_key="plan-hold",
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "insufficient_volume"
    assert detail["available"] == "60"
    assert client.get("/plans").json()["plans"] == []


# ------------------------------------------------ Decimal 精度：预演与执行一致


def test_plan_decimal_precision_matches_execution(client):
    register(client, "A1", "S1", "0.000000005")
    register(client, "A1", "S2", "0.000000003")
    plan_id = plan(
        client,
        [{"source": "A1", "destination": "B1", "volume": "0.000000005"}],
        request_key="plan-tiny",
    ).json()["plan_id"]

    assert confirm(client, plan_id, "confirm-tiny").status_code == 201
    a1 = client.get("/wells/A1").json()
    assert a1["volume"] == "0.000000003"
    assert amounts(a1) == {"S1": Decimal("0.000000002"), "S2": Decimal("0.000000001")}
    b1 = client.get("/wells/B1").json()
    assert amounts(b1) == {"S1": Decimal("0.000000003"), "S2": Decimal("0.000000002")}


# ------------------------------------------------ 并发串行


def test_concurrent_same_key_plan_replays_once(client):
    register(client, "A1", "S1", "100")
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(
            lambda: plan(
                client,
                [{"source": "A1", "destination": "B1", "volume": "40"}],
                request_key="plan-conc",
            )
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]
    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["plan_id"] for b in bodies} == {bodies[0]["plan_id"]}
    assert [b["replayed"] for b in bodies].count(True) == 1
    assert len(client.get("/plans").json()["plans"]) == 1


def test_concurrent_same_key_confirm_replays_once(client):
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}], "plan-cc"
    ).json()["plan_id"]
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(lambda: confirm(client, plan_id, "confirm-cc"))
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    # 一个首次确认、一个重放：只执行一次
    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["committed_batch_id"] for b in bodies} == {bodies[0]["committed_batch_id"]}
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert len(client.get("/batches").json()["batches"]) == 1


def test_concurrent_other_key_confirm_no_double_execute(client):
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}], "plan-ck"
    ).json()["plan_id"]

    barrier = threading.Barrier(2)
    threads, results = [], []
    for key in ("conf-a", "conf-b"):
        t, r = _run_in_thread(
            lambda key=key: (barrier.wait(), confirm(client, plan_id, key))[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    # 先提交者执行，后到者看到已 committed：409，不重复扣量
    assert sorted(resp.status_code for resp in responses) == [201, 409]
    conflict = next(resp for resp in responses if resp.status_code == 409)
    assert conflict.json()["detail"]["reason"] == "already_committed"
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert len(client.get("/batches").json()["batches"]) == 1


def test_top_up_committed_first_confirm_sees_change(client, monkeypatch):
    """补液先持锁提交：确认在锁后重读状态，看到体积变化返回 409，计划可重试。"""
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}], "plan-race"
    ).json()["plan_id"]

    gate = _Gate()
    confirm_ready = threading.Event()
    original_register = ledger.register_initial_sample
    original_get_plan_row = ledger.get_plan_row

    def gated_register(conn, well, sample, volume):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "补液线程未被释放"
        return original_register(conn, well, sample, volume)

    def gated_get_plan_row(conn, pid):
        # 确认事务外预检前：先确认补液已持锁，再让确认去排队取锁
        confirm_ready.wait(10.0)
        return original_get_plan_row(conn, pid)

    monkeypatch.setattr(ledger, "register_initial_sample", gated_register)
    monkeypatch.setattr(ledger, "get_plan_row", gated_get_plan_row)

    # 确认先启动：停在预检门闩前，尚未取锁
    conf_thread, conf_result = _run_in_thread(lambda: confirm(client, plan_id, "conf-race"))
    topup_thread, topup_result = _run_in_thread(lambda: register(client, "A1", "S1", "5"))
    gate.wait_inside()  # 补液持锁

    confirm_ready.set()  # 放行确认：它在 BEGIN IMMEDIATE 处排队
    threading.Event().wait(0.3)
    assert conf_thread.is_alive(), "确认不应在补液持锁期间完成"

    gate.release.set()
    topup_body = _join(topup_thread, topup_result)
    conf_resp = _join(conf_thread, conf_result)

    assert topup_body["volume"] == "105"
    assert conf_resp.status_code == 409, conf_resp.text
    detail = conf_resp.json()["detail"]
    assert detail["reason"] == "plan_state_changed"
    # 液体保持补液后的状态，计划保持可重试
    assert client.get("/wells/A1").json()["volume"] == "105"
    assert client.get("/wells/B1").status_code == 404
    assert client.get(f"/plans/{plan_id}").json()["status"] == "planned"


def test_confirm_committed_first_top_up_stacks_after(client, monkeypatch):
    """确认先持锁提交：补液在锁上等待，随后叠加在执行后的状态上。"""
    register(client, "A1", "S1", "100")
    plan_id = plan(
        client, [{"source": "A1", "destination": "B1", "volume": "40"}], "plan-first"
    ).json()["plan_id"]

    gate = _Gate()
    original_execute = ledger.execute_batch

    def gated_execute(conn, request_key, operations, reservation_id=None):
        # 已在 BEGIN IMMEDIATE 之后、状态比对通过：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "确认线程未被释放"
        return original_execute(conn, request_key, operations, reservation_id)

    monkeypatch.setattr(ledger, "execute_batch", gated_execute)

    conf_thread, conf_result = _run_in_thread(lambda: confirm(client, plan_id, "conf-first"))
    gate.wait_inside()  # 确认持锁

    topup_thread, topup_result = _run_in_thread(lambda: register(client, "A1", "S1", "5"))
    threading.Event().wait(0.3)
    assert topup_thread.is_alive(), "补液应在确认的写锁上排队"

    gate.release.set()
    conf_resp = _join(conf_thread, conf_result)
    topup_body = _join(topup_thread, topup_result)

    assert conf_resp.status_code == 201, conf_resp.text
    # 确认先执行（A1: 100 -> 60），补液随后叠加：65
    assert topup_body["volume"] == "65"
    a1 = client.get("/wells/A1").json()
    assert a1["volume"] == "65"
    assert amounts(a1) == {"S1": Decimal("65")}
    assert client.get("/wells/B1").json()["volume"] == "40"

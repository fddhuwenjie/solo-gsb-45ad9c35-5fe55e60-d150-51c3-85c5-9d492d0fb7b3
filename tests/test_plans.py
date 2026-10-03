"""移液计划「先核对、后确认」流程的端到端测试。

计划（POST /plans）在只读事务固定涉及孔规范快照并逐步校验，不写批次、不
改变液体；确认（POST /plans/{id}/confirmations）在写事务重读状态，仅当
快照各维（体积、组成、容量、隔离、生效预留）均未变化才按原顺序原子执行。
"""

import os
import sqlite3
import threading
from decimal import Decimal


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def plan(client, request_key, operations, reservation_id=None):
    payload = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        payload["reservation_id"] = reservation_id
    return client.post("/plans", json=payload)


def confirm(client, plan_id, confirm_key):
    return client.post(
        f"/plans/{plan_id}/confirmations", json={"confirm_key": confirm_key}
    )


def reserve(client, items, request_key):
    return client.post(
        "/reservations", json={"request_key": request_key, "items": items}
    )


def batch(client, request_key, operations, reservation_id=None):
    payload = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        payload["reservation_id"] = reservation_id
    return client.post("/batches", json=payload)


def db_query(sql, args=()):
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ------------------------------------------------ 预演 + 确认成功


def test_plan_preview_does_not_change_liquid_or_write_batch(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = plan(
        client,
        "p1",
        [
            {"source": "A1", "destination": "B1", "volume": "40"},
            {"source": "A2", "destination": "B1", "volume": "30"},
        ],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["committed_at"] is None
    assert body["confirmation"] is None
    assert body["replayed"] is False
    # 逐步校验结果
    steps = body["validation"]["steps"]
    assert [s["step"] for s in steps] == [1, 2]
    assert all(s["ok"] for s in steps)
    assert steps[0]["source_volume_before"] == "100"
    assert steps[0]["source_volume_after"] == "60"
    assert steps[0]["destination_volume_after"] == "40"
    assert steps[0]["moved_composition"] == [{"sample": "S1", "amount": "40"}]
    assert steps[1]["destination_volume_before"] == "40"
    assert steps[1]["destination_volume_after"] == "70"
    # 规范快照固定了涉及孔（含尚未登记的目标 B1）
    snapshots = {s["well"]: s for s in body["snapshot"]}
    assert set(snapshots) == {"A1", "A2", "B1"}
    assert snapshots["A1"]["volume"] == "100"
    assert snapshots["B1"]["registered"] is False
    assert snapshots["B1"]["volume"] is None
    # 液体与批次均未变化
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/batches").json()["batches"] == []


def test_plan_confirmation_executes_in_original_order(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    pid = plan(
        client,
        "p1",
        [
            {"source": "A1", "destination": "B1", "volume": "40"},
            {"source": "A2", "destination": "B1", "volume": "30"},
        ],
    ).json()["plan_id"]

    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["plan_id"] == pid
    assert body["batch_id"] == body["batch"]["batch_id"]
    assert body["batch"]["status"] == "committed"
    assert [op["step"] for op in body["batch"]["operations"]] == [1, 2]
    # 计划标记 committed
    stored = client.get(f"/plans/{pid}").json()
    assert stored["status"] == "committed"
    assert stored["committed_batch_id"] == body["batch_id"]
    assert stored["confirmation"]["confirm_key"] == "cf-1"
    # 液体按原顺序执行
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/A2").json()["volume"] == "30"
    b1 = client.get("/wells/B1").json()
    assert b1["volume"] == "70"
    assert amounts(b1) == {"S1": Decimal("40"), "S2": Decimal("30")}
    # 批次溯源到计划确认
    assert (
        client.get(f"/batches/{body['batch_id']}").json()["plan_confirmation_id"]
        == body["confirmation_id"]
    )


def test_plan_request_key_replay_and_other_target_conflict(client):
    register(client, "A1", "S1", "100")
    ops = [{"source": "A1", "destination": "B1", "volume": "40"}]
    first = plan(client, "p-idem", ops)
    assert first.status_code == 201
    pid = first.json()["plan_id"]

    replay = plan(client, "p-idem", ops)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["plan_id"] == pid
    assert len(client.get("/plans").json()["plans"]) == 1

    # 同键换目标（换目标孔）-> 409，不新增计划
    resp = plan(client, "p-idem", [{"source": "A1", "destination": "B2", "volume": "40"}])
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"
    assert resp.json()["detail"]["existing_plan_id"] == pid
    assert len(client.get("/plans").json()["plans"]) == 1

    # 同键换预留 -> 409
    rid = reserve(client, [{"well": "A1", "volume": "10"}], "rv-1").json()[
        "reservation_id"
    ]
    resp = plan(
        client,
        "p-idem",
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        reservation_id=rid,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"


def test_plan_validation_errors(client):
    assert client.post(
        "/plans", json={"request_key": "p-empty", "operations": []}
    ).status_code == 422
    register(client, "A1", "S1", "10")
    assert client.post(
        "/plans",
        json={
            "request_key": "p-self",
            "operations": [{"source": "A1", "destination": "A1", "volume": "1"}],
        },
    ).status_code == 422
    assert client.get("/plans/999").status_code == 404
    assert confirm(client, 999, "cf-x").status_code == 404


# ------------------------------------------------ 预演失败不落计划


def test_plan_preview_insufficient_volume_is_not_persisted(client):
    register(client, "A1", "S1", "100")
    payload = {
        "request_key": "p-fail",
        "operations": [
            {"source": "A1", "destination": "B1", "volume": "60"},
            {"source": "A1", "destination": "B2", "volume": "50"},
        ],
    }
    resp = client.post("/plans", json=payload)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "insufficient_volume"
    assert detail["failed_step"] == 2
    assert detail["well"] == "A1"
    assert detail["available"] == "40"
    assert detail["requested"] == "50"
    # 逐步结果：第一步通过、第二步失败
    assert [s["ok"] for s in detail["steps"]] == [True, False]
    assert detail["steps"][1]["reason"] == "insufficient_volume"
    # 不落计划、不写批次、液体不变、键未消耗
    assert client.get("/plans").json()["plans"] == []
    assert client.get("/batches").json()["batches"] == []
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    retry = client.post("/plans", json=payload)
    assert retry.status_code == 409  # 仍按实时状态预演，而非重放旧结果
    # 修正后同键成功
    payload["operations"][1]["volume"] = "40"
    assert client.post("/plans", json=payload).status_code == 201


def test_plan_preview_capacity_exceeded_is_not_persisted(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S9", "50")
    client.put("/wells/B1/capacity", json={"max_capacity": "60"})
    resp = plan(
        client,
        "p-cap",
        [
            {"source": "A1", "destination": "C1", "volume": "10"},
            {"source": "A1", "destination": "B1", "volume": "20"},
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["failed_step"] == 2
    assert detail["well"] == "B1"
    assert detail["volume"] == "50"
    assert detail["adding"] == "20"
    assert detail["max_capacity"] == "60"
    assert [s["ok"] for s in detail["steps"]] == [True, False]
    # 不落计划、液体不变
    assert client.get("/plans").json()["plans"] == []
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/C1").status_code == 404


def test_plan_preview_capacity_checked_per_step_move_out_then_in(client):
    """预演与执行同一套逐步容量规则：先移出再移入可通过。"""
    register(client, "A1", "S1", "50")
    register(client, "B1", "S2", "70")
    client.put("/wells/B1/capacity", json={"max_capacity": "80"})
    resp = plan(
        client,
        "p-steps",
        [
            {"source": "B1", "destination": "C1", "volume": "30"},
            {"source": "A1", "destination": "B1", "volume": "40"},
        ],
    )
    assert resp.status_code == 201, resp.text
    steps = resp.json()["validation"]["steps"]
    assert steps[1]["destination_volume_before"] == "40"
    assert steps[1]["destination_volume_after"] == "80"
    assert confirm(client, resp.json()["plan_id"], "cf-1").status_code == 201
    assert client.get("/wells/B1").json()["volume"] == "80"


def test_plan_preview_quarantined_well_rejected_without_plan(client):
    register(client, "A1", "S1", "100")
    client.post(
        "/quarantines",
        json={
            "request_key": "q-1",
            "case_id": "CASE-1",
            "reason": "排查",
            "wells": ["A1"],
        },
    )
    resp = plan(client, "p-q", [{"source": "A1", "destination": "B1", "volume": "1"}])
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    assert resp.json()["detail"]["quarantined_wells"] == [
        {"well": "A1", "case_id": "CASE-1"}
    ]
    assert client.get("/plans").json()["plans"] == []


def test_plan_preview_reservation_errors_are_not_persisted(client):
    register(client, "A1", "S1", "100")
    # 未知预留 -> 404
    resp = plan(
        client,
        "p-no-rv",
        [{"source": "A1", "destination": "B1", "volume": "1"}],
        reservation_id=42,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "reservation_not_found"
    # 已取消预留 -> 409 reservation_not_active
    rid = reserve(client, [{"well": "A1", "volume": "10"}], "rv-1").json()[
        "reservation_id"
    ]
    client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-1"}
    )
    resp = plan(
        client,
        "p-canceled",
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        reservation_id=rid,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "reservation_not_active"
    # 未逐孔逐量兑现 -> 409，不落计划
    rid2 = reserve(client, [{"well": "A1", "volume": "40"}], "rv-2").json()[
        "reservation_id"
    ]
    resp = plan(
        client,
        "p-mis",
        [{"source": "A1", "destination": "B1", "volume": "30"}],
        reservation_id=rid2,
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_fulfillment_mismatch"
    assert detail["mismatched_wells"] == [
        {"well": "A1", "reserved": "40", "requested": "30"}
    ]
    assert client.get("/plans").json()["plans"] == []


def test_plan_unbound_respects_active_reservation_in_preview(client):
    """未绑定计划的逐步可用量同样扣除生效预留：预演失败不落计划。"""
    register(client, "A1", "S1", "100")
    reserve(client, [{"well": "A1", "volume": "60"}], "rv-1")
    resp = plan(
        client, "p-unbound", [{"source": "A1", "destination": "B1", "volume": "50"}]
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "insufficient_volume"
    assert detail["available"] == "40"
    assert client.get("/plans").json()["plans"] == []
    # 快照中含生效预留明细；取 40 的计划可建、可确认
    ok = plan(
        client, "p-ok", [{"source": "A1", "destination": "B1", "volume": "40"}]
    )
    assert ok.status_code == 201
    snap = {s["well"]: s for s in ok.json()["snapshot"]}["A1"]
    assert snap["active_reservations"] == [{"reservation_id": 1, "volume": "60"}]
    assert confirm(client, ok.json()["plan_id"], "cf-1").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "60"


# ------------------------------------------------ 确认时快照失效


def test_confirm_conflict_lists_well_and_reason_and_plan_stays_retryable(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    register(client, "A1", "S1", "5")  # 预演之后补液

    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_stale"
    assert detail["plan_id"] == pid
    changed = {c["well"]: c for c in detail["changed_wells"]}
    assert set(changed["A1"]["reasons"]) == {"volume_changed", "composition_changed"}
    assert changed["A1"]["volume_at_plan"] == "100"
    assert changed["A1"]["volume_now"] == "105"
    # 计划保持待确认、confirm_key 未消耗，液体未被计划改动
    assert client.get(f"/plans/{pid}").json()["status"] == "awaiting_confirmation"
    assert client.get("/wells/A1").json()["volume"] == "105"
    # 失效状态下原键重试仍是 409（不扣量）
    assert confirm(client, pid, "cf-1").status_code == 409
    assert client.get("/wells/B1").status_code == 404


def test_confirm_capacity_change_is_stale(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    client.put("/wells/A1/capacity", json={"max_capacity": "120"})
    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409
    changed = {c["well"]: c for c in resp.json()["detail"]["changed_wells"]}
    assert changed["A1"]["reasons"] == ["capacity_changed"]
    assert changed["A1"]["max_capacity_at_plan"] is None
    assert changed["A1"]["max_capacity_now"] == "120"
    assert client.get(f"/plans/{pid}").json()["status"] == "awaiting_confirmation"


def test_confirm_quarantine_change_is_stale(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-9", "reason": "x", "wells": ["A1"]},
    )
    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409
    changed = {c["well"]: c for c in resp.json()["detail"]["changed_wells"]}
    assert changed["A1"]["reasons"] == ["quarantined"]
    # 解除隔离后原 confirm_key 重试成功（计划可重试）
    client.post(
        "/quarantines/releases",
        json={"request_key": "rel-1", "case_id": "CASE-9", "wells": ["A1"]},
    )
    assert confirm(client, pid, "cf-1").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "90"


def test_confirm_reservation_change_is_stale(client):
    register(client, "A1", "S1", "100")
    # 计划后新建预留 -> 生效预留集合变化
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    rid = reserve(client, [{"well": "A1", "volume": "20"}], "rv-1").json()[
        "reservation_id"
    ]
    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409
    changed = {c["well"]: c for c in resp.json()["detail"]["changed_wells"]}
    assert changed["A1"]["reasons"] == ["reservations_changed"]
    assert changed["A1"]["reservations"] == [
        {
            "reservation_id": rid,
            "change": "reserved",
            "volume_at_plan": None,
            "volume_now": "20",
        }
    ]
    # 取消该预留后集合恢复一致，原键确认成功
    client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-1"}
    )
    assert confirm(client, pid, "cf-1").status_code == 201


def test_confirm_destination_registered_in_between_is_stale(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    register(client, "B1", "S9", "5")  # 计划目标孔在期间被登记
    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409
    changed = {c["well"]: c for c in resp.json()["detail"]["changed_wells"]}
    assert changed["B1"]["reasons"] == ["well_registered", "volume_changed", "composition_changed"]
    assert changed["B1"]["was_registered"] is False
    assert changed["B1"]["registered_now"] is True


def test_confirm_composition_change_with_same_volume_is_stale(client):
    """体积相同但组成改变（直接改库模拟组分替换）也必须识别为失效。"""
    register(client, "A1", "S1", "10")
    pid = plan(
        client, "p1",
        [{"source": "A1", "destination": "B1", "volume": "0.000000001"}],
    ).json()["plan_id"]
    db_file = os.environ["LINEAGE_DB"]
    conn = sqlite3.connect(db_file)
    try:
        sid = conn.execute("SELECT id FROM samples WHERE name = 'S1'").fetchone()[0]
        conn.execute(
            "INSERT INTO samples (name) VALUES ('S2')"
        )
        sid2 = conn.execute("SELECT id FROM samples WHERE name = 'S2'").fetchone()[0]
        # 体积保持 10：把 S1 的量改成 9，加入 S2=1
        conn.execute(
            "UPDATE well_composition SET amount = '9' WHERE well = 'A1' AND sample_id = ?",
            (sid,),
        )
        conn.execute(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES ('A1', ?, '1')",
            (sid2,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409
    changed = {c["well"]: c for c in resp.json()["detail"]["changed_wells"]}
    assert "composition_changed" in changed["A1"]["reasons"]
    assert "volume_changed" not in changed["A1"]["reasons"]
    samples = {c["sample"]: c for c in changed["A1"]["composition"]}
    assert samples["S1"]["amount_at_plan"] == "10"
    assert samples["S1"]["amount_now"] == "9"
    assert samples["S2"]["amount_at_plan"] == "0"
    assert samples["S2"]["amount_now"] == "1"
    # 液体未被计划改动
    assert client.get("/wells/A1").json()["volume"] == "10"


def test_confirm_after_state_restored_to_snapshot_succeeds(client):
    """冲突源消失、状态回到快照后，同一计划与 confirm_key 仍可确认。"""
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    bid = batch(
        client,
        "b-direct",
        [{"source": "A1", "destination": "C1", "volume": "5"}],
    ).json()["batch_id"]
    assert confirm(client, pid, "cf-1").status_code == 409
    # 撤销直接批次：A1 液体恢复 100、C1 删除；生效预留集合本就未变
    client.post(f"/batches/{bid}/reversals", json={"request_key": "rev-1"})
    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "90"
    assert client.get("/wells/B1").json()["volume"] == "10"


# ------------------------------------------------ 确认重放、错键、重复确认不重复扣量


def test_confirmation_replay_does_not_double_apply(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]
    first = confirm(client, pid, "cf-1")
    assert first.status_code == 201
    cid = first.json()["confirmation_id"]

    replay = confirm(client, pid, "cf-1")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["confirmation_id"] == cid
    # 只执行一次
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert len(db_query("SELECT id FROM batches")) == 1


def test_wrong_confirm_key_conflict_does_not_apply(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    p1 = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    p2 = plan(
        client, "p2", [{"source": "A2", "destination": "B2", "volume": "10"}]
    ).json()["plan_id"]
    assert confirm(client, p1, "shared-cf").status_code == 201
    # 同键确认另一计划 -> 409，p2 不执行（A2 与 p1 互不触及，p2 快照仍有效，
    # 仅因错键被拒绝）
    resp = confirm(client, p2, "shared-cf")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["plan_id"] == p2
    assert detail["existing_plan_id"] == p1
    assert client.get(f"/plans/{p2}").json()["status"] == "awaiting_confirmation"
    assert client.get("/wells/B2").status_code == 404
    assert client.get("/wells/A2").json()["volume"] == "50"
    # p2 换独立键后快照仍有效，正常确认（错键没有消耗 p2 的确认资格）
    assert confirm(client, p2, "cf-2").status_code == 201
    assert client.get("/wells/A2").json()["volume"] == "40"


def test_second_confirmation_of_committed_plan_is_rejected(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    assert confirm(client, pid, "cf-1").status_code == 201
    resp = confirm(client, pid, "cf-2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_committed"
    # 不重复扣量
    assert client.get("/wells/A1").json()["volume"] == "90"
    assert len(db_query("SELECT id FROM batches")) == 1


def test_confirm_key_colliding_with_batch_request_key_is_rejected(client):
    """confirm_key 已被直接批次（批次 request_key 空间）使用：明确 409，不执行。"""
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    bid = batch(
        client, "shared-key", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["batch_id"]
    pid = plan(
        client, "p1", [{"source": "A2", "destination": "B2", "volume": "10"}]
    ).json()["plan_id"]
    resp = confirm(client, pid, "shared-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_batch_id"] == bid
    # 计划未执行、键未消耗：换独立 confirm_key 后正常确认
    assert client.get("/wells/A2").json()["volume"] == "50"
    assert confirm(client, pid, "cf-other").status_code == 201
    assert client.get("/wells/A2").json()["volume"] == "40"


def test_concurrent_same_confirm_key_applies_once(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]
    threads, results = [], []

    def runner():
        out = {}
        try:
            out["value"] = confirm(client, pid, "cf-c")
        except Exception as exc:  # 在线程外重新抛出
            out["error"] = exc
        results.append(out)

    for _ in range(2):
        t = threading.Thread(target=runner)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(30)
        assert not t.is_alive()
    for out in results:
        if "error" in out:
            raise out["error"]
    statuses = sorted(out["value"].status_code for out in results)
    assert statuses == [200, 201], statuses
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert len(db_query("SELECT id FROM batches")) == 1


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


def _join(thread, result, timeout=30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时未结束（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_concurrent_writer_committed_first_confirmation_is_stale(client, monkeypatch):
    """直接批次先持锁提交：计划确认在锁后读到新状态，409 plan_stale 且不扣量。

    与 test_concurrency.py 相同的门闩手法：把确认挡在取写锁之前，让直接批次
    先持锁提交，再放行确认——它在 BEGIN IMMEDIATE 后必然读到已提交的新状态。
    """
    import contextlib

    import app.main as main_module

    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]

    confirm_ready = threading.Event()
    release = threading.Event()
    original_write = main_module.db_module.write_transaction
    entered = {"confirm": False}

    @contextlib.contextmanager
    def gated_write(conn):
        # 只拦截确认端点（以尚未置位的标志识别）：挡在它取写锁之前
        if not entered["confirm"]:
            entered["confirm"] = True
            confirm_ready.set()
            release.wait(10)
        with original_write(conn) as c:
            yield c

    monkeypatch.setattr(main_module.db_module, "write_transaction", gated_write)

    # 确认启动：停在写事务入口（尚未取锁）
    c_thread, c_result = _run_in_thread(lambda: confirm(client, pid, "cf-1"))
    assert confirm_ready.wait(10)

    # 直接批次（不经门闩的请求连接）先取锁并完整提交
    direct = batch(
        client,
        "b-direct",
        [{"source": "A1", "destination": "C1", "volume": "5"}],
    )
    assert direct.status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "95"

    # 放行确认：锁后读到 A1=95 -> plan_stale，不执行、不扣量
    release.set()
    resp = _join(c_thread, c_result)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "plan_stale"
    assert "volume_changed" in {
        c["well"]: c for c in resp.json()["detail"]["changed_wells"]
    }["A1"]["reasons"]
    assert client.get("/wells/B1").status_code == 404
    assert len(db_query("SELECT id FROM batches")) == 1
    assert client.get(f"/plans/{pid}").json()["status"] == "awaiting_confirmation"


def test_concurrent_confirmation_serializes_against_direct_batch(client, monkeypatch):
    """确认先持锁执行时，并发直接批次在锁上等待：要么确认成功要么批次先成功后确认 409。"""
    from app import ledger

    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]

    gate = threading.Event()
    entered = threading.Event()
    original = ledger.execute_batch

    def gated_execute_batch(conn, request_key, operations, reservation_id=None,
                            plan_confirmation_id=None):
        # 门闩计划确认路径：锁内、执行前
        if plan_confirmation_id is not None:
            entered.set()
            gate.wait(10)
        return original(
            conn, request_key, operations, reservation_id, plan_confirmation_id
        )

    monkeypatch.setattr(ledger, "execute_batch", gated_execute_batch)

    c_thread, c_result = _run_in_thread(lambda: confirm(client, pid, "cf-1"))
    assert entered.wait(10)

    # 直接批次在确认的写锁上排队
    b_thread, b_result = _run_in_thread(
        lambda: batch(
            client,
            "b-wait",
            [{"source": "A1", "destination": "C1", "volume": "5"}],
        )
    )
    threading.Event().wait(0.3)
    assert b_thread.is_alive(), "直接批次应在计划确认的写锁上排队"
    gate.set()
    confirmed = _join(c_thread, c_result)
    direct = _join(b_thread, b_result)

    # 确认先提交：A1=60、B1=40；直接批次再取 5：A1=55、C1=5
    assert confirmed.status_code == 201
    assert direct.status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "55"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get("/wells/C1").json()["volume"] == "5"
    # 两个批次（确认批次 + 直接批次），确认只有一次
    assert len(db_query("SELECT id FROM batches")) == 2
    assert len(db_query("SELECT id FROM plan_confirmations")) == 1


# ------------------------------------------------ 绑定预留的计划


def test_bound_plan_consumes_reservation_at_confirmation(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "30"}],
        "rv-1",
    ).json()["reservation_id"]
    pid = plan(
        client,
        "p1",
        [
            {"source": "A1", "destination": "C1", "volume": "10"},
            {"source": "A2", "destination": "C1", "volume": "30"},
            {"source": "A1", "destination": "C2", "volume": "30"},
        ],
        reservation_id=rid,
    ).json()["plan_id"]
    # 确认前预留仍 active、液体未动
    assert client.get(f"/reservations/{rid}").json()["status"] == "active"
    assert client.get("/wells/A1").json()["volume"] == "100"

    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 201, resp.text
    stored = client.get(f"/reservations/{rid}").json()
    assert stored["status"] == "consumed"
    assert stored["consumed_batch_id"] == resp.json()["batch_id"]
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/A2").json()["volume"] == "30"


def test_bound_plan_confirmation_after_reservation_canceled_is_stale(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], "rv-1").json()[
        "reservation_id"
    ]
    pid = plan(
        client,
        "p1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
        reservation_id=rid,
    ).json()["plan_id"]
    client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-1"}
    )
    resp = confirm(client, pid, "cf-1")
    assert resp.status_code == 409
    changed = {c["well"]: c for c in resp.json()["detail"]["changed_wells"]}
    assert changed["A1"]["reasons"] == ["reservations_changed"]
    # 计划未执行、批次未产生
    assert client.get("/batches").json()["batches"] == []
    assert client.get("/wells/A1").json()["volume"] == "100"


# ------------------------------------------------ 计划批次与撤销/查询


def test_committed_plan_batch_is_reversible(client):
    register(client, "A1", "S1", "100")
    pid = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "40"}]
    ).json()["plan_id"]
    cid = confirm(client, pid, "cf-1").json()["confirmation_id"]
    bid = client.get(f"/plans/{pid}").json()["committed_batch_id"]

    resp = client.post(
        f"/batches/{bid}/reversals", json={"request_key": "rev-1"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["batch"]["plan_confirmation_id"] == cid
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    # 计划/批次记录保留，计划仍标记 committed（无「退回待确认」语义）
    assert client.get(f"/plans/{pid}").json()["status"] == "committed"
    assert client.get(f"/batches/{bid}").json()["reversed"] is True


def test_plan_listing_and_well_history(client):
    register(client, "A1", "S1", "100")
    p1 = plan(
        client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).json()["plan_id"]
    p2 = plan(
        client, "p2", [{"source": "A1", "destination": "B2", "volume": "5"}]
    ).json()["plan_id"]
    confirm(client, p1, "cf-1")
    listing = client.get("/plans").json()["plans"]
    assert [p["plan_id"] for p in listing] == [p1, p2]
    statuses = {p["plan_id"]: p["status"] for p in listing}
    assert statuses == {p1: "committed", p2: "awaiting_confirmation"}
    # 待确认计划不出现在孔位批次史中（尚未写批次）
    history = client.get("/wells/A1/batches").json()["batches"]
    assert [b["request_key"] for b in history] == ["cf-1"]


def test_plan_preview_uses_decimal_quantum_precision(client):
    """预演与执行同一套最大余量法：量子级取液结果一致且不变量成立。"""
    for sample in ("S1", "S2", "S3"):
        register(client, "A1", sample, "0.000000001")
    resp = plan(
        client,
        "p-tiny",
        [{"source": "A1", "destination": "B1", "volume": "0.000000001"}],
    )
    assert resp.status_code == 201, resp.text
    moved = resp.json()["validation"]["steps"][0]["moved_composition"]
    assert moved == [{"sample": "S1", "amount": "0.000000001"}]
    assert confirm(client, resp.json()["plan_id"], "cf-1").status_code == 201
    src = client.get("/wells/A1").json()
    assert sum(amounts(src).values(), Decimal(0)) == Decimal(src["volume"])
    assert src["volume"] == "0.000000002"
    dst = client.get("/wells/B1").json()
    assert amounts(dst) == {"S1": Decimal("0.000000001")}


def test_awaiting_plan_does_not_block_other_operations(client):
    """待确认计划不预留液体、不阻塞补液、直接批次、容量变更、隔离或预留。"""
    register(client, "A1", "S1", "100")
    plan(client, "p1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    assert register(client, "A1", "S1", "10")["volume"] == "110"
    assert batch(
        client, "b1", [{"source": "A1", "destination": "C1", "volume": "5"}]
    ).status_code == 201
    assert client.put("/wells/A1/capacity", json={"max_capacity": "200"}).status_code == 200
    assert reserve(client, [{"well": "A1", "volume": "10"}], "rv-1").status_code == 201
    # 隔离不与计划互斥（计划本身不持有任何体积/孔锁）
    rid = 1
    client.post(
        f"/reservations/{rid}/cancellations", json={"request_key": "cancel-1"}
    )
    assert client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-1", "reason": "x", "wells": ["A1"]},
    ).status_code == 201
    # 上述变更使计划快照失效：确认返回 409，计划保持待确认
    resp = confirm(client, 1, "cf-1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "plan_stale"
    assert client.get("/plans/1").json()["status"] == "awaiting_confirmation"

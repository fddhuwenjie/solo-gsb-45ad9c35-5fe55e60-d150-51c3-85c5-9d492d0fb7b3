"""孔位隔离：申请、解除与对既有操作（补液/批次/容量/撤销）的限制。"""

import os
import sqlite3
from decimal import Decimal


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def quarantine(client, wells, case_id="CASE-1", reason="污染排查", request_key="q-1"):
    return client.post(
        "/quarantines",
        json={
            "request_key": request_key,
            "case_id": case_id,
            "reason": reason,
            "wells": wells,
        },
    )


def release(client, wells, case_id="CASE-1", request_key="rel-1"):
    return client.post(
        "/quarantines/releases",
        json={"request_key": request_key, "case_id": case_id, "wells": wells},
    )


def committed_batch_id(client, request_key, source, destination, volume):
    resp = client.post(
        "/batches",
        json={
            "request_key": request_key,
            "operations": [{"source": source, "destination": destination, "volume": volume}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


def reverse(client, batch_id, request_key):
    return client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": request_key}
    )


def db_count(sql):
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------ 隔离申请


def test_quarantine_create_and_queries_show_status(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = quarantine(client, ["A1", "A2"], case_id="CASE-2026-101", reason="疑似污染")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["case_id"] == "CASE-2026-101"
    assert body["reason"] == "疑似污染"
    assert [w["well"] for w in body["wells"]] == ["A1", "A2"]
    assert all(w["status"] == "quarantined" for w in body["wells"])
    assert all(w["released_at"] is None for w in body["wells"])

    # 孔位查询返回当前隔离信息
    well = client.get("/wells/A1").json()
    assert well["quarantine"]["case_id"] == "CASE-2026-101"
    assert well["quarantine"]["reason"] == "疑似污染"
    assert well["quarantine"]["request_key"] == "q-1"
    assert well["quarantine"]["quarantined_at"]
    assert well["volume"] == "100"  # 隔离不改变液体状态

    # 相关批次查询同样返回当前隔离信息
    batches = client.get("/wells/A1/batches").json()
    assert batches["quarantine"]["case_id"] == "CASE-2026-101"

    # 未隔离的孔 quarantine 为 null
    register(client, "B1", "S3", "10")
    assert client.get("/wells/B1").json()["quarantine"] is None

    # 审计查询可见申请记录
    listing = client.get("/quarantines").json()["quarantines"]
    assert [q["quarantine_id"] for q in listing] == [body["quarantine_id"]]


def test_quarantine_unregistered_well_is_404_and_no_partial_state(client):
    register(client, "A1", "S1", "100")
    resp = quarantine(client, ["A1", "Z9"])
    assert resp.status_code == 404, resp.text
    # 整次申请不留部分状态：A1 未隔离、无申请记录、request_key 未消耗
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/quarantines").json()["quarantines"] == []
    assert quarantine(client, ["A1"]).status_code == 201


def test_quarantine_conflict_leaves_no_partial_state(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    assert quarantine(client, ["A1"], case_id="CASE-1", request_key="q-1").status_code == 201

    # A1 已被其他案件隔离：整次申请拒绝，A2 不被隔离
    resp = quarantine(client, ["A1", "A2"], case_id="CASE-2", request_key="q-2")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_quarantined"
    assert detail["conflicting_wells"] == [{"well": "A1", "case_id": "CASE-1"}]
    assert client.get("/wells/A2").json()["quarantine"] is None
    assert len(client.get("/quarantines").json()["quarantines"]) == 1

    # 冲突不消耗 request_key：排除冲突孔后同键可重试
    assert quarantine(client, ["A2"], case_id="CASE-2", request_key="q-2").status_code == 201

    # 同一案件重复隔离同一孔同样视为冲突（孔须尚未隔离）
    resp = quarantine(client, ["A1"], case_id="CASE-1", request_key="q-3")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_quarantined"


def test_quarantine_idempotent_replay(client):
    register(client, "A1", "S1", "100")
    first = quarantine(client, ["A1"], request_key="q-idem")
    assert first.status_code == 201

    second = quarantine(client, ["A1"], request_key="q-idem")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["quarantine_id"] == first.json()["quarantine_id"]
    # 不会重复建立
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_quarantine_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    assert quarantine(client, ["A1"], case_id="CASE-1", request_key="q-key").status_code == 201

    # 同键换孔位
    resp = quarantine(client, ["A2"], case_id="CASE-1", request_key="q-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_case_id"] == "CASE-1"
    assert detail["existing_wells"] == ["A1"]

    # 同键换案件
    resp = quarantine(client, ["A1"], case_id="CASE-2", request_key="q-key")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"

    # 状态不变
    assert client.get("/wells/A2").json()["quarantine"] is None
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_quarantine_wells_normalized_and_deduped(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    # 小写、重复、乱序：归一化为排序去重后的目标
    resp = quarantine(client, ["a2", "A1", "A2"], request_key="q-norm")
    assert resp.status_code == 201
    assert [w["well"] for w in resp.json()["wells"]] == ["A1", "A2"]
    # 同键同目标（不同写法）重放
    replay = quarantine(client, ["A1", "a2"], request_key="q-norm")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_quarantine_validation_rejected(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, []).status_code == 422
    assert quarantine(client, ["A1"], reason="").status_code == 422
    assert quarantine(client, ["A1"], case_id="").status_code == 422
    assert quarantine(client, ["not-a-well"]).status_code == 422


# ------------------------------------------------ 隔离孔的操作限制


def test_quarantined_well_cannot_top_up(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, ["A1"]).status_code == 201

    resp = client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "10"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert detail["quarantined_wells"] == [{"well": "A1", "case_id": "CASE-1"}]
    # 液体不变
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_quarantined_well_rejected_as_batch_source_and_destination(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    assert quarantine(client, ["A1"]).status_code == 201

    # 作为来源
    resp = client.post(
        "/batches",
        json={
            "request_key": "bq-src",
            "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    assert resp.json()["detail"]["quarantined_wells"] == [
        {"well": "A1", "case_id": "CASE-1"}
    ]

    # 作为目标
    resp = client.post(
        "/batches",
        json={
            "request_key": "bq-dst",
            "operations": [{"source": "A2", "destination": "A1", "volume": "10"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"

    # 液体、批次均不变：不落失败批次、不消耗 request_key
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "60"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/batches").json()["batches"] == []
    assert db_count("SELECT COUNT(*) FROM batches") == 0


def test_quarantine_conflict_batch_key_reusable_after_release(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, ["A1"]).status_code == 201
    payload = {
        "request_key": "bq-retry",
        "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
    }
    assert client.post("/batches", json=payload).status_code == 409

    assert release(client, ["A1"]).status_code == 201
    # 解除后同键重试成功
    resp = client.post("/batches", json=payload)
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/B1").json()["volume"] == "10"


def test_batch_touching_quarantined_well_rolls_back_atomically(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    assert quarantine(client, ["A1"]).status_code == 201

    # 第一步本可成功，第二步触及隔离孔 -> 整批拒绝，第一步不留痕迹
    resp = client.post(
        "/batches",
        json={
            "request_key": "bq-atomic",
            "operations": [
                {"source": "A2", "destination": "C1", "volume": "10"},
                {"source": "A1", "destination": "C2", "volume": "10"},
            ],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    assert client.get("/wells/A2").json()["volume"] == "60"
    assert client.get("/wells/C1").status_code == 404
    assert client.get("/batches").json()["batches"] == []


def test_quarantined_well_capacity_change_blocked(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, ["A1"]).status_code == 201

    resp = client.put("/wells/A1/capacity", json={"max_capacity": "200"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert detail["quarantined_wells"] == [{"well": "A1", "case_id": "CASE-1"}]
    # 取消上限同样被禁止
    assert client.put("/wells/A1/capacity", json={"max_capacity": None}).status_code == 409
    # 配置不变
    assert client.get("/wells/A1").json()["max_capacity"] is None


def test_reversal_blocked_by_quarantined_well(client):
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "rq-1", "A1", "B1", "40")
    assert quarantine(client, ["B1"], case_id="CASE-9").status_code == 201

    resp = reverse(client, bid, "rev-q")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert detail["batch_id"] == bid
    assert detail["quarantined_wells"] == [{"well": "B1", "case_id": "CASE-9"}]

    # 液体与审计均不变
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get(f"/batches/{bid}").json()["reversed"] is False
    assert db_count("SELECT COUNT(*) FROM reversals") == 0

    # 解除隔离后同键撤销成功（冲突未消耗 request_key）
    assert release(client, ["B1"], case_id="CASE-9").status_code == 201
    assert reverse(client, bid, "rev-q").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"


# ------------------------------------------------ 解除隔离


def test_release_success_and_audit_retained(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    q = quarantine(client, ["A1", "A2"], case_id="CASE-1", request_key="q-1").json()

    resp = release(client, ["A1", "A2"], case_id="CASE-1", request_key="rel-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["case_id"] == "CASE-1"
    assert body["wells"] == [
        {"well": "A1", "quarantine_id": q["quarantine_id"]},
        {"well": "A2", "quarantine_id": q["quarantine_id"]},
    ]

    # 孔位查询不再返回隔离信息
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/A2/batches").json()["quarantine"] is None

    # 申请与解除审计均保留
    stored = client.get("/quarantines").json()["quarantines"][0]
    assert stored["quarantine_id"] == q["quarantine_id"]
    assert [w["status"] for w in stored["wells"]] == ["released", "released"]
    assert all(w["released_at"] for w in stored["wells"])
    releases = client.get("/quarantines/releases").json()["releases"]
    assert [r["release_id"] for r in releases] == [body["release_id"]]

    # 解除后孔位恢复可用：补液、批次、容量、撤销照常
    assert register(client, "A1", "S1", "10")["volume"] == "110"
    assert client.put("/wells/A1/capacity", json={"max_capacity": "200"}).status_code == 200


def test_release_idempotent_replay(client):
    register(client, "A1", "S1", "100")
    quarantine(client, ["A1"], request_key="q-1")
    first = release(client, ["A1"], request_key="rel-idem")
    assert first.status_code == 201

    second = release(client, ["A1"], request_key="rel-idem")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["release_id"] == first.json()["release_id"]
    assert len(client.get("/quarantines/releases").json()["releases"]) == 1


def test_release_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    quarantine(client, ["A1", "A2"], request_key="q-1")
    assert release(client, ["A1"], request_key="rel-key").status_code == 201

    # 同键换孔位
    resp = release(client, ["A2"], request_key="rel-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_wells"] == ["A1"]
    # 同键换案件
    assert release(client, ["A1"], case_id="CASE-2", request_key="rel-key").status_code == 409
    # A2 仍隔离
    assert client.get("/wells/A2").json()["quarantine"]["case_id"] == "CASE-1"


def test_release_mismatch_rejected_atomically(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    quarantine(client, ["A1"], case_id="CASE-1", request_key="q-1")
    quarantine(client, ["A2"], case_id="CASE-2", request_key="q-2")

    # A2 的隔离案件是 CASE-2，与请求不符 -> 整次拒绝，A1 不解除
    resp = release(client, ["A1", "A2"], case_id="CASE-1", request_key="rel-x")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "release_mismatch"
    assert detail["mismatched_wells"] == [
        {"well": "A2", "reason": "case_mismatch", "case_id": "CASE-2"}
    ]
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"
    assert client.get("/quarantines/releases").json()["releases"] == []

    # 未隔离的孔（含未登记孔）-> not_quarantined，同样整次拒绝
    resp = release(client, ["A1", "B1"], case_id="CASE-1", request_key="rel-y")
    assert resp.status_code == 409
    assert resp.json()["detail"]["mismatched_wells"] == [
        {"well": "B1", "reason": "not_quarantined"}
    ]
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"

    # 冲突不消耗 request_key：修正后同键成功
    assert release(client, ["A1"], case_id="CASE-1", request_key="rel-x").status_code == 201
    assert release(client, ["A2"], case_id="CASE-2", request_key="rel-y").status_code == 201


def test_requarantine_after_release(client):
    register(client, "A1", "S1", "100")
    quarantine(client, ["A1"], case_id="CASE-1", request_key="q-1")
    assert release(client, ["A1"], case_id="CASE-1", request_key="rel-1").status_code == 201

    # 同一孔可被其他案件重新隔离
    resp = quarantine(client, ["A1"], case_id="CASE-2", request_key="q-2")
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-2"
    # 两次申请记录都在
    assert len(client.get("/quarantines").json()["quarantines"]) == 2


def test_quarantine_replay_after_release_does_not_reapply(client):
    register(client, "A1", "S1", "100")
    quarantine(client, ["A1"], case_id="CASE-1", request_key="q-1")
    assert release(client, ["A1"], case_id="CASE-1", request_key="rel-1").status_code == 201

    # 同键同目标重放：返回首次申请结果，不会重新隔离
    replay = quarantine(client, ["A1"], case_id="CASE-1", request_key="q-1")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert client.get("/wells/A1").json()["quarantine"] is None


def test_release_replay_after_requarantine(client):
    register(client, "A1", "S1", "100")
    quarantine(client, ["A1"], case_id="CASE-1", request_key="q-1")
    release(client, ["A1"], case_id="CASE-1", request_key="rel-1")
    # A1 被另一案件重新隔离
    quarantine(client, ["A1"], case_id="CASE-2", request_key="q-2")

    # 旧解除键同目标重放：按重放处理，不误报不匹配、不影响新隔离
    replay = release(client, ["A1"], case_id="CASE-1", request_key="rel-1")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-2"


def test_quarantine_does_not_change_liquid_or_decimal_state(client):
    register(client, "A1", "S1", "0.000000005")
    register(client, "A1", "S2", "0.000000003")
    assert quarantine(client, ["A1"]).status_code == 201
    body = client.get("/wells/A1").json()
    assert body["volume"] == "0.000000008"
    amounts = {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}
    assert amounts == {"S1": Decimal("0.000000005"), "S2": Decimal("0.000000003")}
    assert release(client, ["A1"]).status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "0.000000008"

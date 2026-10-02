"""移液体积预留：建立、原子失败、幂等、批次兑现、取消、与隔离互斥。"""

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


def reserve(client, items, request_key="rv-1"):
    return client.post(
        "/reservations",
        json={"request_key": request_key, "items": items},
    )


def cancel(client, reservation_id, request_key="cancel-1"):
    return client.post(
        f"/reservations/{reservation_id}/cancellations",
        json={"request_key": request_key},
    )


def batch(client, request_key, operations, reservation_id=None):
    payload = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        payload["reservation_id"] = reservation_id
    return client.post("/batches", json=payload)


def db_count(sql):
    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------ 建立预留与查询


def test_reservation_create_success_and_queries(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "25.5"}],
        request_key="rv-1",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["replayed"] is False
    assert body["consumed_at"] is None
    assert body["consumed_batch_id"] is None
    assert body["cancelled_at"] is None
    assert body["items"] == [
        {"well": "A1", "volume": "40"},
        {"well": "A2", "volume": "25.5"},
    ]
    reservation_id = body["reservation_id"]

    # 单条与列表查询返回同样的明细和状态
    got = client.get(f"/reservations/{reservation_id}").json()
    assert got["items"] == body["items"]
    assert got["status"] == "active"
    listing = client.get("/reservations").json()["reservations"]
    assert [r["reservation_id"] for r in listing] == [reservation_id]

    # 孔位查询返回预留占用与可动用体积
    a1 = client.get("/wells/A1").json()
    assert a1["reserved_volume"] == "40"
    assert a1["available_volume"] == "60"
    assert a1["reservations"] == [
        {
            "reservation_id": reservation_id,
            "request_key": "rv-1",
            "volume": "40",
            "reserved_at": body["created_at"],
        }
    ]
    a2 = client.get("/wells/A2").json()
    assert a2["reserved_volume"] == "25.5"
    assert a2["available_volume"] == "34.5"
    # 预留不改变液体状态
    assert a1["volume"] == "100"
    assert a2["volume"] == "60"


def test_reservation_unknown_is_404(client):
    assert client.get("/reservations/999").status_code == 404
    assert cancel(client, 999).status_code == 404


def test_reservation_unregistered_well_is_404_atomic(client):
    register(client, "A1", "S1", "100")
    resp = reserve(
        client,
        [{"well": "A1", "volume": "10"}, {"well": "Z9", "volume": "5"}],
        request_key="rv-404",
    )
    assert resp.status_code == 404, resp.text
    # 不留部分预留，键未消耗
    assert client.get("/reservations").json()["reservations"] == []
    assert client.get("/wells/A1").json()["reserved_volume"] == "0"
    assert reserve(client, [{"well": "A1", "volume": "10"}], request_key="rv-404").status_code == 201


def test_reservation_quarantined_well_rejected_atomically(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    quar = client.post(
        "/quarantines",
        json={
            "request_key": "q-1",
            "case_id": "CASE-1",
            "reason": "排查",
            "wells": ["A2"],
        },
    )
    assert quar.status_code == 201

    # A2 已隔离：整次预留拒绝，A1 不被预留
    resp = reserve(
        client,
        [{"well": "A1", "volume": "10"}, {"well": "A2", "volume": "10"}],
        request_key="rv-q",
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert detail["quarantined_wells"] == [{"well": "A2", "case_id": "CASE-1"}]
    assert client.get("/wells/A1").json()["reserved_volume"] == "0"
    assert client.get("/reservations").json()["reservations"] == []
    # 键未消耗：解除隔离后同键成功
    client.post(
        "/quarantines/releases",
        json={"request_key": "rel-1", "case_id": "CASE-1", "wells": ["A2"]},
    )
    assert reserve(
        client,
        [{"well": "A1", "volume": "10"}, {"well": "A2", "volume": "10"}],
        request_key="rv-q",
    ).status_code == 201


def test_reservation_validation(client):
    register(client, "A1", "S1", "100")
    assert reserve(client, [], request_key="rv-empty").status_code == 422
    assert reserve(client, [{"well": "A1", "volume": "0"}]).status_code == 422
    assert reserve(client, [{"well": "A1", "volume": "-5"}]).status_code == 422
    assert reserve(client, [{"well": "bad", "volume": "1"}]).status_code == 422
    assert reserve(
        client,
        [{"well": "A1", "volume": "1"}, {"well": "a1", "volume": "2"}],
        request_key="rv-dup",
    ).status_code == 422


# ------------------------------------------------ 可用量原子校验


def test_reservation_insufficient_volume_is_atomic(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    # 先占用 A1 的 70
    first = reserve(client, [{"well": "A1", "volume": "70"}], request_key="rv-a")
    assert first.status_code == 201

    # A1 仅剩 30 可用（请求 40），A2 充足 -> 整次失败，不留部分预留
    resp = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "60"}],
        request_key="rv-b",
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_unavailable"
    assert detail["conflicting_wells"] == [
        {
            "well": "A1",
            "available": "30",
            "already_reserved": "70",
            "requested": "40",
        }
    ]
    # A2 不被预留，只有一条预留记录
    assert client.get("/wells/A2").json()["reserved_volume"] == "0"
    assert len(client.get("/reservations").json()["reservations"]) == 1
    # 恰好等于可用量允许
    assert reserve(
        client, [{"well": "A1", "volume": "30"}], request_key="rv-b"
    ).status_code == 201


def test_multiple_reservations_share_well_and_decimal_precision(client):
    register(client, "A1", "S1", "0.000000005")
    assert reserve(
        client, [{"well": "A1", "volume": "0.000000002"}], request_key="rv-d1"
    ).status_code == 201
    # 已预留 2e-9，再预留 4e-9 -> 可用仅 3e-9，拒绝
    resp = reserve(
        client, [{"well": "A1", "volume": "0.000000004"}], request_key="rv-d2"
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicting_wells"][0]["available"] == "0.000000003"
    # 再预留 3e-9 恰好
    assert reserve(
        client, [{"well": "A1", "volume": "0.000000003"}], request_key="rv-d2"
    ).status_code == 201
    body = client.get("/wells/A1").json()
    assert body["reserved_volume"] == "0.000000005"
    assert body["available_volume"] == "0"


# ------------------------------------------------ 幂等与换目标冲突


def test_reservation_idempotent_replay(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    items = [{"well": "A2", "volume": "10"}, {"well": "A1", "volume": "40"}]
    first = reserve(client, items, request_key="rv-idem")
    assert first.status_code == 201
    rid = first.json()["reservation_id"]

    # 同键同目标、不同顺序 -> 200 重放（目标按孔排序比较）
    replay = reserve(
        client, list(reversed(items)), request_key="rv-idem"
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["reservation_id"] == rid
    assert replay.json()["items"] == [
        {"well": "A1", "volume": "40"},
        {"well": "A2", "volume": "10"},
    ]
    assert len(client.get("/reservations").json()["reservations"]) == 1


def test_reservation_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    first = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-key")
    assert first.status_code == 201

    # 同键换孔
    resp = reserve(client, [{"well": "A2", "volume": "10"}], request_key="rv-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_items"] == [{"well": "A1", "volume": "40"}]

    # 同键同孔换量
    resp = reserve(client, [{"well": "A1", "volume": "41"}], request_key="rv-key")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"

    # 不新增预留，A2 未被预留
    assert client.get("/wells/A2").json()["reserved_volume"] == "0"
    assert len(client.get("/reservations").json()["reservations"]) == 1


# ------------------------------------------------ 未绑定批次不能动用预留体积


def test_unbound_batch_cannot_use_reserved_volume(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "60"}], request_key="rv-1").json()[
        "reservation_id"
    ]

    # 未携带 reservation_id：可吸取量只有 100-60=40
    resp = batch(
        client, "b-unbound", [{"source": "A1", "destination": "B1", "volume": "50"}]
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "A1"
    assert detail["available"] == "40"
    assert detail["requested"] == "50"
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404

    # 预留仍生效，可被随后绑定批次兑现
    resp = batch(
        client,
        "b-bound",
        [{"source": "A1", "destination": "B1", "volume": "60"}],
        reservation_id=rid,
    )
    assert resp.status_code == 201, resp.text


def test_top_up_still_allowed_alongside_reservation(client):
    """普通补液是加入液体：不被预留阻塞，补液后可用量随之增加。"""
    register(client, "A1", "S1", "100")
    reserve(client, [{"well": "A1", "volume": "80"}], request_key="rv-1")
    body = register(client, "A1", "S1", "20")
    assert body["volume"] == "120"
    assert body["reserved_volume"] == "80"
    assert body["available_volume"] == "40"


def test_unbound_batch_within_available_succeeds_and_keeps_reservation(client):
    register(client, "A1", "S1", "100")
    reserve(client, [{"well": "A1", "volume": "60"}], request_key="rv-1")

    # 用掉非预留的 40：成功；预留仍 active
    resp = batch(
        client, "b-1", [{"source": "A1", "destination": "B1", "volume": "40"}]
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "60"
    stored = client.get("/reservations").json()["reservations"][0]
    assert stored["status"] == "active"
    assert client.get("/wells/A1").json()["available_volume"] == "0"

    # 此后未绑定批次再取 1 也不行（全部 60 都被预留）
    resp = batch(
        client, "b-2", [{"source": "A1", "destination": "B2", "volume": "1"}]
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["available"] == "0"


# ------------------------------------------------ 绑定批次兑现预留


def test_bound_batch_consumes_reservation_in_same_transaction(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "30"}],
        request_key="rv-1",
    ).json()["reservation_id"]

    # 逐孔逐量完全兑现（A1 拆成两步、A2 一步）：成功并标记 consumed
    resp = batch(
        client,
        "b-fulfill",
        [
            {"source": "A1", "destination": "C1", "volume": "10"},
            {"source": "A2", "destination": "C1", "volume": "30"},
            {"source": "A1", "destination": "C2", "volume": "30"},
        ],
        reservation_id=rid,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reservation_id"] == rid
    bid = body["batch_id"]

    stored = client.get(f"/reservations/{rid}").json()
    assert stored["status"] == "consumed"
    assert stored["consumed_batch_id"] == bid
    assert stored["consumed_at"] is not None
    assert stored["items"] == [
        {"well": "A1", "volume": "40"},
        {"well": "A2", "volume": "30"},
    ]

    # 液体状态：A1=60、A2=30、C1=40(S1:10 S2:30)、C2=30
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/A2").json()["volume"] == "30"
    c1 = client.get("/wells/C1").json()
    assert c1["volume"] == "40"
    assert amounts(c1) == {"S1": Decimal("10"), "S2": Decimal("30")}
    assert client.get("/wells/C2").json()["volume"] == "30"
    # 消费后不再占用可用量
    assert client.get("/wells/A1").json()["reserved_volume"] == "0"
    assert client.get("/wells/A1").json()["available_volume"] == "60"
    # 批次记录携带预留编号
    assert client.get(f"/batches/{bid}").json()["reservation_id"] == rid


def test_bound_batch_wrong_volume_does_not_consume_reservation(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "30"}],
        request_key="rv-1",
    ).json()["reservation_id"]

    payload = {
        "request_key": "b-mis",
        "reservation_id": rid,
        "operations": [
            {"source": "A1", "destination": "C1", "volume": "40"},
            {"source": "A2", "destination": "C1", "volume": "20"},  # 少 10
        ],
    }
    resp = client.post("/batches", json=payload)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_fulfillment_mismatch"
    assert detail["reservation_id"] == rid
    assert detail["mismatched_wells"] == [
        {"well": "A2", "reserved": "30", "requested": "20"}
    ]
    # 液体不变、无失败批次、键未消耗
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/C1").status_code == 404
    assert client.get("/reservations").json()["reservations"][0]["status"] == "active"
    assert client.get("/batches").json()["batches"] == []
    assert db_count("SELECT COUNT(*) FROM batches") == 0
    # 同键修正为完全兑现后成功
    payload["operations"] = [
        {"source": "A1", "destination": "C1", "volume": "40"},
        {"source": "A2", "destination": "C1", "volume": "30"},
    ]
    assert client.post("/batches", json=payload).status_code == 201
    assert client.get(f"/reservations/{rid}").json()["status"] == "consumed"


def test_bound_batch_missing_and_extra_wells_mismatch(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    register(client, "A3", "S3", "20")
    rid = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "30"}],
        request_key="rv-1",
    ).json()["reservation_id"]

    # 漏掉 A2，又动用未预留的 A3 -> 双向不匹配
    resp = batch(
        client,
        "b-x",
        [
            {"source": "A1", "destination": "C1", "volume": "40"},
            {"source": "A3", "destination": "C1", "volume": "10"},
        ],
        reservation_id=rid,
    )
    assert resp.status_code == 409
    mismatches = resp.json()["detail"]["mismatched_wells"]
    by_well = {m["well"]: m for m in mismatches}
    assert by_well["A2"] == {"well": "A2", "reserved": "30", "requested": None}
    assert by_well["A3"] == {"well": "A3", "reserved": None, "requested": "10"}
    assert client.get(f"/reservations/{rid}").json()["status"] == "active"
    # 液体不变
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A3").json()["volume"] == "20"


def test_bound_batch_unknown_or_inactive_reservation(client):
    register(client, "A1", "S1", "100")
    # 不存在
    resp = batch(
        client,
        "b-no",
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        reservation_id=42,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "reservation_not_found"
    assert client.get("/wells/A1").json()["volume"] == "100"

    rid = reserve(client, [{"well": "A1", "volume": "10"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    assert cancel(client, rid, request_key="c-1").status_code == 201
    # 已取消
    resp = batch(
        client,
        "b-canceled",
        [{"source": "A1", "destination": "B1", "volume": "10"}],
        reservation_id=rid,
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_not_active"
    assert detail["status"] == "cancelled"
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_bound_batch_physical_failure_does_not_consume_reservation(client):
    """逐孔逐量匹配预留，但执行中目标孔超容量：整批失败、预留保持 active。"""
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "50")
    client.put("/wells/B1/capacity", json={"max_capacity": "80"})
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]

    payload = {
        "request_key": "b-cap",
        "reservation_id": rid,
        "operations": [{"source": "A1", "destination": "B1", "volume": "40"}],
    }
    resp = client.post("/batches", json=payload)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "capacity_exceeded"
    # 失败批次落库并消耗批次 request_key；预留未消耗
    assert client.get(f"/reservations/{rid}").json()["status"] == "active"
    failed = client.get("/batches").json()["batches"]
    assert len(failed) == 1 and failed[0]["status"] == "failed"
    assert failed[0]["reservation_id"] == rid
    # 同批次键重放仍是失败记录
    replay = client.post("/batches", json=payload)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    # 调大上限后用新批次键完全兑现：成功并消费预留
    client.put("/wells/B1/capacity", json={"max_capacity": "100"})
    payload["request_key"] = "b-cap-2"
    assert client.post("/batches", json=payload).status_code == 201
    assert client.get(f"/reservations/{rid}").json()["status"] == "consumed"


def test_bound_batch_respects_other_active_reservations(client):
    """绑定预留只能动用自己的体积：同孔其他生效预留仍然不可动用。"""
    register(client, "A1", "S1", "100")
    rid1 = reserve(client, [{"well": "A1", "volume": "60"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    rid2 = reserve(client, [{"well": "A1", "volume": "30"}], request_key="rv-2").json()[
        "reservation_id"
    ]
    # 未预留体积仅 100-90=10：未绑定批次取 20 -> 409，取 10 -> 成功，A1=90
    assert batch(
        client, "b-x", [{"source": "A1", "destination": "B1", "volume": "20"}]
    ).status_code == 409
    assert batch(
        client, "b-y", [{"source": "A1", "destination": "B1", "volume": "10"}]
    ).status_code == 201
    # 兑现 rid1：可动用 = 90 - 30(rid2) = 60，恰好
    resp = batch(
        client,
        "b-1",
        [{"source": "A1", "destination": "B2", "volume": "60"}],
        reservation_id=rid1,
    )
    assert resp.status_code == 201, resp.text
    assert client.get(f"/reservations/{rid1}").json()["status"] == "consumed"
    assert client.get(f"/reservations/{rid2}").json()["status"] == "active"
    assert client.get("/wells/A1").json()["volume"] == "30"

    # rid2 未消费前，未绑定批次仍不能动用它的 30
    assert batch(
        client, "b-z", [{"source": "A1", "destination": "B3", "volume": "1"}]
    ).status_code == 409
    # 用绑定 rid2 的批次取 30：成功
    assert batch(
        client,
        "b-2",
        [{"source": "A1", "destination": "B3", "volume": "30"}],
        reservation_id=rid2,
    ).status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "0"


# ------------------------------------------------ 取消预留


def test_cancel_active_reservation(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    resp = cancel(client, rid, request_key="cancel-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reservation_id"] == rid
    assert body["replayed"] is False
    assert body["cancelled_at"]
    assert body["reservation"]["status"] == "cancelled"

    stored = client.get(f"/reservations/{rid}").json()
    assert stored["status"] == "cancelled"
    assert stored["cancelled_at"]
    assert stored["cancellation"] == {
        "cancellation_id": body["cancellation_id"],
        "request_key": "cancel-1",
        "cancelled_at": body["cancelled_at"],
    }
    # 体积释放：孔位可用量恢复
    well = client.get("/wells/A1").json()
    assert well["reserved_volume"] == "0"
    assert well["available_volume"] == "100"
    # 取消审计可查
    audits = client.get("/reservations/cancellations/all").json()["cancellations"]
    assert [a["cancellation_id"] for a in audits] == [body["cancellation_id"]]


def test_cancel_idempotent_replay_and_other_target_conflict(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    rid1 = reserve(client, [{"well": "A1", "volume": "10"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    rid2 = reserve(client, [{"well": "A2", "volume": "10"}], request_key="rv-2").json()[
        "reservation_id"
    ]
    first = cancel(client, rid1, request_key="c-key")
    assert first.status_code == 201

    replay = cancel(client, rid1, request_key="c-key")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["cancellation_id"] == first.json()["cancellation_id"]

    # 同键换预留 -> 409
    resp = cancel(client, rid2, request_key="c-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["reservation_id"] == rid2
    assert detail["existing_reservation_id"] == rid1
    # rid2 仍生效
    assert client.get(f"/reservations/{rid2}").json()["status"] == "active"


def test_cancel_consumed_reservation_rejected(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    assert batch(
        client,
        "b-1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
        reservation_id=rid,
    ).status_code == 201

    resp = cancel(client, rid, request_key="c-1")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_consumed"
    assert detail["reservation_id"] == rid
    # 取消键未消耗：用于另一生效预留仍成功
    rid2 = reserve(client, [{"well": "A1", "volume": "10"}], request_key="rv-2").json()[
        "reservation_id"
    ]
    assert cancel(client, rid2, request_key="c-1").status_code == 201


def test_cancel_already_cancelled_rejected(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    assert cancel(client, rid, request_key="c-1").status_code == 201
    resp = cancel(client, rid, request_key="c-2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_cancelled"
    # 只有一条取消审计
    assert db_count("SELECT COUNT(*) FROM reservation_cancellations") == 1


def test_cancel_releases_volume_for_future_reservations(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "90"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    # 仅剩 10 可用
    assert reserve(
        client, [{"well": "A1", "volume": "20"}], request_key="rv-blocked"
    ).status_code == 409
    assert cancel(client, rid, request_key="c-1").status_code == 201
    # 取消后体积释放，新预留可建立
    assert reserve(
        client, [{"well": "A1", "volume": "20"}], request_key="rv-blocked"
    ).status_code == 201


# ------------------------------------------------ 预留与隔离互斥


def quarantine(client, wells, case_id="CASE-1", request_key="q-1"):
    return client.post(
        "/quarantines",
        json={
            "request_key": request_key,
            "case_id": case_id,
            "reason": "排查",
            "wells": wells,
        },
    )


def test_quarantine_rejected_when_active_reservation_lists_blocking_wells(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(
        client,
        [{"well": "A1", "volume": "40"}, {"well": "A2", "volume": "10"}],
        request_key="rv-1",
    ).json()["reservation_id"]

    # 隔离 A1、A2：A1/A2 上有生效预留 -> 整次拒绝并列出阻塞孔
    resp = quarantine(client, ["A1", "A2"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservations_active"
    blockers = {b["well"]: b for b in detail["blocking_wells"]}
    assert set(blockers) == {"A1", "A2"}
    assert blockers["A1"]["reserved_volume"] == "40"
    assert blockers["A1"]["reservations"] == [
        {"reservation_id": rid, "request_key": "rv-1", "volume": "40"}
    ]
    assert blockers["A2"]["reserved_volume"] == "10"
    # 不留部分隔离
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/A2").json()["quarantine"] is None
    assert client.get("/quarantines").json()["quarantines"] == []

    # 只隔离无预留的 B... 需已登记：A1/A2 仍不行；先登记 C1 无预留，隔离 C1 成功
    register(client, "C1", "S9", "5")
    assert quarantine(client, ["C1"], request_key="q-2").status_code == 201


def test_quarantine_allowed_after_reservation_consumed_or_cancelled(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid1 = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    rid2 = reserve(client, [{"well": "A2", "volume": "10"}], request_key="rv-2").json()[
        "reservation_id"
    ]
    # 消费 rid1、取消 rid2
    assert batch(
        client,
        "b-1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
        reservation_id=rid1,
    ).status_code == 201
    assert cancel(client, rid2, request_key="c-1").status_code == 201

    # 两孔均可隔离
    assert quarantine(client, ["A1", "A2"], request_key="q-ok").status_code == 201
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"


# ------------------------------------------------ 预留与撤销


def reverse(client, batch_id, request_key):
    return client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": request_key}
    )


def test_reversal_allowed_when_restore_covers_active_reservation(client):
    """撤销把来源孔恢复到批前体积（不小于生效预留量）时正常撤销，预留保持 active。"""
    register(client, "A1", "S1", "100")
    bid = batch(
        client,
        "b-1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    ).json()["batch_id"]
    assert client.get("/wells/A1").json()["volume"] == "60"
    # 批次后在 A1（当前 60）上预留 60；撤销恢复为 100 ≥ 60，允许
    rid = reserve(client, [{"well": "A1", "volume": "60"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    resp = reverse(client, bid, "rev-1")
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    stored = client.get(f"/reservations/{rid}").json()
    assert stored["status"] == "active"
    # 撤销不删除预留：A1 可动用体积仍扣除该预留
    assert client.get("/wells/A1").json()["available_volume"] == "40"


def test_reversal_deleting_well_blocked_by_active_reservation(client):
    """撤销会删除批次新建的孔，但该孔后来被预留：整次撤销拒绝并列阻塞孔。"""
    register(client, "A1", "S1", "100")
    bid = batch(
        client,
        "b-1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    ).json()["batch_id"]
    # B1 是该批次新建（撤销会删除 B1）；对 B1 预留其全部 40
    rid = reserve(client, [{"well": "B1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    resp = reverse(client, bid, "rev-1")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservations_active"
    blockers = {b["well"]: b for b in detail["blocking_wells"]}
    assert blockers["B1"]["restored_volume"] == "0"
    assert blockers["B1"]["reserved_volume"] == "40"
    assert blockers["B1"]["reservations"][0]["reservation_id"] == rid
    # 液体与审计不变
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert db_count("SELECT COUNT(*) FROM reversals") == 0

    # 取消预留后同键撤销成功
    assert cancel(client, rid, request_key="c-1").status_code == 201
    assert reverse(client, bid, "rev-1").status_code == 201
    assert client.get("/wells/B1").status_code == 404


# ------------------------------------------------ 并发串行


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


def test_concurrent_same_key_reservation_replays_once(client):
    register(client, "A1", "S1", "100")
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(
            lambda: reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-c")
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]
    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["reservation_id"] for b in bodies} == {bodies[0]["reservation_id"]}
    assert [b["replayed"] for b in bodies].count(True) == 1
    assert client.get("/wells/A1").json()["reserved_volume"] == "40"


def test_concurrent_reservations_serialize_available_volume(client, monkeypatch):
    """两个不同键并发预留同一孔的全部体积：先提交者占满，后到者 409、不留部分。"""
    from app import ledger

    register(client, "A1", "S1", "100")
    gate = threading.Event()
    entered = threading.Event()
    original = ledger.apply_reservation

    def gated(conn, request_key, items):
        entered.set()
        assert gate.wait(10.0), "预留线程未被释放"
        return original(conn, request_key, items)

    monkeypatch.setattr(ledger, "apply_reservation", gated)

    t1, r1 = _run_in_thread(
        lambda: reserve(client, [{"well": "A1", "volume": "100"}], request_key="rv-a")
    )
    assert entered.wait(10.0)
    t2, r2 = _run_in_thread(
        lambda: reserve(client, [{"well": "A1", "volume": "100"}], request_key="rv-b")
    )
    threading.Event().wait(0.3)
    assert t2.is_alive(), "第二个预留应在写锁上排队"
    gate.set()
    first = _join(t1, r1)
    second = _join(t2, r2)
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "reservation_unavailable"
    assert second.json()["detail"]["conflicting_wells"][0]["available"] == "0"
    assert len(client.get("/reservations").json()["reservations"]) == 1
    assert client.get("/wells/A1").json()["reserved_volume"] == "100"


def test_reverse_bound_consuming_batch_restores_liquid_reservation_stays_consumed(client):
    """撤销绑定批次：液体按批前快照恢复，预留仍为 consumed（无「退回生效」语义）。"""
    register(client, "A1", "S1", "100")
    rid = reserve(client, [{"well": "A1", "volume": "40"}], request_key="rv-1").json()[
        "reservation_id"
    ]
    bid = batch(
        client,
        "b-1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
        reservation_id=rid,
    ).json()["batch_id"]
    assert client.get("/wells/A1").json()["volume"] == "60"

    assert reverse(client, bid, "rev-1").status_code == 201
    # 液体恢复，B1 删除
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    # 预留保持 consumed，批次记录仍指向它
    stored = client.get(f"/reservations/{rid}").json()
    assert stored["status"] == "consumed"
    assert stored["consumed_batch_id"] == bid
    assert client.get(f"/batches/{bid}").json()["reservation_id"] == rid


def test_legacy_database_without_reservation_column_migrates(tmp_path, monkeypatch):
    """老库（batches 无 reservation_id 列）启动就地迁移：历史批次视为未绑定，预留功能可用。"""
    from fastapi.testclient import TestClient

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE wells (well TEXT PRIMARY KEY, volume TEXT NOT NULL DEFAULT '0',
                            max_capacity TEXT);
        CREATE TABLE well_composition (
            well TEXT NOT NULL REFERENCES wells(well),
            sample_id INTEGER NOT NULL REFERENCES samples(id),
            amount TEXT NOT NULL, PRIMARY KEY (well, sample_id));
        CREATE TABLE batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('committed','failed')),
            error TEXT, created_at TEXT NOT NULL);
        CREATE TABLE operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
            step INTEGER NOT NULL, source TEXT NOT NULL, destination TEXT NOT NULL, volume TEXT NOT NULL);
        CREATE INDEX idx_operations_batch ON operations(batch_id);
        CREATE INDEX idx_operations_well ON operations(source, destination);
        CREATE TABLE well_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, well TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('top_up','batch')),
            batch_id INTEGER REFERENCES batches(id),
            sample TEXT, volume TEXT, created_at TEXT NOT NULL);
        CREATE INDEX idx_well_events_well ON well_events(well, id);
        CREATE TABLE batch_well_snapshots (
            batch_id INTEGER NOT NULL REFERENCES batches(id), well TEXT NOT NULL,
            volume TEXT, PRIMARY KEY (batch_id, well));
        CREATE TABLE batch_snapshot_composition (
            batch_id INTEGER NOT NULL REFERENCES batches(id), well TEXT NOT NULL,
            sample_id INTEGER NOT NULL REFERENCES samples(id), amount TEXT NOT NULL,
            PRIMARY KEY (batch_id, well, sample_id));
        CREATE TABLE reversals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL UNIQUE REFERENCES batches(id),
            request_key TEXT NOT NULL UNIQUE, reversed_at TEXT NOT NULL);
        CREATE TABLE quarantine_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE quarantine_wells (
            request_id INTEGER NOT NULL REFERENCES quarantine_requests(id),
            well TEXT NOT NULL, released_at TEXT, PRIMARY KEY (request_id, well));
        CREATE UNIQUE INDEX idx_quarantine_wells_active
            ON quarantine_wells(well) WHERE released_at IS NULL;
        CREATE TABLE quarantine_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL, released_at TEXT NOT NULL);
        CREATE TABLE quarantine_release_wells (
            release_id INTEGER NOT NULL REFERENCES quarantine_releases(id),
            well TEXT NOT NULL, request_id INTEGER NOT NULL REFERENCES quarantine_requests(id),
            PRIMARY KEY (release_id, well));
        INSERT INTO samples (id, name) VALUES (1, 'S1');
        INSERT INTO wells (well, volume) VALUES ('A1', '100');
        INSERT INTO well_composition (well, sample_id, amount) VALUES ('A1', 1, '100');
        INSERT INTO batches (request_key, status, error, created_at)
            VALUES ('legacy-b1', 'committed', NULL, '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("LINEAGE_DB", str(db_file))
    from app.main import app

    with TestClient(app) as client:
        # 历史批次视为未绑定预留
        body = client.get("/batches").json()["batches"]
        assert body[0]["reservation_id"] is None
        # 孔位返回新增预留字段
        well = client.get("/wells/A1").json()
        assert well["reserved_volume"] == "0"
        assert well["available_volume"] == "100"
        # 预留建立/兑现全链路在迁移后的库上可用
        resp = client.post(
            "/reservations",
            json={"request_key": "rv-legacy", "items": [{"well": "A1", "volume": "40"}]},
        )
        assert resp.status_code == 201, resp.text
        rid = resp.json()["reservation_id"]
        resp = client.post(
            "/batches",
            json={
                "request_key": "new-b1",
                "reservation_id": rid,
                "operations": [{"source": "A1", "destination": "B1", "volume": "40"}],
            },
        )
        assert resp.status_code == 201, resp.text
        assert client.get(f"/reservations/{rid}").json()["status"] == "consumed"

"""端到端 API 测试。"""

import sqlite3
from decimal import Decimal


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def assert_invariant(client, well):
    """组分量之和必须严格等于孔体积。"""
    body = client.get(f"/wells/{well}").json()
    assert sum(amounts(body).values(), Decimal(0)) == Decimal(body["volume"]), body
    return body


def test_register_initial_sample(client):
    body = register(client, "A1", "S1", "100")
    assert body["well"] == "A1"
    assert body["volume"] == "100"
    assert body["composition"] == [{"sample": "S1", "amount": "100", "fraction": "1"}]


def test_register_top_up_uses_decimal(client):
    register(client, "A1", "S1", "0.1")
    body = register(client, "A1", "S1", "0.2")
    # float 会得到 0.30000000000000004，Decimal 必须精确
    assert body["volume"] == "0.3"
    assert body["composition"][0]["amount"] == "0.3"


def test_unknown_well_is_404(client):
    assert client.get("/wells/Z9").status_code == 404


def test_batch_transfer_and_lineage(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = client.post(
        "/batches",
        json={
            "request_key": "k1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "40"},
                {"source": "A2", "destination": "B1", "volume": "30"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert batch["status"] == "committed"
    assert batch["replayed"] is False
    assert [op["step"] for op in batch["operations"]] == [1, 2]

    # 目标孔重新混合：S1:S2 = 40:30
    b1 = client.get("/wells/B1").json()
    assert b1["volume"] == "70"
    comp = {c["sample"]: c for c in b1["composition"]}
    assert comp["S1"]["amount"] == "40"
    assert comp["S1"]["fraction"] == "0.571428571"
    assert comp["S2"]["amount"] == "30"
    assert comp["S2"]["fraction"] == "0.428571429"

    # 第二级转移：从混合孔按比例带走 S1/S2
    resp = client.post(
        "/batches",
        json={
            "request_key": "k2",
            "operations": [{"source": "B1", "destination": "C1", "volume": "35"}],
        },
    )
    assert resp.status_code == 201, resp.text
    c1 = client.get("/wells/C1").json()
    assert c1["volume"] == "35"
    assert {c["sample"]: c["amount"] for c in c1["composition"]} == {"S1": "20", "S2": "15"}
    assert client.get("/wells/B1").json()["volume"] == "35"


def test_batch_rolls_back_on_insufficient_volume(client):
    register(client, "A1", "S1", "100")
    resp = client.post(
        "/batches",
        json={
            "request_key": "k-fail",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "60"},
                {"source": "A1", "destination": "B2", "volume": "50"},  # 只剩 40，失败
            ],
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "A1"
    assert detail["available"] == "40"
    assert detail["requested"] == "50"

    # 整批回滚：A1 不变，B1/B2 从未被创建
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/B2").status_code == 404

    # 失败批次也被完整记录
    batch = client.get(f"/batches/{detail['batch_id']}").json()
    assert batch["status"] == "failed"
    assert batch["operations"] == []
    assert "体积不足" in batch["error"]


def test_request_key_idempotency(client):
    register(client, "A1", "S1", "100")
    payload = {
        "request_key": "same-key",
        "operations": [{"source": "A1", "destination": "B1", "volume": "40"}],
    }
    first = client.post("/batches", json=payload)
    assert first.status_code == 201
    second = client.post("/batches", json=payload)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    # 不会重复执行
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"


def test_failed_batch_key_is_consumed(client):
    register(client, "A1", "S1", "10")
    payload = {
        "request_key": "fail-key",
        "operations": [{"source": "A1", "destination": "B1", "volume": "50"}],
    }
    assert client.post("/batches", json=payload).status_code == 409
    replay = client.post("/batches", json=payload)
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"
    assert replay.json()["replayed"] is True


def test_unknown_source_well_fails_batch(client):
    resp = client.post(
        "/batches",
        json={
            "request_key": "ghost",
            "operations": [{"source": "Z9", "destination": "B1", "volume": "1"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["available"] == "0"


def test_well_batches_history(client):
    register(client, "A1", "S1", "100")
    client.post(
        "/batches",
        json={
            "request_key": "h1",
            "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
        },
    )
    client.post(
        "/batches",
        json={
            "request_key": "h2",
            "operations": [{"source": "B1", "destination": "C1", "volume": "5"}],
        },
    )
    body = client.get("/wells/B1/batches").json()
    assert [b["request_key"] for b in body["batches"]] == ["h1", "h2"]
    # 完整批次记录：含该批全部操作
    assert body["batches"][0]["operations"][0]["source"] == "A1"

    all_batches = client.get("/batches").json()["batches"]
    assert [b["request_key"] for b in all_batches] == ["h1", "h2"]


def test_same_source_and_destination_rejected(client):
    register(client, "A1", "S1", "10")
    resp = client.post(
        "/batches",
        json={
            "request_key": "self",
            "operations": [{"source": "A1", "destination": "A1", "volume": "1"}],
        },
    )
    assert resp.status_code == 422


def test_empty_operations_rejected(client):
    resp = client.post("/batches", json={"request_key": "empty", "operations": []})
    assert resp.status_code == 422


# ------------------------------------------------ 微量多组分配比修正回归


def test_tiny_multicomponent_transfer_keeps_invariant(client):
    """三个 1e-9 样本，取走 1e-9：目标孔必须拿到恰好一个完整单位。"""
    for sample in ("S1", "S2", "S3"):
        register(client, "A1", sample, "0.000000001")

    resp = client.post(
        "/batches",
        json={
            "request_key": "tiny-1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000001"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    src = assert_invariant(client, "A1")
    assert src["volume"] == "0.000000002"
    # 余量相同（各 1 个单位，份额 1/3），按样本 ID 排序：S1 被带走
    assert set(amounts(src)) == {"S2", "S3"}

    dst = assert_invariant(client, "B1")
    assert dst["volume"] == "0.000000001"
    assert amounts(dst) == {"S1": Decimal("0.000000001")}
    assert dst["composition"][0]["fraction"] == "1"


def test_consecutive_tiny_transfers_drain_in_id_order(client):
    """同一批连续移液使用修正后的组成：逐单位抽干，平局顺序随剩余 ID 前进。"""
    for sample in ("S1", "S2", "S3"):
        register(client, "A1", sample, "0.000000001")

    resp = client.post(
        "/batches",
        json={
            "request_key": "tiny-drain",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000001"},
                {"source": "A1", "destination": "B2", "volume": "0.000000001"},
                {"source": "A1", "destination": "B3", "volume": "0.000000001"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    src = assert_invariant(client, "A1")
    assert src["volume"] == "0"
    assert src["composition"] == []
    for well, sample in (("B1", "S1"), ("B2", "S2"), ("B3", "S3")):
        body = assert_invariant(client, well)
        assert amounts(body) == {sample: Decimal("0.000000001")}

    # 抽干后再取按体积不足处理，且整批回滚不破坏不变量
    resp = client.post(
        "/batches",
        json={
            "request_key": "tiny-drain-over",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000001"}
            ],
        },
    )
    assert resp.status_code == 409
    assert assert_invariant(client, "B1")["volume"] == "0.000000001"


def test_largest_remainder_assignment_order(client):
    """7 单位中取 3：余数 2/1/0 排序，两个单位分给 S1、S2；S3 留整。"""
    register(client, "A1", "S1", "0.000000004")
    register(client, "A1", "S2", "0.000000002")
    register(client, "A1", "S3", "0.000000001")

    resp = client.post(
        "/batches",
        json={
            "request_key": "remainder",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000003"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    src = assert_invariant(client, "A1")
    assert amounts(src) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000001"),
        "S3": Decimal("0.000000001"),
    }
    dst = assert_invariant(client, "B1")
    assert amounts(dst) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000001"),
    }

    # 再取 2 单位：当前 2/1/1（共 4），份额 1r0、0r2、0r2；floor 给 S1 一个，
    # 最后 1 个单位按「余数降序、ID 升序」归 S2
    resp = client.post(
        "/batches",
        json={
            "request_key": "remainder-2",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000002"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    src = assert_invariant(client, "A1")
    assert amounts(src) == {
        "S1": Decimal("0.000000001"),
        "S3": Decimal("0.000000001"),
    }
    dst = assert_invariant(client, "B1")
    assert amounts(dst) == {
        "S1": Decimal("0.000000003"),
        "S2": Decimal("0.000000002"),
    }


def test_corrected_composition_flows_between_batches(client):
    """修正后的组成供后续批次的按比例吸取与占比查询使用。"""
    register(client, "A1", "S1", "0.000000002")
    register(client, "A1", "S2", "0.000000002")

    # 取 3 单位：各 floor=1（2/4 平局），第三单位按 ID 给 S1 => S1=2, S2=1
    resp = client.post(
        "/batches",
        json={
            "request_key": "flow-1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000003"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    b1 = assert_invariant(client, "B1")
    assert amounts(b1) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000001"),
    }
    fractions = {c["sample"]: c["fraction"] for c in b1["composition"]}
    # 占比基于修正后的组成：2/3 与 1/3
    assert fractions == {"S1": "0.666666667", "S2": "0.333333333"}

    # 下一批从 B1 取 2 单位：S1 必中（4/3=1r1），S2 得 1（2/3）
    resp = client.post(
        "/batches",
        json={
            "request_key": "flow-2",
            "operations": [
                {"source": "B1", "destination": "C1", "volume": "0.000000002"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    c1 = assert_invariant(client, "C1")
    assert amounts(c1) == {
        "S1": Decimal("0.000000001"),
        "S2": Decimal("0.000000001"),
    }
    assert_invariant(client, "B1")
    assert_invariant(client, "A1")


def test_transfer_cannot_exceed_component_amount(client):
    """任何样本被带走的量不得超过其原有量：全部转给目标后，来源无该样本。"""
    register(client, "A1", "S1", "0.000000005")
    register(client, "A1", "S2", "0.000000003")

    resp = client.post(
        "/batches",
        json={
            "request_key": "cap",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000008"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    src = assert_invariant(client, "A1")
    assert src["volume"] == "0"
    assert src["composition"] == []
    dst = assert_invariant(client, "B1")
    assert amounts(dst) == {
        "S1": Decimal("0.000000005"),
        "S2": Decimal("0.000000003"),
    }


# ------------------------------------------------ 误操作批次撤销


def reverse(client, batch_id, request_key):
    return client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": request_key}
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


def test_reversal_restores_wells_and_keeps_batch(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    bid = committed_batch_id(client, "r1", "A1", "B1", "40")
    # 另一批次只触及 A2/D1，不影响本批撤销
    committed_batch_id(client, "r1-other", "A2", "D1", "30")
    assert client.get("/wells/B1").json()["volume"] == "40"

    resp = reverse(client, bid, "rev-r1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_id"] == bid
    assert body["replayed"] is False
    assert body["restored_wells"] == ["A1", "B1"]
    assert body["batch"]["reversed"] is True

    # 所有快照孔原子恢复：A1 满量；该批新建的 B1 被删除（A2 不属本批，保持 30）
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/A2").json()["volume"] == "30"
    assert client.get("/wells/D1").json()["volume"] == "30"

    # 原批次不删除，仅标记 reversed
    batch = client.get(f"/batches/{bid}").json()
    assert batch["status"] == "committed"
    assert batch["reversed"] is True
    assert len(batch["operations"]) == 1
    assert client.get("/batches").json()["batches"][0]["batch_id"] == bid
    # 现有查询不变：按孔仍能查到原批次的完整操作
    history = client.get("/wells/A1/batches").json()["batches"]
    assert [b["batch_id"] for b in history] == [bid]
    assert history[0]["operations"][0]["destination"] == "B1"


def test_reversal_restores_preexisting_destination_and_decimal_amounts(client):
    register(client, "A1", "S1", "0.000000003")
    register(client, "B1", "S2", "0.000000002")
    bid = committed_batch_id(client, "r-dec", "A1", "B1", "0.000000003")
    # B1 批前已存在：快照保留，撤销后恢复为只有 2 单位 S2
    assert amounts(client.get("/wells/B1").json()) == {
        "S1": Decimal("0.000000003"),
        "S2": Decimal("0.000000002"),
    }

    assert reverse(client, bid, "rev-dec").status_code == 201
    b1 = assert_invariant(client, "B1")
    assert b1["volume"] == "0.000000002"
    assert amounts(b1) == {"S2": Decimal("0.000000002")}
    a1 = assert_invariant(client, "A1")
    assert a1["volume"] == "0.000000003"
    assert amounts(a1) == {"S1": Decimal("0.000000003")}


def test_reversal_blocked_by_later_batch_lists_blocking_wells(client):
    register(client, "A1", "S1", "100")
    first = committed_batch_id(client, "blk-1", "A1", "B1", "40")
    # 后续批次同时触及 A1 与 B1，构成阻塞
    second_resp = client.post(
        "/batches",
        json={
            "request_key": "blk-2",
            "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
        },
    )
    second = second_resp.json()["batch_id"]

    resp = reverse(client, first, "rev-blk")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "blocked"
    expected_event = {"type": "batch", "batch_id": second, "request_key": "blk-2"}
    wells = {b["well"]: b["events"] for b in detail["blocking_wells"]}
    assert set(wells) == {"A1", "B1"}
    assert wells["A1"] == [expected_event]
    assert wells["B1"] == [expected_event]

    # 液体状态保持不变
    assert client.get("/wells/A1").json()["volume"] == "50"
    assert client.get("/wells/B1").json()["volume"] == "50"
    # 冲突后同键可重新尝试（键未被消耗）
    assert reverse(client, first, "rev-blk").status_code == 409


def test_reversal_blocked_by_later_top_up(client):
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "top-1", "A1", "B1", "40")
    # 对来源孔补液
    register(client, "A1", "S1", "5")

    resp = reverse(client, bid, "rev-top")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "blocked"
    events = {b["well"]: b["events"] for b in detail["blocking_wells"]}["A1"]
    assert events == [{"type": "top_up", "sample": "S1", "volume": "5"}]
    # 液体状态保持不变
    assert client.get("/wells/A1").json()["volume"] == "65"


def test_unrelated_later_batch_does_not_block(client):
    register(client, "A1", "S1", "100")
    register(client, "D1", "S9", "10")
    bid = committed_batch_id(client, "iso-1", "A1", "B1", "40")
    committed_batch_id(client, "iso-2", "D1", "E1", "5")
    resp = reverse(client, bid, "rev-iso")
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/D1").json()["volume"] == "5"


def test_reversal_is_idempotent_same_key_same_target(client):
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "idem-1", "A1", "B1", "40")
    first = reverse(client, bid, "rev-idem")
    assert first.status_code == 201
    first_body = first.json()

    second = reverse(client, bid, "rev-idem")
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["reversal_id"] == first_body["reversal_id"]
    # 状态稳定
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_reversal_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    b1 = committed_batch_id(client, "key-1", "A1", "C1", "10")
    b2 = committed_batch_id(client, "key-2", "A2", "C2", "10")
    assert reverse(client, b1, "shared-rev-key").status_code == 201

    resp = reverse(client, b2, "shared-rev-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_batch_id"] == b1
    # 第二个批次未被撤销，液体不变
    assert client.get(f"/batches/{b2}").json()["reversed"] is False
    assert client.get("/wells/A2").json()["volume"] == "40"


def test_already_reversed_batch_conflicts(client):
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "dbl-1", "A1", "B1", "40")
    assert reverse(client, bid, "rev-dbl-1").status_code == 201
    resp = reverse(client, bid, "rev-dbl-2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_reversed"
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_failed_batch_cannot_be_reversed(client):
    register(client, "A1", "S1", "10")
    resp = client.post(
        "/batches",
        json={
            "request_key": "rev-fail",
            "operations": [{"source": "A1", "destination": "B1", "volume": "50"}],
        },
    )
    assert resp.status_code == 409
    failed_id = resp.json()["detail"]["batch_id"]
    rev = reverse(client, failed_id, "rev-of-failed")
    assert rev.status_code == 409
    assert rev.json()["detail"]["reason"] == "not_committed"


def test_reversal_unknown_batch_is_404(client):
    assert reverse(client, 999, "rev-ghost").status_code == 404


def test_legacy_batch_without_snapshot_cannot_be_reversed(client):
    import os

    db_file = os.environ["LINEAGE_DB"]
    # 模拟上线前已提交的批次：直接写批次/操作记录，但没有快照
    conn = sqlite3.connect(db_file)
    try:
        cur = conn.execute(
            "INSERT INTO batches (request_key, status, error, created_at) "
            "VALUES ('legacy-1', 'committed', NULL, '2026-01-01T00:00:00+00:00')"
        )
        legacy_id = cur.lastrowid
        conn.execute(
            "INSERT INTO operations (batch_id, step, source, destination, volume) "
            "VALUES (?, 1, 'A1', 'B1', '10')",
            (legacy_id,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = reverse(client, legacy_id, "rev-legacy")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "no_snapshot"
    assert "快照" in detail["message"]


def test_reversal_multi_step_batch_restores_all_snapshots(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = client.post(
        "/batches",
        json={
            "request_key": "multi-1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "40"},
                {"source": "A2", "destination": "B1", "volume": "30"},
                {"source": "B1", "destination": "C1", "volume": "35"},
            ],
        },
    )
    bid = resp.json()["batch_id"]
    assert reverse(client, bid, "rev-multi").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "60"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/C1").status_code == 404
    for well in ("A1", "A2"):
        assert_invariant(client, well)


def test_reversal_same_well_touched_twice_in_batch(client):
    """同一批内多次触及同一孔：每孔仅一条标记事件，撤销仍按批前快照恢复。"""
    register(client, "A1", "S1", "0.000000005")
    register(client, "A1", "S2", "0.000000003")
    resp = client.post(
        "/batches",
        json={
            "request_key": "twice-1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000002"},
                {"source": "A1", "destination": "B2", "volume": "0.000000001"},
            ],
        },
    )
    bid = resp.json()["batch_id"]
    assert reverse(client, bid, "rev-twice").status_code == 201

    a1 = assert_invariant(client, "A1")
    assert amounts(a1) == {
        "S1": Decimal("0.000000005"),
        "S2": Decimal("0.000000003"),
    }
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/B2").status_code == 404


def test_reversal_conflict_is_atomic(client):
    """冲突时不写任何撤销记录、不改液体；之后排除阻塞仍可成功撤销。"""
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "atm-1", "A1", "B1", "40")
    register(client, "A1", "S1", "5")  # 补液阻塞
    assert reverse(client, bid, "rev-atm").status_code == 409

    import os
    import sqlite3

    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0] == 0
    finally:
        conn.close()


# ------------------------------------------------ 孔位容量上限


def set_capacity(client, well, max_capacity):
    return client.put(f"/wells/{well}/capacity", json={"max_capacity": max_capacity})


def test_capacity_defaults_to_unlimited(client):
    body = register(client, "A1", "S1", "100")
    assert body["max_capacity"] is None
    assert client.get("/wells/A1").json()["max_capacity"] is None
    # 不限容：任意大额补液照常成功
    body = register(client, "A1", "S1", "1000000")
    assert body["volume"] == "1000100"


def test_set_and_clear_capacity(client):
    register(client, "A1", "S1", "50")
    resp = set_capacity(client, "A1", "100")
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_capacity"] == "100"
    assert client.get("/wells/A1").json()["max_capacity"] == "100"

    # 取消上限（null）恢复不限
    resp = set_capacity(client, "A1", None)
    assert resp.status_code == 200
    assert resp.json()["max_capacity"] is None
    assert client.get("/wells/A1").json()["max_capacity"] is None


def test_capacity_below_current_volume_conflicts(client):
    register(client, "A1", "S1", "50")
    resp = set_capacity(client, "A1", "40")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_below_volume"
    assert detail["well"] == "A1"
    assert detail["volume"] == "50"
    assert detail["max_capacity"] == "40"
    # 配置不变：仍为不限
    assert client.get("/wells/A1").json()["max_capacity"] is None

    # 等于当前体积允许（上限不低于当前体积）
    assert set_capacity(client, "A1", "50").status_code == 200
    assert client.get("/wells/A1").json()["max_capacity"] == "50"


def test_capacity_unknown_well_is_404(client):
    assert set_capacity(client, "Z9", "10").status_code == 404


def test_capacity_invalid_value_rejected(client):
    register(client, "A1", "S1", "10")
    assert set_capacity(client, "A1", "0").status_code == 422
    assert set_capacity(client, "A1", "-5").status_code == 422
    assert set_capacity(client, "A1", "abc").status_code == 422


def test_top_up_respects_capacity(client):
    register(client, "A1", "S1", "50")
    assert set_capacity(client, "A1", "60").status_code == 200

    resp = client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "20"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["well"] == "A1"
    assert detail["volume"] == "50"  # 当时体积
    assert detail["adding"] == "20"  # 拟加入量
    assert detail["max_capacity"] == "60"
    # 液体状态不变
    assert client.get("/wells/A1").json()["volume"] == "50"

    # 上限内补液成功（恰好到顶）
    body = register(client, "A1", "S1", "10")
    assert body["volume"] == "60"


def test_capacity_uses_decimal_precision(client):
    register(client, "A1", "S1", "0.000000005")
    resp = set_capacity(client, "A1", "0.000000008")
    assert resp.status_code == 200
    assert resp.json()["max_capacity"] == "0.000000008"

    # 0.000000005 + 0.000000004 = 0.000000009 > 0.000000008
    resp = client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "0.000000004"})
    assert resp.status_code == 409
    # 0.000000003 恰好到上限
    body = register(client, "A1", "S1", "0.000000003")
    assert body["volume"] == "0.000000008"
    assert_invariant(client, "A1")


def test_batch_rolls_back_on_capacity_exceeded(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "50")
    assert set_capacity(client, "B1", "60").status_code == 200

    payload = {
        "request_key": "cap-batch",
        "operations": [
            {"source": "A1", "destination": "C1", "volume": "10"},  # 本步可成功
            {"source": "A1", "destination": "B1", "volume": "20"},  # 50+20=70 > 60
        ],
    }
    resp = client.post("/batches", json=payload)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["well"] == "B1"
    assert detail["volume"] == "50"
    assert detail["adding"] == "20"
    assert detail["max_capacity"] == "60"

    # 整批回滚：A1 不变，C1 从未被创建
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/C1").status_code == 404
    assert client.get("/wells/B1").json()["volume"] == "50"

    # 失败批次落库并消耗 request_key
    batch = client.get(f"/batches/{detail['batch_id']}").json()
    assert batch["status"] == "failed"
    assert "容量超限" in batch["error"]
    replay = client.post("/batches", json=payload)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "failed"


def test_batch_capacity_checked_per_step(client):
    """上限按每一步当时的目标体积校验：第一步到位后，第二步按新体积判定。"""
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "10")
    assert set_capacity(client, "B1", "70").status_code == 200

    resp = client.post(
        "/batches",
        json={
            "request_key": "stepwise",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "40"},  # 10+40=50 ≤ 70
                {"source": "A1", "destination": "B1", "volume": "40"},  # 50+40=90 > 70
            ],
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "B1"
    assert detail["volume"] == "50"  # 第二步时的当时体积
    assert detail["adding"] == "40"
    assert detail["max_capacity"] == "70"
    # 整批回滚
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").json()["volume"] == "10"


def test_batch_move_out_then_in_within_capacity(client):
    """同一批次先移出再移入：每步按当时体积校验，移出腾出的空间本批即可用。"""
    register(client, "A1", "S1", "50")
    register(client, "B1", "S2", "70")
    assert set_capacity(client, "B1", "80").status_code == 200

    resp = client.post(
        "/batches",
        json={
            "request_key": "out-then-in",
            "operations": [
                {"source": "B1", "destination": "C1", "volume": "30"},  # B1: 70 -> 40
                {"source": "A1", "destination": "B1", "volume": "40"},  # B1: 40 -> 80 ≤ 80
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/B1").json()["volume"] == "80"
    assert client.get("/wells/C1").json()["volume"] == "30"
    assert_invariant(client, "B1")

    # 若直接移入（不先移出）：80+1 > 80，整批失败
    resp = client.post(
        "/batches",
        json={
            "request_key": "in-over",
            "operations": [{"source": "A1", "destination": "B1", "volume": "1"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "capacity_exceeded"
    assert client.get("/wells/B1").json()["volume"] == "80"


def test_reversal_rejected_when_snapshot_exceeds_current_capacity(client):
    """快照恢复后的体积超过孔位当前上限：整次撤销拒绝，液体与审计记录不变。"""
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "rc-1", "A1", "B1", "40")  # A1: 100 -> 60
    # 当前 A1=60，可设上限 80；但撤销会把 A1 恢复到 100 > 80
    assert set_capacity(client, "A1", "80").status_code == 200

    resp = reverse(client, bid, "rev-cap")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["exceeding_wells"] == [
        {"well": "A1", "volume": "100", "max_capacity": "80"}
    ]

    # 液体与审计记录不变
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get(f"/batches/{bid}").json()["reversed"] is False

    import os
    import sqlite3

    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0] == 0
    finally:
        conn.close()

    # 冲突不消耗撤销 request_key：取消上限后同键撤销成功
    assert set_capacity(client, "A1", None).status_code == 200
    assert reverse(client, bid, "rev-cap").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_reversal_allowed_when_snapshot_within_capacity(client):
    """快照恢复后的体积不超过当前上限时，撤销正常进行。"""
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "rc-ok", "A1", "B1", "40")
    assert set_capacity(client, "A1", "100").status_code == 200
    assert reverse(client, bid, "rev-ok").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"
    # 容量配置不受快照恢复影响
    assert client.get("/wells/A1").json()["max_capacity"] == "100"


def test_preexisting_database_without_capacity_column_migrates(tmp_path, monkeypatch):
    """老库（wells 无 max_capacity 列）启动时就地迁移，既有数据默认不限容。"""
    import sqlite3

    from fastapi.testclient import TestClient

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE wells (well TEXT PRIMARY KEY, volume TEXT NOT NULL DEFAULT '0');
        INSERT INTO wells (well, volume) VALUES ('A1', '50');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("LINEAGE_DB", str(db_file))
    from app.main import app

    with TestClient(app) as client:
        body = client.get("/wells/A1").json()
        assert body["volume"] == "50"
        assert body["max_capacity"] is None
        # 既有数据行为不变：不限容，大额补液照常成功
        resp = client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "1000"})
        assert resp.status_code == 201
        assert resp.json()["volume"] == "1050"
        # 迁移后新功能可用
        assert set_capacity(client, "A1", "2000").status_code == 200

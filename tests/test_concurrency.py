"""撤销与补液（及其相互之间）并发的回归测试。

SQLite 写事务统一为 BEGIN IMMEDIATE：所有变更从校验开始即互斥。
以下用例通过对台账函数加「门闩」的方式在真实多线程下确定性地
控制两种先后顺序（TestClient 的同步端点运行在工作线程中）：

1. 补液先持锁提交：撤销必须在锁后读到新事件，返回 409 并列出阻塞
   孔位与补液操作，液体保持补液后的状态（组分不丢失）；
2. 撤销先持锁提交：补液在锁上等待，提交后叠加在恢复后的状态上
   （包括撤销中被整体删除、随后又补液重建的孔）。
"""

import threading
from decimal import Decimal

import pytest

from app import ledger


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


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
        except Exception as exc:  # 在线程外重新抛出，附带原始堆栈信息
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


@pytest.fixture()
def reversible_batch(client):
    """A1(S1)=100，批次 A1->B1 40 已提交：A1=60，B1=40(S1)。"""
    register(client, "A1", "S1", "100")
    bid = committed_batch_id(client, "conc-1", "A1", "B1", "40")
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    return bid


# ------------------------------------------------ 补液先提交 -> 撤销 409


def test_top_up_committed_first_blocks_reversal(client, reversible_batch, monkeypatch):
    bid = reversible_batch
    topup_inside = _Gate()
    rev_ready = threading.Event()
    original_register = ledger.register_initial_sample
    original_find = ledger.find_reversal_by_key

    def gated_register(conn, well, sample, volume):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        topup_inside.entered.set()
        assert topup_inside.release.wait(10.0), "补液线程未被释放"
        return original_register(conn, well, sample, volume)

    def gated_find(conn, request_key):
        # 撤销事务外预检前：先确认补液已持锁，再让撤销去排队取锁
        rev_ready.wait(10.0)
        return original_find(conn, request_key)

    monkeypatch.setattr(ledger, "register_initial_sample", gated_register)
    monkeypatch.setattr(ledger, "find_reversal_by_key", gated_find)

    # 撤销先启动：停在预检门闩前，尚未取锁
    rev_thread, rev_result = _run_in_thread(lambda: reverse(client, bid, "rev-topup-first"))

    # 补液启动并持锁停在门闩内
    topup_thread, topup_result = _run_in_thread(
        lambda: register(client, "A1", "S1", "5")
    )
    topup_inside.wait_inside()

    # 放行撤销：它在 BEGIN IMMEDIATE 处排队，不能越过补液的锁
    rev_ready.set()
    threading.Event().wait(0.3)
    assert rev_thread.is_alive(), "撤销不应在补液持锁期间完成"

    topup_inside.release.set()
    topup_body = _join(topup_thread, topup_result)
    rev_resp = _join(rev_thread, rev_result)

    # 补液成功
    assert topup_body["volume"] == "65"
    # 撤销在锁后重检阻塞条件：409 且列出 A1 上的补液操作
    assert rev_resp.status_code == 409, rev_resp.text
    detail = rev_resp.json()["detail"]
    assert detail["reason"] == "blocked"
    events = {b["well"]: b["events"] for b in detail["blocking_wells"]}
    assert events["A1"] == [{"type": "top_up", "sample": "S1", "volume": "5"}]

    # 组分不丢失：A1 仍是补液后的 65，批次未撤销，B1 保持 40
    a1 = client.get("/wells/A1").json()
    assert a1["volume"] == "65"
    assert amounts(a1) == {"S1": Decimal("65")}
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get(f"/batches/{bid}").json()["reversed"] is False

    # 冲突未消耗撤销 request_key：补液既成事实后重试仍是同样的 409
    again = reverse(client, bid, "rev-topup-first")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "blocked"


# ------------------------------------------------ 撤销先提交 -> 补液叠加


def test_reversal_committed_first_top_up_stacks_on_restore(
    client, reversible_batch, monkeypatch
):
    bid = reversible_batch
    gate = _Gate()
    original = ledger.apply_reversal

    def gated_reversal(conn, batch_id, request_key):
        # 已在 BEGIN IMMEDIATE 之后、阻塞检查通过：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "撤销线程未被释放"
        return original(conn, batch_id, request_key)

    monkeypatch.setattr(ledger, "apply_reversal", gated_reversal)

    def top_ups():
        # A1 是快照中恢复的孔；B1 是撤销中被删除、随后重建的孔
        a1 = register(client, "A1", "S2", "7")
        b1 = register(client, "B1", "S3", "9")
        return a1, b1

    rev_thread, rev_result = _run_in_thread(lambda: reverse(client, bid, "rev-first"))
    gate.wait_inside()  # 撤销持锁、已过阻塞检查

    topup_thread, topup_result = _run_in_thread(top_ups)
    threading.Event().wait(0.3)
    assert topup_thread.is_alive(), "补液应在撤销的写锁上排队"
    assert rev_thread.is_alive()

    gate.release.set()
    rev_resp = _join(rev_thread, rev_result)
    a1_after, b1_after = _join(topup_thread, topup_result)

    assert rev_resp.status_code == 201, rev_resp.text
    # A1 补液叠加在恢复后的 100(S1) 上：107，S1=100、S2=7
    assert a1_after["volume"] == "107"
    assert amounts(a1_after) == {"S1": Decimal("100"), "S2": Decimal("7")}
    # B1 撤销时被删除，补液在恢复后的状态上重建：9，仅 S3=9
    assert b1_after["volume"] == "9"
    assert amounts(b1_after) == {"S3": Decimal("9")}

    # 落库状态与返回一致，不变量成立
    a1 = client.get("/wells/A1").json()
    assert sum(amounts(a1).values(), Decimal(0)) == Decimal(a1["volume"]) == Decimal("107")
    b1 = client.get("/wells/B1").json()
    assert sum(amounts(b1).values(), Decimal(0)) == Decimal(b1["volume"]) == Decimal("9")
    assert client.get(f"/batches/{bid}").json()["reversed"] is True


# --------------------------------------- request_key 并发重放/换目标语义


def test_concurrent_same_key_same_target_replays_once(client, reversible_batch):
    bid = reversible_batch
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(lambda: reverse(client, bid, "rev-conc-idem"))
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    # 一个首次成功、一个重放，指向同一条撤销记录，状态只恢复一次
    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["reversal_id"] for b in bodies} == {bodies[0]["reversal_id"]}
    assert [b["replayed"] for b in bodies].count(True) == 1
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_concurrent_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    b1 = committed_batch_id(client, "ck-1", "A1", "C1", "10")
    b2 = committed_batch_id(client, "ck-2", "A2", "C2", "10")

    barrier = threading.Barrier(2)
    threads, results = [], []
    for target in (b1, b2):
        t, r = _run_in_thread(
            lambda target=target: (barrier.wait(), reverse(client, target, "shared-rev"))[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    statuses = sorted(resp.status_code for resp in responses)
    assert statuses == [201, 409], statuses
    conflict = next(resp for resp in responses if resp.status_code == 409)
    detail = conflict.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert {detail["batch_id"], detail["existing_batch_id"]} == {b1, b2}

    # 只有一个批次被撤销，另一个液体不变
    import sqlite3
    import os

    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0] == 1
    finally:
        conn.close()
    reversed_target = detail["existing_batch_id"]
    untouched = b2 if reversed_target == b1 else b1
    untouched_source = "A2" if untouched == b2 else "A1"
    untouched_dest = "C2" if untouched == b2 else "C1"
    assert client.get(f"/wells/{untouched_source}").json()["volume"] == (
        "40" if untouched == b2 else "90"
    )
    assert client.get(f"/wells/{untouched_dest}").json()["volume"] == "10"


# ------------------------------------------------ 容量变更与补液串行


def test_top_up_committed_first_capacity_change_sees_new_volume(client, monkeypatch):
    """补液先持锁提交：容量变更在锁后读到新体积，新上限低于它即 409，配置不变。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original_register = ledger.register_initial_sample

    def gated_register(conn, well, sample, volume):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "补液线程未被释放"
        return original_register(conn, well, sample, volume)

    monkeypatch.setattr(ledger, "register_initial_sample", gated_register)

    topup_thread, topup_result = _run_in_thread(lambda: register(client, "A1", "S1", "10"))
    gate.wait_inside()  # 补液持锁

    cap_thread, cap_result = _run_in_thread(
        lambda: client.put("/wells/A1/capacity", json={"max_capacity": "105"})
    )
    threading.Event().wait(0.3)
    assert cap_thread.is_alive(), "容量变更应在补液的写锁上排队"

    gate.release.set()
    topup_body = _join(topup_thread, topup_result)
    cap_resp = _join(cap_thread, cap_result)

    # 补液先提交：A1=110；容量变更看到已提交的 110，105 < 110 -> 409
    assert topup_body["volume"] == "110"
    assert cap_resp.status_code == 409, cap_resp.text
    detail = cap_resp.json()["detail"]
    assert detail["reason"] == "capacity_below_volume"
    assert detail["volume"] == "110"
    # 配置不变：仍为不限
    assert client.get("/wells/A1").json()["max_capacity"] is None


def test_capacity_committed_first_top_up_sees_limit(client, monkeypatch):
    """容量变更先持锁提交：补液在锁后读到新上限，超限即 409，液体不变。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original_set = ledger.set_well_capacity

    def gated_set(conn, well, capacity):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "容量变更线程未被释放"
        return original_set(conn, well, capacity)

    monkeypatch.setattr(ledger, "set_well_capacity", gated_set)

    cap_thread, cap_result = _run_in_thread(
        lambda: client.put("/wells/A1/capacity", json={"max_capacity": "105"})
    )
    gate.wait_inside()  # 容量变更持锁

    topup_thread, topup_result = _run_in_thread(
        lambda: client.post("/wells", json={"well": "A1", "sample": "S1", "volume": "10"})
    )
    threading.Event().wait(0.3)
    assert topup_thread.is_alive(), "补液应在容量变更的写锁上排队"

    gate.release.set()
    cap_resp = _join(cap_thread, cap_result)
    topup_resp = _join(topup_thread, topup_result)

    # 容量变更先提交：上限 105；补液看到已提交的上限，100+10 > 105 -> 409
    assert cap_resp.status_code == 200, cap_resp.text
    assert cap_resp.json()["max_capacity"] == "105"
    assert topup_resp.status_code == 409, topup_resp.text
    detail = topup_resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["max_capacity"] == "105"
    # 液体不变
    assert client.get("/wells/A1").json()["volume"] == "100"


# ------------------------------------------------ 隔离与既有变更串行


def quarantine(client, wells, case_id="CASE-1", request_key="q-conc"):
    return client.post(
        "/quarantines",
        json={
            "request_key": request_key,
            "case_id": case_id,
            "reason": "污染排查",
            "wells": wells,
        },
    )


def release(client, wells, case_id="CASE-1", request_key="rel-conc"):
    return client.post(
        "/quarantines/releases",
        json={"request_key": request_key, "case_id": case_id, "wells": wells},
    )


def test_quarantine_committed_first_batch_sees_it(client, monkeypatch):
    """隔离先持锁提交：批次在锁后读到隔离状态，409 且不落失败批次。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.apply_quarantine

    def gated_quarantine(conn, request_key, case_id, reason, wells):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "隔离线程未被释放"
        return original(conn, request_key, case_id, reason, wells)

    monkeypatch.setattr(ledger, "apply_quarantine", gated_quarantine)

    q_thread, q_result = _run_in_thread(lambda: quarantine(client, ["A1"]))
    gate.wait_inside()  # 隔离持锁

    batch_payload = {
        "request_key": "conc-batch",
        "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
    }
    b_thread, b_result = _run_in_thread(lambda: client.post("/batches", json=batch_payload))
    threading.Event().wait(0.3)
    assert b_thread.is_alive(), "批次应在隔离的写锁上排队"

    gate.release.set()
    q_resp = _join(q_thread, q_result)
    b_resp = _join(b_thread, b_result)

    assert q_resp.status_code == 201, q_resp.text
    assert b_resp.status_code == 409, b_resp.text
    assert b_resp.json()["detail"]["reason"] == "well_quarantined"
    # 液体、批次均不变：不落失败批次、不消耗 request_key
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/batches").json()["batches"] == []

    # 解除隔离后同键批次成功
    assert release(client, ["A1"]).status_code == 201
    assert client.post("/batches", json=batch_payload).status_code == 201


def test_batch_committed_first_quarantine_still_applies(client, monkeypatch):
    """批次先持锁提交：隔离在锁后读到最新状态，正常建立（隔离不依赖液体状态）。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.execute_batch

    def gated_batch(conn, request_key, operations, reservation_id=None):
        gate.entered.set()
        assert gate.release.wait(10.0), "批次线程未被释放"
        return original(conn, request_key, operations, reservation_id)

    monkeypatch.setattr(ledger, "execute_batch", gated_batch)

    b_thread, b_result = _run_in_thread(
        lambda: client.post(
            "/batches",
            json={
                "request_key": "conc-batch-first",
                "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
            },
        )
    )
    gate.wait_inside()  # 批次持锁

    q_thread, q_result = _run_in_thread(lambda: quarantine(client, ["B1"]))
    threading.Event().wait(0.3)
    assert q_thread.is_alive(), "隔离应在批次的写锁上排队"

    gate.release.set()
    b_resp = _join(b_thread, b_result)
    q_resp = _join(q_thread, q_result)

    assert b_resp.status_code == 201
    assert q_resp.status_code == 201, q_resp.text
    assert client.get("/wells/B1").json()["quarantine"]["case_id"] == "CASE-1"


# --------------------------------------- 隔离 request_key 并发语义


def test_concurrent_same_key_same_target_quarantine_replays_once(client):
    register(client, "A1", "S1", "100")
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(lambda: quarantine(client, ["A1"], request_key="q-idem-conc"))
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["quarantine_id"] for b in bodies} == {bodies[0]["quarantine_id"]}
    assert [b["replayed"] for b in bodies].count(True) == 1
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_concurrent_same_key_other_target_quarantine_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")

    barrier = threading.Barrier(2)
    threads, results = [], []
    for well in ("A1", "A2"):
        t, r = _run_in_thread(
            lambda well=well: (
                barrier.wait(),
                quarantine(client, [well], request_key="q-shared"),
            )[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [201, 409]
    conflict = next(resp for resp in responses if resp.status_code == 409)
    assert conflict.json()["detail"]["reason"] == "request_key_conflict"
    # 只有一个申请落库，另一孔未被隔离
    quarantines = client.get("/quarantines").json()["quarantines"]
    assert len(quarantines) == 1
    isolated = quarantines[0]["wells"][0]["well"]
    other = "A2" if isolated == "A1" else "A1"
    assert client.get(f"/wells/{other}").json()["quarantine"] is None


def test_concurrent_quarantine_same_well_one_wins(client):
    """不同键同时隔离同一孔：先提交者成功，后到者在锁内看到已隔离并整次拒绝。"""
    register(client, "A1", "S1", "100")

    barrier = threading.Barrier(2)
    threads, results = [], []
    for case_id, key in (("CASE-1", "q-race-1"), ("CASE-2", "q-race-2")):
        t, r = _run_in_thread(
            lambda case_id=case_id, key=key: (
                barrier.wait(),
                quarantine(client, ["A1"], case_id=case_id, request_key=key),
            )[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [201, 409]
    conflict = next(resp for resp in responses if resp.status_code == 409)
    detail = conflict.json()["detail"]
    assert detail["reason"] == "already_quarantined"
    assert detail["conflicting_wells"][0]["well"] == "A1"
    # 只有一条生效隔离
    quarantines = client.get("/quarantines").json()["quarantines"]
    assert len(quarantines) == 1
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == (
        quarantines[0]["case_id"]
    )


def test_concurrent_same_key_release_replays_once(client):
    register(client, "A1", "S1", "100")
    assert quarantine(client, ["A1"], request_key="q-for-rel").status_code == 201

    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(lambda: release(client, ["A1"], request_key="rel-idem-conc"))
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["release_id"] for b in bodies} == {bodies[0]["release_id"]}
    assert [b["replayed"] for b in bodies].count(True) == 1
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert len(client.get("/quarantines/releases").json()["releases"]) == 1

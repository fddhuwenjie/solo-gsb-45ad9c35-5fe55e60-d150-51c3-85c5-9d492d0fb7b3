"""微孔板样本谱系追踪 API。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response

from . import db as db_module
from . import ledger
from .schemas import (
    BatchCreate,
    CapacityUpdate,
    QuarantineCreate,
    QuarantineReleaseCreate,
    ReservationCancelCreate,
    ReservationCreate,
    ReversalCreate,
    WellCreate,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db_module.init_db()
    yield


app = FastAPI(
    title="微孔板样本谱系追踪 API",
    version="1.0.0",
    description="登记孔位初始样本，原子执行批量移液，追踪各初始样本在每个孔位中的占比。",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db_module.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 孔位

def _capacity_exceeded_detail(exc: ledger.CapacityExceeded, **extra) -> dict:
    """容量超限 409 的统一载荷：超限孔位、当时体积、拟加入量与上限。"""
    return {
        "message": str(exc),
        "reason": "capacity_exceeded",
        "well": exc.well,
        "volume": ledger.dec_str(exc.volume),
        "adding": ledger.dec_str(exc.adding),
        "max_capacity": ledger.dec_str(exc.max_capacity),
        **extra,
    }


def _well_quarantined_detail(exc: ledger.WellQuarantined, **extra) -> dict:
    """隔离冲突 409 的统一载荷：冲突孔位及其隔离案件。"""
    return {
        "message": str(exc),
        "reason": "well_quarantined",
        "quarantined_wells": exc.wells,
        **extra,
    }


@app.post("/wells", status_code=201)
def register_well(payload: WellCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """登记孔位中的初始样本与体积；同一孔位重复登记视为补液混匀。

    孔位设有容量上限时，补液后体积超过上限返回 409，液体状态不变。
    """
    try:
        with db_module.write_transaction(conn):
            # 立即取写锁：读改写与并发撤销/批次/容量变更串行，补液不会被旧快照覆盖
            ledger.register_initial_sample(conn, payload.well, payload.sample, payload.volume)
    except ledger.WellQuarantined as exc:
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    except ledger.CapacityExceeded as exc:
        raise HTTPException(status_code=409, detail=_capacity_exceeded_detail(exc))
    return ledger.well_state(conn, payload.well)


@app.put("/wells/{well}/capacity")
def set_capacity(
    well: str,
    payload: CapacityUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """设置或取消孔位的最大容量（"max_capacity": null 表示取消，恢复不限）。

    新上限低于孔位当前体积时返回 409 且配置不变。容量变更与补液、批次、
    撤销共用同一把写锁串行：先提交者决定后续校验看到的状态。
    """
    well = well.strip().upper()
    try:
        with db_module.write_transaction(conn):
            ledger.set_well_capacity(conn, well, payload.max_capacity)
    except ledger.WellNotFound:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    except ledger.WellQuarantined as exc:
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    except ledger.CapacityConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "capacity_below_volume",
                "well": exc.well,
                "volume": ledger.dec_str(exc.volume),
                "max_capacity": ledger.dec_str(exc.max_capacity),
            },
        )
    return ledger.well_state(conn, well)


@app.get("/wells/{well}")
def get_well(well: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """查询孔位当前体积与各初始样本占比。"""
    state = ledger.well_state(conn, well.strip().upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    return state


@app.get("/wells/{well}/batches")
def get_well_batches(well: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """查询涉及该孔位（作为来源或目标）的完整批次记录。"""
    well = well.strip().upper()
    if ledger.get_well_volume(conn, well) is None:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    rows = conn.execute(
        "SELECT DISTINCT batch_id FROM operations WHERE source = ? OR destination = ? "
        "ORDER BY batch_id",
        (well, well),
    ).fetchall()
    return {
        "well": well,
        "quarantine": ledger.well_quarantine(conn, well),
        "batches": [ledger.batch_payload(conn, row["batch_id"]) for row in rows],
    }


# ---------------------------------------------------------------- 批次

@app.post("/batches")
def submit_batch(
    payload: BatchCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """提交一批移液操作：单事务按序执行，任一步体积不足或目标孔容量超限则整批回滚。

    request_key 幂等：相同键重复提交不会重复执行，直接返回首个结果。

    携带 reservation_id 时为绑定预留批次：预留必须生效，且批次从每个预留
    来源孔吸取的体积之和与预留体积严格相等（预留的每个孔都必须作为来源），
    否则 409（不兑现、不消耗预留、不消耗 request_key）；成功提交时在同一
    事务把预留标记为 consumed。未绑定预留的普通批次不能动用任何生效预留
    体积：来源孔可吸取量为「当前体积 − 生效预留量」。
    """
    existing = ledger.find_batch_by_key(conn, payload.request_key)
    if existing is not None:
        response.status_code = 200
        return ledger.batch_payload(conn, existing["id"], replayed=True)

    try:
        # 立即取写事务：幂等检查、预留/体积校验与全部写入持锁完成，与并发
        # 补液/撤销/批次/预留从校验开始串行
        with db_module.write_transaction(conn):
            batch_id = ledger.execute_batch(
                conn, payload.request_key, payload.operations, payload.reservation_id
            )
    except ledger.WellQuarantined as exc:
        # 隔离冲突：液体、批次与审计均不变——不落失败批次、不消耗 request_key，
        # 解除隔离后同键可重试
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    except ledger.ReservationNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "message": str(exc),
                "reason": "reservation_not_found",
                "reservation_id": exc.reservation_id,
            },
        )
    except ledger.ReservationNotActive as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "reservation_not_active",
                "reservation_id": exc.reservation_id,
                "status": exc.status,
            },
        )
    except ledger.ReservationFulfillmentMismatch as exc:
        # 未逐孔逐量兑现：整批拒绝，不落失败批次、不消耗 request_key、预留不消耗
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "reservation_fulfillment_mismatch",
                "reservation_id": exc.reservation_id,
                "mismatched_wells": exc.mismatches,
            },
        )
    except ledger.InsufficientVolume as exc:
        # 状态已回滚；把失败批次也记录下来，消耗掉这个 request_key
        try:
            with db_module.write_transaction(conn):
                batch_id = ledger.insert_batch(
                    conn,
                    payload.request_key,
                    "failed",
                    error=str(exc),
                    reservation_id=payload.reservation_id,
                )
        except sqlite3.IntegrityError:
            row = ledger.find_batch_by_key(conn, payload.request_key)
            response.status_code = 200
            return ledger.batch_payload(conn, row["id"], replayed=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "batch_id": batch_id,
                "well": exc.well,
                "available": ledger.dec_str(exc.available),
                "requested": ledger.dec_str(exc.requested),
            },
        )
    except ledger.CapacityExceeded as exc:
        # 与体积不足同理：状态已回滚；失败批次落库并消耗 request_key
        try:
            with db_module.write_transaction(conn):
                batch_id = ledger.insert_batch(
                    conn,
                    payload.request_key,
                    "failed",
                    error=str(exc),
                    reservation_id=payload.reservation_id,
                )
        except sqlite3.IntegrityError:
            row = ledger.find_batch_by_key(conn, payload.request_key)
            response.status_code = 200
            return ledger.batch_payload(conn, row["id"], replayed=True)
        raise HTTPException(
            status_code=409,
            detail=_capacity_exceeded_detail(exc, batch_id=batch_id),
        )
    except sqlite3.IntegrityError:
        # 并发下同一 request_key 已被写入：按重放处理，不重复执行
        row = ledger.find_batch_by_key(conn, payload.request_key)
        response.status_code = 200
        return ledger.batch_payload(conn, row["id"], replayed=True)

    response.status_code = 201
    return ledger.batch_payload(conn, batch_id)


@app.get("/batches")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部批次记录（含失败批次）。"""
    rows = conn.execute("SELECT id FROM batches ORDER BY id").fetchall()
    return {"batches": [ledger.batch_payload(conn, row["id"]) for row in rows]}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """单个批次的完整记录。"""
    row = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return ledger.batch_payload(conn, batch_id)


def _request_key_conflict_detail(request_key: str, batch_id: int, existing_batch_id: int) -> dict:
    return {
        "message": (
            f"request_key {request_key} 已用于撤销批次 "
            f"{existing_batch_id}，不能再用于批次 {batch_id}"
        ),
        "reason": "request_key_conflict",
        "batch_id": batch_id,
        "existing_batch_id": existing_batch_id,
    }


@app.post("/batches/{batch_id}/reversals")
def reverse_batch(
    batch_id: int,
    payload: ReversalCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """撤销一个已提交批次：原子恢复其批前快照并追加审计记录，不删除原批次。

    仅当目标批次已提交、尚未撤销、保存了批前快照，之后没有补液或已提交
    批次触及相关孔位，且快照恢复后的各孔体积不超过其当前容量上限时可撤销；
    条件不符返回 409 并列出阻塞孔位或超限孔位，液体状态保持不变。
    request_key 幂等：同键同目标返回首次结果（200），同键换目标返回 409。
    """
    # 事务外快速路径：无并发时直接给出重放/换目标结论，免取写锁。
    existing = ledger.find_reversal_by_key(conn, payload.request_key)
    if existing is not None:
        if int(existing["batch_id"]) != batch_id:
            raise HTTPException(
                status_code=409,
                detail=_request_key_conflict_detail(
                    payload.request_key, batch_id, int(existing["batch_id"])
                ),
            )
        response.status_code = 200
        return ledger.get_reversal(conn, int(existing["id"]), replayed=True)

    try:
        # BEGIN IMMEDIATE 立即取锁：幂等复核、阻塞检查与快照恢复持同一把
        # 锁完成，期间并发补液/批次只能在锁上等待；先提交的补液/批次必然
        # 已对本事务可见，阻塞检查据此返回 409，液体状态不被旧快照覆盖。
        with db_module.write_transaction(conn):
            # 并发下另一同键撤销可能已在此期间提交：持锁后复核，保证
            # 同键同目标仍按重放（200）处理，而非误判为重复撤销。
            row = ledger.find_reversal_by_key(conn, payload.request_key)
            if row is not None:
                if int(row["batch_id"]) != batch_id:
                    raise HTTPException(
                        status_code=409,
                        detail=_request_key_conflict_detail(
                            payload.request_key, batch_id, int(row["batch_id"])
                        ),
                    )
                response.status_code = 200
                return ledger.get_reversal(conn, int(row["id"]), replayed=True)
            reversal_id = ledger.apply_reversal(conn, batch_id, payload.request_key)
    except ledger.WellQuarantined as exc:
        # 批次触及隔离孔：液体与审计记录不变，request_key 不消耗
        raise HTTPException(
            status_code=409,
            detail=_well_quarantined_detail(exc, batch_id=batch_id),
        )
    except ledger.ReversalConflict as exc:
        if exc.reason == "not_found":
            raise HTTPException(status_code=404, detail=exc.message)
        detail: dict = {"message": exc.message, "reason": exc.reason, "batch_id": batch_id}
        if exc.blockers:
            detail["blocking_wells"] = exc.blockers
        if exc.exceeding:
            detail["exceeding_wells"] = exc.exceeding
        raise HTTPException(status_code=409, detail=detail)
    except sqlite3.IntegrityError:
        # 兜底：极端并发下 request_key 唯一约束拦截（正常已被锁内复核覆盖）
        row = ledger.find_reversal_by_key(conn, payload.request_key)
        if row is not None:
            if int(row["batch_id"]) != batch_id:
                raise HTTPException(
                    status_code=409,
                    detail=_request_key_conflict_detail(
                        payload.request_key, batch_id, int(row["batch_id"])
                    ),
                )
            response.status_code = 200
            return ledger.get_reversal(conn, int(row["id"]), replayed=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"批次 {batch_id} 已经撤销，不能重复撤销",
                "reason": "already_reversed",
                "batch_id": batch_id,
            },
        )

    response.status_code = 201
    return ledger.get_reversal(conn, reversal_id)


# ---------------------------------------------------------------- 孔位隔离

def _quarantine_target(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, list[str]]:
    """隔离申请的目标：案件 + 孔位集合（同键换目标判定的依据）。"""
    wells = [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM quarantine_wells WHERE request_id = ? ORDER BY well",
            (row["id"],),
        ).fetchall()
    ]
    return row["case_id"], wells


def _release_target(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, list[str]]:
    """解除记录的目标：案件 + 孔位集合。"""
    wells = [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM quarantine_release_wells WHERE release_id = ? ORDER BY well",
            (row["id"],),
        ).fetchall()
    ]
    return row["case_id"], wells


def _quarantine_replay_or_conflict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    payload: QuarantineCreate,
    response: Response,
) -> dict:
    """同键同目标（案件 + 孔位集合）按重放返回首次结果；同键换目标返回 409。"""
    case_id, wells = _quarantine_target(conn, row)
    if case_id == payload.case_id and wells == payload.wells:
        response.status_code = 200
        return ledger.quarantine_payload(conn, int(row["id"]), replayed=True)
    raise HTTPException(
        status_code=409,
        detail={
            "message": (
                f"request_key {payload.request_key} 已用于案件 {case_id} 对孔位 "
                f"{'、'.join(wells)} 的隔离申请，不能再用于案件 "
                f"{payload.case_id} 对 {'、'.join(payload.wells)} 的申请"
            ),
            "reason": "request_key_conflict",
            "existing_case_id": case_id,
            "existing_wells": wells,
        },
    )


def _release_replay_or_conflict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    payload: QuarantineReleaseCreate,
    response: Response,
) -> dict:
    case_id, wells = _release_target(conn, row)
    if case_id == payload.case_id and wells == payload.wells:
        response.status_code = 200
        return ledger.release_payload(conn, int(row["id"]), replayed=True)
    raise HTTPException(
        status_code=409,
        detail={
            "message": (
                f"request_key {payload.request_key} 已用于案件 {case_id} 对孔位 "
                f"{'、'.join(wells)} 的解除隔离，不能再用于案件 "
                f"{payload.case_id} 对 {'、'.join(payload.wells)} 的解除"
            ),
            "reason": "request_key_conflict",
            "existing_case_id": case_id,
            "existing_wells": wells,
        },
    )


@app.post("/quarantines")
def create_quarantine(
    payload: QuarantineCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """提交孔位隔离申请：同一事务确认所有孔已登记且尚未隔离，原子建立隔离记录。

    任一孔未登记（404）或已处于隔离状态（409 already_quarantined，逐孔列出
    当前案件）时整次申请拒绝，不留部分状态、不消耗 request_key。
    request_key 幂等：同键同目标返回首次结果（200），同键换目标返回 409。
    """
    # 事务外快速路径：无并发时直接给出重放/换目标结论，免取写锁。
    existing = ledger.find_quarantine_by_key(conn, payload.request_key)
    if existing is not None:
        return _quarantine_replay_or_conflict(conn, existing, payload, response)

    try:
        # BEGIN IMMEDIATE 立即取锁：幂等复核、未隔离校验与写入持同一把锁完成，
        # 与并发补液/批次/撤销/容量变更/其他隔离申请串行。
        with db_module.write_transaction(conn):
            row = ledger.find_quarantine_by_key(conn, payload.request_key)
            if row is not None:
                return _quarantine_replay_or_conflict(conn, row, payload, response)
            quarantine_id = ledger.apply_quarantine(
                conn, payload.request_key, payload.case_id, payload.reason, payload.wells
            )
    except ledger.WellNotFound as exc:
        raise HTTPException(status_code=404, detail=f"孔位 {exc.well} 尚未登记，不能隔离")
    except ledger.QuarantineConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "already_quarantined",
                "conflicting_wells": exc.conflicts,
            },
        )
    except ledger.ReservationsBlockQuarantine as exc:
        # 孔上存在生效预留：整次隔离拒绝，逐孔列出阻塞预留，不消耗 request_key
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "reservations_active",
                "blocking_wells": exc.blockers,
            },
        )
    except sqlite3.IntegrityError:
        # 兜底：极端并发下 request_key 唯一约束拦截（正常已被锁内复核覆盖）
        row = ledger.find_quarantine_by_key(conn, payload.request_key)
        if row is not None:
            return _quarantine_replay_or_conflict(conn, row, payload, response)
        raise HTTPException(
            status_code=409,
            detail={"message": "隔离申请与现有隔离冲突", "reason": "already_quarantined"},
        )

    response.status_code = 201
    return ledger.quarantine_payload(conn, quarantine_id)


@app.post("/quarantines/releases")
def release_quarantine(
    payload: QuarantineReleaseCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """解除隔离：须携带对应 case_id，批量解除任一孔不匹配即整体拒绝。

    每个孔都必须处于隔离状态且隔离案件与 case_id 一致，否则 409
    release_mismatch（逐孔列出不匹配原因），整次解除不留部分状态、不消耗
    request_key。成功后申请记录保留（孔位行标记解除时间），解除审计单独
    追加。request_key 幂等：同键同目标返回首次结果（200），同键换目标 409。
    """
    existing = ledger.find_release_by_key(conn, payload.request_key)
    if existing is not None:
        return _release_replay_or_conflict(conn, existing, payload, response)

    try:
        with db_module.write_transaction(conn):
            row = ledger.find_release_by_key(conn, payload.request_key)
            if row is not None:
                return _release_replay_or_conflict(conn, row, payload, response)
            release_id = ledger.apply_release(
                conn, payload.request_key, payload.case_id, payload.wells
            )
    except ledger.ReleaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "release_mismatch",
                "mismatched_wells": exc.mismatches,
            },
        )
    except sqlite3.IntegrityError:
        row = ledger.find_release_by_key(conn, payload.request_key)
        if row is not None:
            return _release_replay_or_conflict(conn, row, payload, response)
        raise HTTPException(
            status_code=409,
            detail={"message": "解除隔离与现有记录冲突", "reason": "release_mismatch"},
        )

    response.status_code = 201
    return ledger.release_payload(conn, release_id)


@app.get("/quarantines")
def list_quarantines(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部隔离申请记录（含各孔当前状态：隔离中 / 已解除）。"""
    rows = conn.execute("SELECT id FROM quarantine_requests ORDER BY id").fetchall()
    return {"quarantines": [ledger.quarantine_payload(conn, row["id"]) for row in rows]}


@app.get("/quarantines/releases")
def list_quarantine_releases(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部解除隔离审计记录。"""
    rows = conn.execute("SELECT id FROM quarantine_releases ORDER BY id").fetchall()
    return {"releases": [ledger.release_payload(conn, row["id"]) for row in rows]}


# ---------------------------------------------------------------- 移液体积预留


def _reservation_replay_or_conflict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    payload: ReservationCreate,
    response: Response,
) -> dict:
    """同键同目标（逐孔体积的排序集合）按重放返回首次结果；同键换目标返回 409。"""
    target = ledger.reservation_target(conn, int(row["id"]))
    submitted = sorted((item.well, ledger.dec_str(ledger.q(item.volume))) for item in payload.items)
    if target == submitted:
        response.status_code = 200
        return ledger.reservation_payload(conn, int(row["id"]), replayed=True)
    raise HTTPException(
        status_code=409,
        detail={
            "message": (
                f"request_key {payload.request_key} 已用于另一组预留目标"
                f"（{', '.join(f'{w}:{v}' for w, v in target)}），不能再用于"
                f"（{', '.join(f'{w}:{v}' for w, v in submitted)}）"
            ),
            "reason": "request_key_conflict",
            "existing_items": [{"well": w, "volume": v} for w, v in target],
        },
    )


@app.post("/reservations", status_code=201)
def create_reservation(
    payload: ReservationCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """为未来批次一次预留多个已登记来源孔的 Decimal 体积。

    服务在同一事务扣除各孔其他生效预留后检查可用量：任一孔可用不足、孔已
    隔离或未登记即整次失败、不留部分预留、不消耗 request_key。
    request_key 幂等：同键同目标重放返回首次结果（200），同键换目标 409。
    """
    # 事务外快速路径：无并发时直接给出重放/换目标结论，免取写锁。
    existing = ledger.find_reservation_by_key(conn, payload.request_key)
    if existing is not None:
        return _reservation_replay_or_conflict(conn, existing, payload, response)

    try:
        # BEGIN IMMEDIATE 立即取锁：幂等复核、可用量/隔离校验与写入持同一把
        # 锁完成，与并发补液/批次/撤销/隔离/取消串行。
        with db_module.write_transaction(conn):
            row = ledger.find_reservation_by_key(conn, payload.request_key)
            if row is not None:
                return _reservation_replay_or_conflict(conn, row, payload, response)
            reservation_id = ledger.apply_reservation(conn, payload.request_key, payload.items)
    except ledger.WellNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"孔位 {exc.well} 尚未登记，不能预留"
        )
    except ledger.WellQuarantined as exc:
        raise HTTPException(status_code=409, detail=_well_quarantined_detail(exc))
    except ledger.ReservationUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "reservation_unavailable",
                "conflicting_wells": exc.conflicts,
            },
        )
    except sqlite3.IntegrityError:
        # 兜底：极端并发下 request_key 唯一约束拦截（正常已被锁内复核覆盖）
        row = ledger.find_reservation_by_key(conn, payload.request_key)
        if row is not None:
            return _reservation_replay_or_conflict(conn, row, payload, response)
        raise HTTPException(
            status_code=409,
            detail={"message": "预留与现有预留冲突", "reason": "reservation_unavailable"},
        )

    response.status_code = 201
    return ledger.reservation_payload(conn, reservation_id)


@app.get("/reservations")
def list_reservations(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部预留记录（含 active / consumed / cancelled 与逐孔明细）。"""
    rows = conn.execute("SELECT id FROM reservations ORDER BY id").fetchall()
    return {"reservations": [ledger.reservation_payload(conn, row["id"]) for row in rows]}


@app.get("/reservations/cancellations/all")
def list_reservation_cancellations(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部预留取消审计记录。"""
    rows = conn.execute(
        "SELECT id FROM reservation_cancellations ORDER BY id"
    ).fetchall()
    return {"cancellations": [ledger.cancellation_payload(conn, row["id"]) for row in rows]}


@app.get("/reservations/{reservation_id}")
def get_reservation(reservation_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """单个预留的明细与状态（含消费批次/取消审计）。"""
    body = ledger.reservation_payload(conn, reservation_id)
    if body is None:
        raise HTTPException(status_code=404, detail=f"预留 {reservation_id} 不存在")
    return body


@app.post("/reservations/{reservation_id}/cancellations", status_code=201)
def cancel_reservation(
    reservation_id: int,
    payload: ReservationCancelCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """用独立 request_key 整体取消一个生效预留。

    只有 active 预留可取消；已消费（consumed）不可取消，已取消的预留不能
    重复取消（409，不消耗 request_key）。取消与预留建立/兑现、隔离申请
    在同一把写锁上串行。取消 request_key 幂等：同键同预留重放返回首次
    结果（200），同键换预留返回 409。
    """
    # 事务外快速路径
    existing = ledger.find_cancellation_by_key(conn, payload.request_key)
    if existing is not None:
        if int(existing["reservation_id"]) != reservation_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"request_key {payload.request_key} 已用于取消预留 "
                        f"{existing['reservation_id']}，不能再用于预留 {reservation_id}"
                    ),
                    "reason": "request_key_conflict",
                    "reservation_id": reservation_id,
                    "existing_reservation_id": int(existing["reservation_id"]),
                },
            )
        response.status_code = 200
        return ledger.cancellation_payload(conn, int(existing["id"]), replayed=True)

    try:
        with db_module.write_transaction(conn):
            row = ledger.find_cancellation_by_key(conn, payload.request_key)
            if row is not None:
                if int(row["reservation_id"]) != reservation_id:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": (
                                f"request_key {payload.request_key} 已用于取消预留 "
                                f"{row['reservation_id']}，不能再用于预留 {reservation_id}"
                            ),
                            "reason": "request_key_conflict",
                            "reservation_id": reservation_id,
                            "existing_reservation_id": int(row["reservation_id"]),
                        },
                    )
                response.status_code = 200
                return ledger.cancellation_payload(conn, int(row["id"]), replayed=True)
            cancellation_id = ledger.apply_cancellation(
                conn, reservation_id, payload.request_key
            )
    except ledger.ReservationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ledger.ReservationCancelConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": exc.message,
                "reason": exc.reason,
                "reservation_id": exc.reservation_id,
                "status": exc.status,
            },
        )
    except sqlite3.IntegrityError:
        row = ledger.find_cancellation_by_key(conn, payload.request_key)
        if row is not None:
            if int(row["reservation_id"]) != reservation_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            f"request_key {payload.request_key} 已用于取消预留 "
                            f"{row['reservation_id']}，不能再用于预留 {reservation_id}"
                        ),
                        "reason": "request_key_conflict",
                        "reservation_id": reservation_id,
                        "existing_reservation_id": int(row["reservation_id"]),
                    },
                )
            response.status_code = 200
            return ledger.cancellation_payload(conn, int(row["id"]), replayed=True)
        raise HTTPException(
            status_code=409,
            detail={"message": "取消预留与现有记录冲突", "reason": "cancel_conflict"},
        )

    response.status_code = 201
    return ledger.cancellation_payload(conn, cancellation_id)

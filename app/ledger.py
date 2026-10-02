"""谱系台账核心逻辑：所有体积运算一律使用 Decimal，不用 float。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

# 统一量化到小数点后 9 位，避免 1/3 这类无限循环小数在库中无限膨胀。
QUANTUM = Decimal("0.000000001")
ZERO = Decimal("0")


class InsufficientVolume(Exception):
    """某一步吸取体积超过来源孔当前体积，触发整批回滚。"""

    def __init__(self, well: str, available: Decimal, requested: Decimal) -> None:
        self.well = well
        self.available = available
        self.requested = requested
        super().__init__(f"孔位 {well} 体积不足：可用 {available}，请求吸取 {requested}")


class WellNotFound(Exception):
    """孔位尚未登记。"""

    def __init__(self, well: str) -> None:
        self.well = well
        super().__init__(f"孔位 {well} 尚未登记")


class CapacityExceeded(Exception):
    """补液/移液将使目标孔超过其容量上限：补液整体拒绝，批次整批回滚。"""

    def __init__(
        self, well: str, volume: Decimal, adding: Decimal, max_capacity: Decimal
    ) -> None:
        self.well = well
        self.volume = volume  # 加入之前的当时体积
        self.adding = adding  # 拟加入量
        self.max_capacity = max_capacity
        super().__init__(
            f"孔位 {well} 容量超限：当前体积 {volume}，拟加入 {adding}，上限 {max_capacity}"
        )


class CapacityConflict(Exception):
    """新容量上限低于孔位当前体积：拒绝变更，容量配置保持不变。"""

    def __init__(self, well: str, volume: Decimal, max_capacity: Decimal) -> None:
        self.well = well
        self.volume = volume
        self.max_capacity = max_capacity
        super().__init__(
            f"孔位 {well} 当前体积 {volume} 高于新容量上限 {max_capacity}，容量配置不变"
        )


class WellQuarantined(Exception):
    """孔位处于隔离状态：禁止补液、作为批次来源/目标、修改容量、预留体积
    或撤销触及它的批次。

    wells 为 _active_quarantine_map 命中的行（含 case_id），冲突时液体、
    批次与审计记录均不变。
    """

    def __init__(self, wells: list[dict]) -> None:
        self.wells = [{"well": w["well"], "case_id": w["case_id"]} for w in wells]
        labels = "、".join(f"{w['well']}（案件 {w['case_id']}）" for w in self.wells)
        super().__init__(f"孔位 {labels} 处于隔离状态，禁止该操作")


def q(value: Decimal) -> Decimal:
    """量化到统一精度。quantize 单调不减，组分量不会被放大到超过原值。"""
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def dec_str(value: Decimal) -> str:
    """Decimal -> 紧凑字符串（去掉尾随 0，避免科学计数法）。"""
    return format(value.normalize(), "f")


# ---------------------------------------------------------------- 孔位与组成

def ensure_sample(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO samples (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM samples WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def ensure_well(conn: sqlite3.Connection, well: str) -> None:
    conn.execute("INSERT OR IGNORE INTO wells (well, volume) VALUES (?, '0')", (well,))


def get_well_volume(conn: sqlite3.Connection, well: str) -> Decimal | None:
    row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
    return None if row is None else Decimal(row["volume"])


def get_well_capacity(conn: sqlite3.Connection, well: str) -> Decimal | None:
    """孔位容量上限；孔未登记或不限（NULL）时返回 None。"""
    row = conn.execute("SELECT max_capacity FROM wells WHERE well = ?", (well,)).fetchone()
    if row is None or row["max_capacity"] is None:
        return None
    return Decimal(row["max_capacity"])


def set_well_capacity(conn: sqlite3.Connection, well: str, capacity: Decimal | None) -> None:
    """设置（capacity）或取消（None）孔位容量上限。

    孔位处于隔离状态时禁止变更（抛 WellQuarantined）；新上限低于当前体积
    时抛 CapacityConflict，配置保持不变；孔未登记抛 WellNotFound。调用方
    须在写事务内调用，使容量变更与补液/批次/撤销/隔离变更串行。
    """
    row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
    if row is None:
        raise WellNotFound(well)
    quarantined = _active_quarantine_map(conn, [well])
    if quarantined:
        raise WellQuarantined([quarantined[well]])
    volume = Decimal(row["volume"])
    if capacity is not None and capacity < volume:
        raise CapacityConflict(well, volume, capacity)
    conn.execute(
        "UPDATE wells SET max_capacity = ? WHERE well = ?",
        (None if capacity is None else dec_str(capacity), well),
    )


def _composition_amount(conn: sqlite3.Connection, well: str, sample_id: int) -> Decimal:
    row = conn.execute(
        "SELECT amount FROM well_composition WHERE well = ? AND sample_id = ?",
        (well, sample_id),
    ).fetchone()
    return Decimal(row["amount"]) if row else ZERO


def _set_composition(conn: sqlite3.Connection, well: str, sample_id: int, amount: Decimal) -> None:
    if amount == 0:
        conn.execute(
            "DELETE FROM well_composition WHERE well = ? AND sample_id = ?",
            (well, sample_id),
        )
    else:
        conn.execute(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(well, sample_id) DO UPDATE SET amount = excluded.amount",
            (well, sample_id, dec_str(amount)),
        )


def _set_volume(conn: sqlite3.Connection, well: str, volume: Decimal) -> None:
    conn.execute("UPDATE wells SET volume = ? WHERE well = ?", (dec_str(volume), well))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(
    conn: sqlite3.Connection,
    well: str,
    event_type: str,
    *,
    batch_id: int | None = None,
    sample: str | None = None,
    volume: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO well_events (well, type, batch_id, sample, volume, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (well, event_type, batch_id, sample, volume, _now()),
    )


def register_initial_sample(conn: sqlite3.Connection, well: str, sample: str, volume: Decimal) -> None:
    """登记初始样本；孔位已有液体时视为补液并立即混匀。

    孔位处于隔离状态时禁止补液（抛 WellQuarantined）；设有容量上限时，
    补液后的体积超过上限即抛 CapacityExceeded，由调用方回滚，液体状态不变。
    """
    quarantined = _active_quarantine_map(conn, [well])
    if quarantined:
        raise WellQuarantined([quarantined[well]])
    ensure_well(conn, well)
    sample_id = ensure_sample(conn, sample)
    current = get_well_volume(conn, well)
    added = q(volume)
    capacity = get_well_capacity(conn, well)
    if capacity is not None and current + added > capacity:
        raise CapacityExceeded(well, current, added, capacity)
    _set_volume(conn, well, current + added)
    _set_composition(conn, well, sample_id, q(_composition_amount(conn, well, sample_id) + volume))
    _record_event(conn, well, "top_up", sample=sample, volume=dec_str(added))


def _allocate_move(
    amounts: list[tuple[int, Decimal]], move_units: int
) -> dict[int, int]:
    """按比例把 move_units 个最小计量单位（QUANTUM）分配给来源孔的各样本。

    先取比例份额 amount_i * move_units / total 的整数单位，再把剩余单位
    按小数余量从大到小分配；余量相同时按样本 ID 升序。各样本分得的单位数
    必然不超过其原有单位数：若 floor(x_i) 已等于 units_i，则 x_i 是整数，
    余量为 0，不会再分到额外单位。
    """
    total_units = sum(int(amount / QUANTUM) for _, amount in amounts)
    scaled = [
        (sample_id, int(amount / QUANTUM) * move_units) for sample_id, amount in amounts
    ]
    allocated = {sample_id: value // total_units for sample_id, value in scaled}
    remaining = move_units - sum(allocated.values())
    if remaining:
        ordered = sorted(scaled, key=lambda item: (-(item[1] % total_units), item[0]))
        for sample_id, _ in ordered[:remaining]:
            allocated[sample_id] += 1
    return allocated


def apply_transfer(
    conn: sqlite3.Connection,
    source: str,
    destination: str,
    volume: Decimal,
    reserved_by_others: Decimal = ZERO,
) -> None:
    """执行一次移液：按来源组成占比，以最小计量单位（QUANTUM）整数取走各样本。

    用最大余量法分配整数单位，保证实际带走的组分量之和恰好等于请求体积，
    从而来源孔和目标孔在每一步都满足「组分量之和 = 体积」。

    reserved_by_others 是该来源孔上本批次无权动用的体积（未绑定批次时为
    全部生效预留量之和；绑定预留时为「其他」生效预留量之和）。可吸取量
    因此为 src_volume - reserved_by_others，低于吸取量即 InsufficientVolume。
    """
    volume = q(volume)
    src_volume = get_well_volume(conn, source) or ZERO
    usable = src_volume - reserved_by_others
    if usable < volume:
        raise InsufficientVolume(source, usable, volume)

    ensure_well(conn, destination)
    rows = conn.execute(
        "SELECT sample_id, amount FROM well_composition WHERE well = ? ORDER BY sample_id",
        (source,),
    ).fetchall()
    amounts = [(row["sample_id"], Decimal(row["amount"])) for row in rows]

    move_units = int(volume / QUANTUM)
    moved_units = _allocate_move(amounts, move_units) if move_units else {}

    moved_total = ZERO
    for sample_id, amount in amounts:
        moved = QUANTUM * moved_units.get(sample_id, 0)
        moved_total += moved
        _set_composition(conn, source, sample_id, amount - moved)
        if moved:
            _set_composition(
                conn,
                destination,
                sample_id,
                _composition_amount(conn, destination, sample_id) + moved,
            )

    # 目标孔容量按本步执行时的当时体积校验：同一批次先移出再移入因此可行；
    # 超限抛 CapacityExceeded，由调用方回滚整批。
    dst_volume = get_well_volume(conn, destination)
    capacity = get_well_capacity(conn, destination)
    if capacity is not None and dst_volume + moved_total > capacity:
        raise CapacityExceeded(destination, dst_volume, moved_total, capacity)

    _set_volume(conn, source, src_volume - moved_total)
    _set_volume(conn, destination, dst_volume + moved_total)


def well_state(conn: sqlite3.Connection, well: str) -> dict | None:
    """孔位当前体积、容量上限（None 为不限）与各初始样本的量与占比。"""
    row = conn.execute(
        "SELECT volume, max_capacity FROM wells WHERE well = ?", (well,)
    ).fetchone()
    if row is None:
        return None
    volume = Decimal(row["volume"])
    rows = conn.execute(
        "SELECT s.name AS sample, wc.amount AS amount "
        "FROM well_composition wc JOIN samples s ON s.id = wc.sample_id "
        "WHERE wc.well = ? ORDER BY s.name",
        (well,),
    ).fetchall()
    composition = []
    for row2 in rows:
        amount = Decimal(row2["amount"])
        fraction = q(amount / volume) if volume > 0 else ZERO
        composition.append(
            {"sample": row2["sample"], "amount": dec_str(amount), "fraction": dec_str(fraction)}
        )
    reserved_total = active_reserved_total(conn, well)
    available = volume - reserved_total
    if available < ZERO:
        available = ZERO
    return {
        "well": well,
        "volume": dec_str(volume),
        "max_capacity": (
            None if row["max_capacity"] is None else dec_str(Decimal(row["max_capacity"]))
        ),
        "quarantine": well_quarantine(conn, well),
        # 生效预留（active）占用的总体积；普通补液与未绑定批次不可动用
        "reserved_volume": dec_str(reserved_total),
        # 普通补液/未绑定批次实际可动用的体积 = 当前体积 - 生效预留量
        "available_volume": dec_str(available),
        "reservations": active_reservation_items(conn, well),
        "composition": composition,
    }


# ---------------------------------------------------------------- 批次

def find_batch_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE request_key = ?", (request_key,)).fetchone()


def insert_batch(
    conn: sqlite3.Connection,
    request_key: str,
    status: str,
    error: str | None = None,
    reservation_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO batches (request_key, status, error, reservation_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (request_key, status, error, reservation_id, _now()),
    )
    return int(cur.lastrowid)


def _save_pre_batch_snapshot(conn: sqlite3.Connection, batch_id: int, wells: list[str]) -> None:
    """保存该批次首次触及各孔在任何移液发生前的完整快照（体积 + 各样本量）。"""
    for well in wells:
        row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
        volume = None if row is None else row["volume"]
        conn.execute(
            "INSERT INTO batch_well_snapshots (batch_id, well, volume) VALUES (?, ?, ?)",
            (batch_id, well, volume),
        )
        comp_rows = conn.execute(
            "SELECT sample_id, amount FROM well_composition WHERE well = ?",
            (well,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO batch_snapshot_composition (batch_id, well, sample_id, amount) "
            "VALUES (?, ?, ?, ?)",
            [(batch_id, well, r["sample_id"], r["amount"]) for r in comp_rows],
        )


def execute_batch(
    conn: sqlite3.Connection,
    request_key: str,
    operations: list,
    reservation_id: int | None = None,
) -> int:
    """在调用方的事务内按提交顺序执行；任一步抛异常由调用方回滚整批。

    任一触及孔处于隔离状态即抛 WellQuarantined——在写入任何批次记录之前，
    因此隔离冲突不落失败批次、不消耗 request_key。

    携带 reservation_id 时（绑定预留批次）：预留必须存在且为 active，且批次
    从每个预留来源孔吸取的体积之和与预留体积严格相等（预留的每个孔也都必须
    作为来源出现），否则抛 ReservationFulfillmentMismatch；此时本批次有权
    动用该预留的体积，但仍不能动用同孔其他生效预留的体积。未绑定预留的普通
    批次不能动用任何生效预留体积。只有逐孔逐量完全兑现并成功提交时，才在同
    一事务把预留标记为 consumed；任何失败随事务回滚，预留不被消耗。
    """
    touched_wells = list(dict.fromkeys(
        well
        for op in operations
        for well in (op.source, op.destination)
    ))
    quarantined = _active_quarantine_map(conn, touched_wells)
    if quarantined:
        raise WellQuarantined([quarantined[w] for w in touched_wells if w in quarantined])

    own_items: dict[str, Decimal] = {}
    if reservation_id is not None:
        res = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if res is None:
            raise ReservationNotFound(reservation_id)
        if res["status"] != "active":
            raise ReservationNotActive(reservation_id, res["status"])
        own_items = {
            r["well"]: Decimal(r["volume"])
            for r in conn.execute(
                "SELECT well, volume FROM reservation_items WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchall()
        }
        requested_by_well: dict[str, Decimal] = {}
        for op in operations:
            requested_by_well[op.source] = requested_by_well.get(op.source, ZERO) + q(
                op.volume
            )
        mismatches = _fulfillment_mismatches(own_items, requested_by_well)
        if mismatches:
            raise ReservationFulfillmentMismatch(reservation_id, mismatches)

    # 各来源孔上本批次无权动用的生效预留量：绑定预留时扣除本预留自身占用的
    # 部分（它正是为这个批次预留的）；未绑定时全部生效预留均不可动用。
    source_wells = list(dict.fromkeys(op.source for op in operations))
    active_map = active_reserved_map(conn, source_wells)
    held_by_others: dict[str, Decimal] = {}
    for well in source_wells:
        held = active_map.get(well, ZERO) - own_items.get(well, ZERO)
        held_by_others[well] = held if held > ZERO else ZERO

    batch_id = insert_batch(
        conn, request_key, "committed", reservation_id=reservation_id
    )
    # 先于任何移液保存批前快照，保证与最终提交同事务、可整体回滚。
    _save_pre_batch_snapshot(conn, batch_id, touched_wells)
    for step, op in enumerate(operations, start=1):
        volume = q(op.volume)
        apply_transfer(
            conn, op.source, op.destination, volume, held_by_others[op.source]
        )
        conn.execute(
            "INSERT INTO operations (batch_id, step, source, destination, volume) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, step, op.source, op.destination, dec_str(volume)),
        )
    for well in touched_wells:
        _record_event(conn, well, "batch", batch_id=batch_id)
    if reservation_id is not None:
        # 与批次同一事务：兑现与标记 consumed 原子，失败不消耗预留。
        mark_reservation_consumed(conn, reservation_id, batch_id)
    return batch_id


def _fulfillment_mismatches(
    reserved: dict[str, Decimal], requested: dict[str, Decimal]
) -> list[dict]:
    """逐孔对比预留体积与批次实际吸取量；完全兑现（孔集与体积均相等）返回 []。"""
    mismatches: list[dict] = []
    for well in sorted(set(reserved) | set(requested)):
        reserved_volume = reserved.get(well)
        requested_volume = requested.get(well)
        if reserved_volume != requested_volume:
            mismatches.append(
                {
                    "well": well,
                    "reserved": None if reserved_volume is None else dec_str(reserved_volume),
                    "requested": None if requested_volume is None else dec_str(requested_volume),
                }
            )
    return mismatches


def batch_payload(conn: sqlite3.Connection, batch_id: int, replayed: bool = False) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    ops = conn.execute(
        "SELECT step, source, destination, volume FROM operations "
        "WHERE batch_id = ? ORDER BY step",
        (batch_id,),
    ).fetchall()
    reversal = conn.execute(
        "SELECT reversed_at FROM reversals WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return {
        "batch_id": row["id"],
        "request_key": row["request_key"],
        "status": row["status"],
        "error": row["error"],
        "reservation_id": row["reservation_id"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "reversed": reversal is not None,
        "reversed_at": reversal["reversed_at"] if reversal else None,
        "operations": [dict(op) for op in ops],
    }


# ---------------------------------------------------------------- 批次撤销

class ReversalConflict(Exception):
    """批次当前不可撤销（已撤销/无快照/存在后续阻塞操作/快照恢复后超容量上限）。"""

    def __init__(
        self,
        reason: str,
        message: str,
        blockers: list[dict] | None = None,
        exceeding: list[dict] | None = None,
    ) -> None:
        self.reason = reason
        self.message = message
        self.blockers = blockers or []
        self.exceeding = exceeding or []
        super().__init__(message)


def find_reversal_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reversals WHERE request_key = ?", (request_key,)
    ).fetchone()


def get_reversal(conn: sqlite3.Connection, reversal_id: int, replayed: bool = False) -> dict | None:
    row = conn.execute(
        "SELECT * FROM reversals WHERE id = ?", (reversal_id,)
    ).fetchone()
    if row is None:
        return None
    wells = [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
            (row["batch_id"],),
        ).fetchall()
    ]
    return {
        "reversal_id": row["id"],
        "batch_id": row["batch_id"],
        "request_key": row["request_key"],
        "reversed_at": row["reversed_at"],
        "replayed": replayed,
        "restored_wells": wells,
        "batch": batch_payload(conn, row["batch_id"]),
    }


def _batch_blockers(conn: sqlite3.Connection, batch_id: int, wells: list[str]) -> list[dict]:
    """批次之后触及这些孔的补液（top_up）与已提交批次（batch）事件。

    阻塞基准是「该批次在这个孔上的事件 id」——批次自身在其他孔的事件
    id 可能更大，但不算后续操作。
    """
    blockers: list[dict] = []
    for well in wells:
        marker = conn.execute(
            "SELECT id FROM well_events WHERE type = 'batch' AND batch_id = ? AND well = ?",
            (batch_id, well),
        ).fetchone()
        if marker is None:
            continue
        rows = conn.execute(
            "SELECT e.type AS type, e.batch_id AS blocker_batch_id, b.request_key AS request_key, "
            "       e.sample AS sample, e.volume AS volume "
            "FROM well_events e LEFT JOIN batches b ON b.id = e.batch_id "
            "WHERE e.well = ? AND e.id > ? ORDER BY e.id",
            (well, marker["id"]),
        ).fetchall()
        if not rows:
            continue
        events = []
        for r in rows:
            event: dict = {"type": r["type"]}
            if r["type"] == "top_up":
                event["sample"] = r["sample"]
                event["volume"] = r["volume"]
            else:
                event["batch_id"] = r["blocker_batch_id"]
                event["request_key"] = r["request_key"]
            events.append(event)
        blockers.append({"well": well, "events": events})
    return blockers


def _restore_snapshot(conn: sqlite3.Connection, batch_id: int) -> None:
    """把该批次快照中的每个孔原子恢复到批前状态（含删除批次中新出现的孔）。"""
    snaps = conn.execute(
        "SELECT well, volume FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    for snap in snaps:
        well = snap["well"]
        # 先清组成再动孔，遵守外键约束。
        conn.execute("DELETE FROM well_composition WHERE well = ?", (well,))
        if snap["volume"] is None:
            conn.execute("DELETE FROM wells WHERE well = ?", (well,))
            continue
        conn.execute("UPDATE wells SET volume = ? WHERE well = ?", (snap["volume"], well))
        comp_rows = conn.execute(
            "SELECT sample_id, amount FROM batch_snapshot_composition "
            "WHERE batch_id = ? AND well = ? ORDER BY sample_id",
            (batch_id, well),
        ).fetchall()
        conn.executemany(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?)",
            [(well, r["sample_id"], r["amount"]) for r in comp_rows],
        )


def apply_reversal(conn: sqlite3.Connection, batch_id: int, request_key: str) -> int:
    """在调用方事务内完成可撤销性校验、快照恢复与审计记录写入。

    校验不通过抛 ReversalConflict 或 WellQuarantined（由调用方随事务回滚，
    液体状态不变）。
    """
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ReversalConflict("not_found", f"批次 {batch_id} 不存在")
    if row["status"] != "committed":
        raise ReversalConflict(
            "not_committed",
            f"批次 {batch_id} 状态为 {row['status']}，仅已提交批次可撤销",
        )
    snaps = conn.execute(
        "SELECT well, volume FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    if not snaps:
        raise ReversalConflict(
            "no_snapshot",
            f"批次 {batch_id} 是上线前提交的批次，没有批前快照，不可撤销",
        )
    if conn.execute(
        "SELECT 1 FROM reversals WHERE batch_id = ?", (batch_id,)
    ).fetchone():
        raise ReversalConflict("already_reversed", f"批次 {batch_id} 已经撤销，不能重复撤销")
    wells = [r["well"] for r in snaps]
    # 隔离孔禁止撤销触及它的批次：恢复快照会改变被隔离孔的液体状态。
    quarantined = _active_quarantine_map(conn, wells)
    if quarantined:
        raise WellQuarantined([quarantined[w] for w in wells if w in quarantined])
    blockers = _batch_blockers(conn, batch_id, wells)
    if blockers:
        raise ReversalConflict(
            "blocked",
            f"批次 {batch_id} 之后有补液或后续批次触及相关孔位，不可撤销",
            blockers,
        )

    # 恢复快照前核对：任一快照孔恢复后的体积超过其当前容量上限，即整次
    # 拒绝撤销（液体与审计记录均不变）。快照为 NULL 的孔恢复后即删除，无需核对。
    exceeding = []
    for snap in snaps:
        if snap["volume"] is None:
            continue
        capacity = get_well_capacity(conn, snap["well"])
        if capacity is not None and Decimal(snap["volume"]) > capacity:
            exceeding.append(
                {
                    "well": snap["well"],
                    "volume": dec_str(Decimal(snap["volume"])),
                    "max_capacity": dec_str(capacity),
                }
            )
    if exceeding:
        raise ReversalConflict(
            "capacity_exceeded",
            f"批次 {batch_id} 撤销后部分孔位将超过当前容量上限，不可撤销",
            exceeding=exceeding,
        )

    # 恢复快照前同样要保证「体积不低于生效预留量」的不变量：恢复后被预留
    # 的液体不得少于各孔 active 预留之和。快照为 NULL 的孔（恢复即删除）上
    # 若仍有生效预留，同样阻塞撤销。
    reservation_blockers = _reservation_restore_blockers(conn, snaps)
    if reservation_blockers:
        raise ReversalConflict(
            "reservations_active",
            f"批次 {batch_id} 撤销后部分孔位液体不足以覆盖生效预留，不可撤销",
            blockers=reservation_blockers,
        )

    _restore_snapshot(conn, batch_id)
    cur = conn.execute(
        "INSERT INTO reversals (batch_id, request_key, reversed_at) VALUES (?, ?, ?)",
        (batch_id, request_key, _now()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 孔位隔离

class QuarantineConflict(Exception):
    """隔离申请冲突：部分孔已处于隔离状态（整次申请拒绝，不留部分状态）。"""

    def __init__(self, conflicts: list[dict]) -> None:
        self.conflicts = [{"well": w["well"], "case_id": w["case_id"]} for w in conflicts]
        labels = "、".join(f"{w['well']}（案件 {w['case_id']}）" for w in self.conflicts)
        super().__init__(f"孔位 {labels} 已处于隔离状态，整次申请拒绝")


class ReleaseConflict(Exception):
    """解除隔离不匹配：孔未处于隔离状态，或隔离案件与给定 case_id 不符。"""

    def __init__(self, mismatches: list[dict]) -> None:
        self.mismatches = mismatches
        parts = []
        for m in mismatches:
            if m["reason"] == "not_quarantined":
                parts.append(f"{m['well']}（未隔离）")
            else:
                parts.append(f"{m['well']}（隔离案件为 {m['case_id']}）")
        super().__init__(f"孔位 {'、'.join(parts)} 与解除请求不匹配，整次解除拒绝")


def _active_quarantine_map(conn: sqlite3.Connection, wells: list[str]) -> dict[str, dict]:
    """这些孔中当前处于隔离状态的孔 -> 隔离信息（含 request_id/case_id/request_key）。"""
    if not wells:
        return {}
    placeholders = ", ".join("?" for _ in wells)
    rows = conn.execute(
        "SELECT qw.well AS well, qw.request_id AS request_id, "
        "       qr.case_id AS case_id, qr.request_key AS request_key "
        "FROM quarantine_wells qw "
        "JOIN quarantine_requests qr ON qr.id = qw.request_id "
        "WHERE qw.released_at IS NULL AND qw.well IN (" + placeholders + ")",
        list(wells),
    ).fetchall()
    return {row["well"]: dict(row) for row in rows}


def well_quarantine(conn: sqlite3.Connection, well: str) -> dict | None:
    """孔位当前隔离信息；未隔离返回 None。"""
    row = conn.execute(
        "SELECT qr.case_id AS case_id, qr.reason AS reason, qr.request_key AS request_key, "
        "       qr.created_at AS quarantined_at "
        "FROM quarantine_wells qw JOIN quarantine_requests qr ON qr.id = qw.request_id "
        "WHERE qw.well = ? AND qw.released_at IS NULL",
        (well,),
    ).fetchone()
    return None if row is None else dict(row)


def find_quarantine_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM quarantine_requests WHERE request_key = ?", (request_key,)
    ).fetchone()


def find_release_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM quarantine_releases WHERE request_key = ?", (request_key,)
    ).fetchone()


def apply_quarantine(
    conn: sqlite3.Connection,
    request_key: str,
    case_id: str,
    reason: str,
    wells: list[str],
) -> int:
    """在调用方事务内校验并原子建立隔离记录。

    任一孔未登记（WellNotFound）或已处于隔离状态（QuarantineConflict）即抛出，
    由调用方回滚，整次申请不留部分状态、不消耗 request_key。
    """
    for well in wells:
        if get_well_volume(conn, well) is None:
            raise WellNotFound(well)
    active = _active_quarantine_map(conn, wells)
    if active:
        raise QuarantineConflict([active[w] for w in wells if w in active])
    # 隔离申请与预留建立/兑现/取消串行：孔上存在生效预留时拒绝隔离
    # （被预留的液体已有归属，隔离会使兑现批次无法执行），逐孔列出阻塞预留。
    blocked = active_reservation_blockers(conn, wells)
    if blocked:
        raise ReservationsBlockQuarantine(blocked)
    cur = conn.execute(
        "INSERT INTO quarantine_requests (request_key, case_id, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (request_key, case_id, reason, _now()),
    )
    request_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO quarantine_wells (request_id, well) VALUES (?, ?)",
        [(request_id, well) for well in wells],
    )
    return request_id


def apply_release(
    conn: sqlite3.Connection,
    request_key: str,
    case_id: str,
    wells: list[str],
) -> int:
    """在调用方事务内校验并原子解除隔离，追加解除审计（申请记录保留）。

    任一孔未处于隔离状态或隔离案件与 case_id 不符即抛 ReleaseConflict，
    由调用方回滚，整次解除不留部分状态、不消耗 request_key。
    """
    active = _active_quarantine_map(conn, wells)
    mismatches = []
    for well in wells:
        row = active.get(well)
        if row is None:
            mismatches.append({"well": well, "reason": "not_quarantined"})
        elif row["case_id"] != case_id:
            mismatches.append(
                {"well": well, "reason": "case_mismatch", "case_id": row["case_id"]}
            )
    if mismatches:
        raise ReleaseConflict(mismatches)
    now = _now()
    cur = conn.execute(
        "INSERT INTO quarantine_releases (request_key, case_id, released_at) VALUES (?, ?, ?)",
        (request_key, case_id, now),
    )
    release_id = int(cur.lastrowid)
    for well in wells:
        conn.execute(
            "UPDATE quarantine_wells SET released_at = ? "
            "WHERE well = ? AND released_at IS NULL",
            (now, well),
        )
        conn.execute(
            "INSERT INTO quarantine_release_wells (release_id, well, request_id) "
            "VALUES (?, ?, ?)",
            (release_id, well, active[well]["request_id"]),
        )
    return release_id


def quarantine_payload(conn: sqlite3.Connection, request_id: int, replayed: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM quarantine_requests WHERE id = ?", (request_id,)
    ).fetchone()
    wells = conn.execute(
        "SELECT well, released_at FROM quarantine_wells WHERE request_id = ? ORDER BY well",
        (request_id,),
    ).fetchall()
    return {
        "quarantine_id": row["id"],
        "request_key": row["request_key"],
        "case_id": row["case_id"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "wells": [
            {
                "well": w["well"],
                "status": "released" if w["released_at"] is not None else "quarantined",
                "released_at": w["released_at"],
            }
            for w in wells
        ],
    }


def release_payload(conn: sqlite3.Connection, release_id: int, replayed: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM quarantine_releases WHERE id = ?", (release_id,)
    ).fetchone()
    wells = conn.execute(
        "SELECT well, request_id FROM quarantine_release_wells WHERE release_id = ? "
        "ORDER BY well",
        (release_id,),
    ).fetchall()
    return {
        "release_id": row["id"],
        "request_key": row["request_key"],
        "case_id": row["case_id"],
        "released_at": row["released_at"],
        "replayed": replayed,
        "wells": [
            {"well": w["well"], "quarantine_id": w["request_id"]} for w in wells
        ],
    }


# ---------------------------------------------------------------- 移液体积预留


class ReservationUnavailable(Exception):
    """预留建立时部分孔可用体积不足（扣除其他生效预留后）：整次预留回滚。"""

    def __init__(self, conflicts: list[dict]) -> None:
        self.conflicts = conflicts
        parts = [
            f"{c['well']}（可用 {c['available']}，请求预留 {c['requested']}，"
            f"已预留 {c['already_reserved']}）"
            for c in conflicts
        ]
        super().__init__(f"孔位 {'、'.join(parts)} 可预留体积不足，整次预留拒绝")


class ReservationNotFound(Exception):
    """预留编号不存在。"""

    def __init__(self, reservation_id: int) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"预留 {reservation_id} 不存在")


class ReservationNotActive(Exception):
    """预留不是生效状态（已消费/已取消），不能再被批次兑现或取消。"""

    def __init__(self, reservation_id: int, status: str) -> None:
        self.reservation_id = reservation_id
        self.status = status
        super().__init__(f"预留 {reservation_id} 当前状态为 {status}，不是生效预留")


class ReservationFulfillmentMismatch(Exception):
    """绑定预留的批次未逐孔逐量完全兑现：整批失败，预留不被消耗。"""

    def __init__(self, reservation_id: int, mismatches: list[dict]) -> None:
        self.reservation_id = reservation_id
        self.mismatches = mismatches
        parts = [
            f"{m['well']}（预留 {m['reserved']}，批次吸取 "
            f"{m['requested'] if m['requested'] is not None else '0'}）"
            for m in mismatches
        ]
        super().__init__(
            f"批次未逐孔逐量兑现预留 {reservation_id}：{'、'.join(parts)}，整批拒绝"
        )


class ReservationCancelConflict(Exception):
    """取消请求不合法：预留不存在/已消费/已取消。"""

    def __init__(
        self, reason: str, message: str, reservation_id: int, status: str | None = None
    ) -> None:
        self.reason = reason
        self.message = message
        self.reservation_id = reservation_id
        self.status = status
        super().__init__(message)


class ReservationsBlockQuarantine(Exception):
    """隔离申请被生效预留阻塞：逐孔列出阻塞预留，整次隔离拒绝。"""

    def __init__(self, blockers: list[dict]) -> None:
        self.blockers = blockers
        labels = "、".join(b["well"] for b in blockers)
        super().__init__(f"孔位 {labels} 上存在生效预留，整次隔离申请拒绝")


def active_reserved_map(conn: sqlite3.Connection, wells: list[str]) -> dict[str, Decimal]:
    """这些孔上当前 active 预留占用的体积之和（一孔可能被多个预留部分占用）。"""
    if not wells:
        return {}
    placeholders = ", ".join("?" for _ in wells)
    rows = conn.execute(
        "SELECT ri.well AS well, ri.volume AS volume "
        "FROM reservation_items ri "
        "JOIN reservations r ON r.id = ri.reservation_id "
        "WHERE r.status = 'active' AND ri.well IN (" + placeholders + ")",
        list(wells),
    ).fetchall()
    totals: dict[str, Decimal] = {well: ZERO for well in wells}
    for row in rows:
        totals[row["well"]] += Decimal(row["volume"])
    return totals


def active_reserved_total(conn: sqlite3.Connection, well: str) -> Decimal:
    """单孔当前 active 预留占用的体积之和。"""
    return active_reserved_map(conn, [well]).get(well, ZERO)


def active_reservation_items(conn: sqlite3.Connection, well: str) -> list[dict]:
    """孔上各条生效预留的明细（预留编号、request_key 与本孔预留体积）。"""
    rows = conn.execute(
        "SELECT r.id AS reservation_id, r.request_key AS request_key, "
        "       ri.volume AS volume, r.created_at AS reserved_at "
        "FROM reservation_items ri "
        "JOIN reservations r ON r.id = ri.reservation_id "
        "WHERE ri.well = ? AND r.status = 'active' "
        "ORDER BY r.id",
        (well,),
    ).fetchall()
    return [
        {
            "reservation_id": row["reservation_id"],
            "request_key": row["request_key"],
            "volume": row["volume"],
            "reserved_at": row["reserved_at"],
        }
        for row in rows
    ]


def active_reservation_blockers(conn: sqlite3.Connection, wells: list[str]) -> list[dict]:
    """这些孔上存在生效预留的孔 -> 阻塞明细（隔离申请整次拒绝时逐孔列出）。"""
    blockers: list[dict] = []
    for well in wells:
        items = active_reservation_items(conn, well)
        if items:
            blockers.append(
                {
                    "well": well,
                    "reserved_volume": dec_str(
                        sum((Decimal(i["volume"]) for i in items), ZERO)
                    ),
                    "reservations": [
                        {
                            "reservation_id": i["reservation_id"],
                            "request_key": i["request_key"],
                            "volume": i["volume"],
                        }
                        for i in items
                    ],
                }
            )
    return blockers


def _reservation_restore_blockers(
    conn: sqlite3.Connection, snaps: list[sqlite3.Row]
) -> list[dict]:
    """撤销恢复快照后体积将低于生效预留量（或孔将被删除但仍有生效预留）的孔。"""
    blockers: list[dict] = []
    for snap in snaps:
        well = snap["well"]
        reserved = active_reserved_total(conn, well)
        if reserved == ZERO:
            continue
        restored = ZERO if snap["volume"] is None else Decimal(snap["volume"])
        if restored < reserved:
            blockers.append(
                {
                    "well": well,
                    "restored_volume": dec_str(restored),
                    "reserved_volume": dec_str(reserved),
                    "reservations": [
                        {
                            "reservation_id": i["reservation_id"],
                            "request_key": i["request_key"],
                            "volume": i["volume"],
                        }
                        for i in active_reservation_items(conn, well)
                    ],
                }
            )
    return blockers


def find_reservation_by_key(
    conn: sqlite3.Connection, request_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reservations WHERE request_key = ?", (request_key,)
    ).fetchone()


def find_cancellation_by_key(
    conn: sqlite3.Connection, request_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reservation_cancellations WHERE request_key = ?", (request_key,)
    ).fetchone()


def apply_reservation(
    conn: sqlite3.Connection, request_key: str, items: list
) -> int:
    """在调用方事务内校验并原子建立预留。

    任一孔未登记（WellNotFound）、处于隔离状态（WellQuarantined）或扣除其他
    生效预留后可用体积不足（ReservationUnavailable）即抛出，由调用方回滚，
    整次预留不留部分状态、不消耗 request_key。
    """
    wells = [item.well for item in items]
    quarantined = _active_quarantine_map(conn, wells)
    if quarantined:
        raise WellQuarantined([quarantined[w] for w in wells if w in quarantined])
    reserved_map = active_reserved_map(conn, wells)
    conflicts: list[dict] = []
    for item in items:
        volume = get_well_volume(conn, item.well)
        if volume is None:
            raise WellNotFound(item.well)
        requested = q(item.volume)
        already_reserved = reserved_map[item.well]
        available = volume - already_reserved
        if available < requested:
            conflicts.append(
                {
                    "well": item.well,
                    "available": dec_str(available),
                    "already_reserved": dec_str(already_reserved),
                    "requested": dec_str(requested),
                }
            )
    if conflicts:
        raise ReservationUnavailable(conflicts)
    cur = conn.execute(
        "INSERT INTO reservations (request_key, status, created_at) VALUES (?, 'active', ?)",
        (request_key, _now()),
    )
    reservation_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO reservation_items (reservation_id, well, volume) VALUES (?, ?, ?)",
        [
            (reservation_id, item.well, dec_str(q(item.volume)))
            for item in items
        ],
    )
    return reservation_id


def mark_reservation_consumed(
    conn: sqlite3.Connection, reservation_id: int, batch_id: int
) -> None:
    """在批次同一事务内把预留标记为 consumed（调用方已完成 active 与兑现校验）。"""
    conn.execute(
        "UPDATE reservations SET status = 'consumed', consumed_at = ?, "
        "consumed_batch_id = ? WHERE id = ?",
        (_now(), batch_id, reservation_id),
    )


def apply_cancellation(
    conn: sqlite3.Connection, reservation_id: int, request_key: str
) -> int:
    """在调用方事务内整体取消一个 active 预留。

    预留不存在抛 ReservationNotFound；已消费/已取消抛 ReservationCancelConflict
    （由调用方随事务回滚，request_key 不消耗）。
    """
    row = conn.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    if row is None:
        raise ReservationNotFound(reservation_id)
    if row["status"] == "consumed":
        raise ReservationCancelConflict(
            "already_consumed",
            f"预留 {reservation_id} 已被批次 {row['consumed_batch_id']} 消费，不能取消",
            reservation_id,
            "consumed",
        )
    if row["status"] == "cancelled":
        existing = conn.execute(
            "SELECT id FROM reservation_cancellations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        raise ReservationCancelConflict(
            "already_cancelled",
            f"预留 {reservation_id} 已经取消，不能重复取消",
            reservation_id,
            "cancelled",
        )
    now = _now()
    cur = conn.execute(
        "INSERT INTO reservation_cancellations (reservation_id, request_key, cancelled_at) "
        "VALUES (?, ?, ?)",
        (reservation_id, request_key, now),
    )
    cancellation_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE reservations SET status = 'cancelled', cancelled_at = ?, "
        "cancellation_id = ? WHERE id = ?",
        (now, cancellation_id, reservation_id),
    )
    return cancellation_id


def reservation_target(
    conn: sqlite3.Connection, reservation_id: int
) -> list[tuple[str, str]]:
    """预留目标：排序后的 (well, volume) 列表，同键换目标判定的依据。"""
    rows = conn.execute(
        "SELECT well, volume FROM reservation_items WHERE reservation_id = ? ORDER BY well",
        (reservation_id,),
    ).fetchall()
    return [(row["well"], row["volume"]) for row in rows]


def reservation_payload(
    conn: sqlite3.Connection, reservation_id: int, replayed: bool = False
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    if row is None:
        return None
    items = conn.execute(
        "SELECT well, volume FROM reservation_items WHERE reservation_id = ? ORDER BY well",
        (reservation_id,),
    ).fetchall()
    cancellation = None
    if row["cancellation_id"] is not None:
        crow = conn.execute(
            "SELECT id, request_key, cancelled_at FROM reservation_cancellations WHERE id = ?",
            (row["cancellation_id"],),
        ).fetchone()
        if crow is not None:
            cancellation = {
                "cancellation_id": crow["id"],
                "request_key": crow["request_key"],
                "cancelled_at": crow["cancelled_at"],
            }
    return {
        "reservation_id": row["id"],
        "request_key": row["request_key"],
        "status": row["status"],
        "replayed": replayed,
        "created_at": row["created_at"],
        "consumed_at": row["consumed_at"],
        "consumed_batch_id": row["consumed_batch_id"],
        "cancelled_at": row["cancelled_at"],
        "cancellation": cancellation,
        "items": [{"well": item["well"], "volume": item["volume"]} for item in items],
    }


def cancellation_payload(
    conn: sqlite3.Connection, cancellation_id: int, replayed: bool = False
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM reservation_cancellations WHERE id = ?", (cancellation_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "cancellation_id": row["id"],
        "reservation_id": row["reservation_id"],
        "request_key": row["request_key"],
        "cancelled_at": row["cancelled_at"],
        "replayed": replayed,
        "reservation": reservation_payload(conn, row["reservation_id"]),
    }

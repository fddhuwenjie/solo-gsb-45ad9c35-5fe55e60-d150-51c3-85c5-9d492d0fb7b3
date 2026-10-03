"""SQLite 连接与建表。

所有体积/组分量都以 TEXT 存储 Decimal 的字符串形式，
避免 SQLite NUMERIC 亲和性把值转成 REAL 丢失精度。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "lineage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wells (
    well    TEXT PRIMARY KEY,
    volume  TEXT NOT NULL DEFAULT '0',
    -- 可选最大容量：NULL 表示不限；TEXT 存 Decimal 字符串
    max_capacity TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS well_composition (
    well        TEXT NOT NULL REFERENCES wells(well),
    sample_id   INTEGER NOT NULL REFERENCES samples(id),
    amount      TEXT NOT NULL,
    PRIMARY KEY (well, sample_id)
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN ('committed', 'failed')),
    error       TEXT,
    -- 绑定的移液体积预留：仅当逐孔逐量完全兑现时批次才提交并把预留置为 consumed；
    -- NULL 表示未绑定预留的普通批次（不能动用任何被预留体积）。
    reservation_id INTEGER REFERENCES reservations(id),
    -- 由「计划 → 确认」流程提交的批次指向其确认记录；NULL 表示直接提交的批次。
    plan_confirmation_id INTEGER REFERENCES plan_confirmations(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    step        INTEGER NOT NULL,
    source      TEXT NOT NULL,
    destination TEXT NOT NULL,
    volume      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_batch ON operations(batch_id);
CREATE INDEX IF NOT EXISTS idx_operations_well ON operations(source, destination);

-- 孔位变更事件流：批次提交与补液各记一条，自增 id 即发生顺序，
-- 用于判断批次之后是否有补液或后续批次触及同一孔位。
CREATE TABLE IF NOT EXISTS well_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    well       TEXT NOT NULL,
    type       TEXT NOT NULL CHECK (type IN ('top_up', 'batch')),
    batch_id   INTEGER REFERENCES batches(id),
    sample     TEXT,
    volume     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_well_events_well ON well_events(well, id);

-- 成功提交批次时，在同一事务内保存其首次触及各孔的批前快照。
-- volume 为 NULL 表示该孔在批次之前尚不存在（撤销时整体删除）。
CREATE TABLE IF NOT EXISTS batch_well_snapshots (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    well     TEXT NOT NULL,
    volume   TEXT,
    PRIMARY KEY (batch_id, well)
);

CREATE TABLE IF NOT EXISTS batch_snapshot_composition (
    batch_id  INTEGER NOT NULL REFERENCES batches(id),
    well      TEXT NOT NULL,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    amount    TEXT NOT NULL,
    PRIMARY KEY (batch_id, well, sample_id)
);

-- 批次撤销审计记录：不删除原批次，只追加一条关联记录。
CREATE TABLE IF NOT EXISTS reversals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL UNIQUE REFERENCES batches(id),
    request_key TEXT NOT NULL UNIQUE,
    reversed_at TEXT NOT NULL
);

-- 孔位隔离申请：同一事务确认所有孔已登记且尚未隔离后原子建立；
-- 申请记录永久保留（解除后不删除），request_key 用于幂等去重。
CREATE TABLE IF NOT EXISTS quarantine_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    case_id     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 申请涉及的孔，每孔一行；released_at 为 NULL 表示隔离中。
-- 部分唯一索引保证同一孔同一时刻只有一条生效隔离。
CREATE TABLE IF NOT EXISTS quarantine_wells (
    request_id  INTEGER NOT NULL REFERENCES quarantine_requests(id),
    well        TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (request_id, well)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantine_wells_active
    ON quarantine_wells(well) WHERE released_at IS NULL;

-- 解除隔离审计：解除单独追加记录，申请记录保留（孔位行标记 released_at）。
CREATE TABLE IF NOT EXISTS quarantine_releases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    case_id     TEXT NOT NULL,
    released_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine_release_wells (
    release_id INTEGER NOT NULL REFERENCES quarantine_releases(id),
    well       TEXT NOT NULL,
    request_id INTEGER NOT NULL REFERENCES quarantine_requests(id),
    PRIMARY KEY (release_id, well)
);

-- 移液体积预留：操作员用 request_key 为未来批次一次预留多个已登记来源孔的
-- Decimal 体积。同一事务扣除各孔其他生效预留后检查可用量，任一不足、孔已
-- 隔离或同一孔重复即整次失败，不留部分预留。
-- status: active（生效，普通补液/未绑定批次不可动用其体积）
--       consumed（已被绑定批次逐孔逐量完全兑现，同事务标记，不可取消）
--       cancelled（经独立 request_key 整体取消）
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN ('active', 'consumed', 'cancelled')),
    created_at  TEXT NOT NULL,
    consumed_at TEXT,
    consumed_batch_id INTEGER REFERENCES batches(id),
    cancelled_at TEXT,
    cancellation_id INTEGER
);

-- 预留的逐孔体积；同一预留内一孔仅一行（由应用层校验重复孔）。
CREATE TABLE IF NOT EXISTS reservation_items (
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    well      TEXT NOT NULL,
    volume    TEXT NOT NULL,
    PRIMARY KEY (reservation_id, well)
);

CREATE INDEX IF NOT EXISTS idx_reservation_items_well
    ON reservation_items(well);

-- 预留取消审计：只有 active 预留可整体取消；已消费不可取消。
-- 取消 request_key 独立幂等。
CREATE TABLE IF NOT EXISTS reservation_cancellations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL UNIQUE REFERENCES reservations(id),
    request_key    TEXT NOT NULL UNIQUE,
    cancelled_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------- 移液计划（先核对、后确认）
-- 计划：调用方以 request_key 提交 operations（可选 reservation_id），服务在只读
-- 事务固定涉及孔的体积/组成/容量/隔离/生效预留规范快照并完成逐步校验后落库；
-- 计划不写批次、不改变液体。确认时在写事务重读状态，仅当快照各维均未变化才
-- 原子执行并标记 committed；任一变化返回 409，计划保持 awaiting_confirmation、
-- 可原 confirm_key 重试。
CREATE TABLE IF NOT EXISTS transfer_plans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key    TEXT NOT NULL UNIQUE,
    reservation_id INTEGER REFERENCES reservations(id),
    -- awaiting_confirmation（已核对待确认）/ committed（已确认并原子执行）
    status         TEXT NOT NULL CHECK (status IN ('awaiting_confirmation', 'committed')),
    created_at     TEXT NOT NULL,
    committed_at   TEXT
);

-- 计划的逐步操作，按 step 保存原始提交顺序（确认时严格按此顺序重放）。
CREATE TABLE IF NOT EXISTS plan_operations (
    plan_id     INTEGER NOT NULL REFERENCES transfer_plans(id),
    step        INTEGER NOT NULL,
    source      TEXT NOT NULL,
    destination TEXT NOT NULL,
    volume      TEXT NOT NULL,
    PRIMARY KEY (plan_id, step)
);

-- 计划创建时（只读事务内）固定的涉及孔规范快照：孔是否存在、体积、容量上限
-- 与隔离状态（quarantine_request_id 为 NULL 表示未隔离；已登记但被抽干的孔
-- volume 仍为 '0'，registered=1；volume 为 NULL 表示当时尚未登记）。
CREATE TABLE IF NOT EXISTS plan_well_snapshots (
    plan_id              INTEGER NOT NULL REFERENCES transfer_plans(id),
    well                 TEXT NOT NULL,
    registered           INTEGER NOT NULL DEFAULT 1,
    volume               TEXT,
    max_capacity         TEXT,
    quarantine_request_id INTEGER,
    PRIMARY KEY (plan_id, well)
);

-- 快照孔在创建时的各初始样本量（确认时逐样本对比，识别组成变化）。
CREATE TABLE IF NOT EXISTS plan_snapshot_composition (
    plan_id   INTEGER NOT NULL REFERENCES transfer_plans(id),
    well      TEXT NOT NULL,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    amount    TEXT NOT NULL,
    PRIMARY KEY (plan_id, well, sample_id)
);

-- 快照创建时各孔的生效预留集合（确认时按「预留编号 -> 体积」逐孔对比，
-- 识别预留被建立、取消、消费或改量）。
CREATE TABLE IF NOT EXISTS plan_snapshot_reservations (
    plan_id        INTEGER NOT NULL REFERENCES transfer_plans(id),
    well           TEXT NOT NULL,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    volume         TEXT NOT NULL,
    PRIMARY KEY (plan_id, well, reservation_id)
);

-- 计划确认：独立 confirm_key 幂等（重放不重复扣量）；同键换计划 409。
CREATE TABLE IF NOT EXISTS plan_confirmations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL UNIQUE REFERENCES transfer_plans(id),
    confirm_key TEXT NOT NULL UNIQUE,
    batch_id    INTEGER REFERENCES batches(id),
    confirmed_at TEXT NOT NULL
);
"""


def db_path() -> Path:
    """数据库路径，可用环境变量 LINEAGE_DB 覆盖（测试用）。"""
    return Path(os.environ.get("LINEAGE_DB", str(DEFAULT_DB_PATH)))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：连接为请求级、不跨请求共享，写互斥由
    # BEGIN IMMEDIATE 的数据库级保留锁保证；关闭线程归属检查后，同步
    # 端点在 ASGI 线程池不同工作线程上建连/用连（含测试并发请求）也可工作。
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """老库就地升级。

    - wells 表补 max_capacity 列：既有行 NULL（不限容量）；
    - batches 表补 reservation_id 列：既有批次均为未绑定预留的普通批次（NULL）；
    - batches 表补 plan_confirmation_id 列：既有批次均为直接提交（NULL）。
    现有数据与行为完全保留。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(wells)")}
    if "max_capacity" not in columns:
        conn.execute("ALTER TABLE wells ADD COLUMN max_capacity TEXT")
    batch_columns = {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}
    if "reservation_id" not in batch_columns:
        conn.execute("ALTER TABLE batches ADD COLUMN reservation_id INTEGER")
    if "plan_confirmation_id" not in batch_columns:
        conn.execute("ALTER TABLE batches ADD COLUMN plan_confirmation_id INTEGER")


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即写事务：BEGIN IMMEDIATE 在事务起点就取得保留锁。

    所有变更端点（补液、批次、撤销、容量变更、隔离申请与解除、预留建立/
    兑现/取消、计划建立/确认）都经此进入事务，使「校验读 → 写入 →
    提交」全程互斥串行：后到的事务在 BEGIN IMMEDIATE 处等待，待先到者
    提交后才开始读库，必然读到已提交的新状态，不会以旧快照覆盖新写入
    （例如撤销完成阻塞检查后另一请求的补液）。等不到锁由 busy_timeout
    决定，仍超时则抛 sqlite3.OperationalError 由调用方处理。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务：整段读取同一一致性快照，不获取写锁也不改变任何状态。

    移液计划创建时在此事务内固定涉及孔的规范快照并完成逐步预演；并发
    补液/批次/容量变更/隔离/预留等写事务不会被其阻塞，其已提交结果也
    不会在事务中途混入（WAL 下读不阻塞写、写不阻塞读）。
    """
    isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.rollback()  # 只读事务：无待提交变更，仅释放快照
    finally:
        conn.isolation_level = isolation

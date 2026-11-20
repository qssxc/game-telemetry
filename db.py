import pathlib
import sqlite3
import sys

def connect(db_path: str, fast: bool = False, readonly=False):
    if readonly:
        # 경로를 그대로 URI 에 넣으면 안 된다. `#` 뒤는 조각, `?` 뒤는 질의로
        # 읽혀 경로가 잘리고 mode=ro 도 떨어져 나간다 — 엉뚱한 곳에 빈 파일을
        # 읽기·쓰기로 새로 만든다. as_uri 가 그런 글자를 %XX 로 바꿔 준다.
        uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    if fast:
        # 적재는 언제든 처음부터 다시 돌릴 수 있다. 중간에 전원이 꺼져도
        # 원본 NDJSON 이 남아 있으므로 내구성을 거래해도 잃는 것이 없다.
        con.execute("PRAGMA synchronous = OFF")
        con.execute("PRAGMA cache_size = -64000")   # 64MB
    return con

# 칼럼의 선언 타입은 SQLite 에서 저장 방식을 거의 결정하지 않지만,
# Metabase 가 칼럼 종류(날짜/불리언/숫자)를 판별할 때 이 선언을 읽는다.
# 그래서 TEXT 로 뭉개지 않고 의미에 맞게 선언한다.
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT    NOT NULL,
    player_id        TEXT    NOT NULL,
    mode             TEXT,
    is_host          BOOLEAN,
    clock_offset_ms  INTEGER,
    build_version    TEXT,
    platform         TEXT,
    started_at       TIMESTAMP,
    duration_ms      INTEGER,   -- session_ended 가 없으면 NULL (비정상 종료)
    dropped_count    INTEGER,
    event_count      INTEGER,
    source_file      TEXT,
    PRIMARY KEY (session_id, player_id)
);

CREATE TABLE IF NOT EXISTS events (
    session_id     TEXT    NOT NULL,
    player_id      TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    t_ms           INTEGER,
    t_raw_ms       INTEGER,
    wall_clock     TIMESTAMP,
    name           TEXT,
    tag            TEXT,
    tier           TEXT,
    build_version  TEXT,
    -- 병합 판정. 적재 시점엔 모두 1 이고 merge.py 가 중복 행을 0 으로 내린다.
    -- 원본은 지우지 않는다. 클라이언트별 관측 시각 자체가 데이터이기 때문이다.
    canonical      INTEGER NOT NULL DEFAULT 1,
    dup_group      TEXT,
    PRIMARY KEY (session_id, player_id, seq)
);

-- 파라미터를 세로로 보관한다. 새 파라미터가 생겨도 스키마 변경이 없다.
-- 숫자와 문자를 각각의 칼럼에 넣어, 조회 시 형변환 없이 집계할 수 있게 한다.
CREATE TABLE IF NOT EXISTS event_params (
    session_id  TEXT    NOT NULL,
    player_id   TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    key         TEXT    NOT NULL,
    num_value   REAL,
    text_value  TEXT,
    bool_value  BOOLEAN
);

-- 병합 단계가 계산한 값. 원본은 건드리지 않고 여기에 덧댄다.
-- merge.py 가 채우며, 비어 있어도 뷰와 지표는 정상 동작한다.
CREATE TABLE IF NOT EXISTS merge_overrides (
    session_id  TEXT NOT NULL,
    player_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    key         TEXT NOT NULL,
    text_value  TEXT,
    num_value   REAL,
    source      TEXT,          -- 어떤 규칙이 이 값을 넣었는지
    PRIMARY KEY (session_id, player_id, seq, key)
);

CREATE TABLE IF NOT EXISTS load_log (
    source_file  TEXT,
    loaded_at    TIMESTAMP,
    line_count   INTEGER,
    error_count  INTEGER
);

CREATE TABLE IF NOT EXISTS metric_results (
    metric_name      TEXT    NOT NULL,
    scope            TEXT    NOT NULL,   -- 'session' | 'party' | 'global'
    session_id       TEXT,               -- scope='global' 이면 NULL
    -- scope='party' 면 NULL. 파티 값은 특정 플레이어의 것이 아니다.
    -- scope='global' 이면 session_id 와 함께 NULL.
    player_id        TEXT,
    dimension_key    TEXT    NOT NULL,   -- 쪼갠 축의 이름. 없으면 ''
    dimension_value  TEXT    NOT NULL,   -- 그 축의 값. 없으면 ''
    value            REAL,
    computed_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_metrics_lookup
    ON metric_results (scope, session_id, player_id, metric_name);
"""

# SQLite 는 인덱스가 없으면 매번 전체 스캔을 한다. 조회 패턴은 "이벤트를
# 이름으로 걸러낸 뒤 각 이벤트의 파라미터를 집는다" 이다.
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_params_lookup
    ON event_params (session_id, player_id, seq, key);
CREATE INDEX IF NOT EXISTS ix_params_key
    ON event_params (key, num_value);
CREATE INDEX IF NOT EXISTS ix_events_name
    ON events (name);
"""

# 우리가 만드는 테이블. --reset 이 지우는 범위이기도 하다.
# 의존 순서의 역순으로 적는다.
OUR_TABLES = ("load_log", "metric_results", "merge_overrides",
              "event_params", "events", "sessions")


def run_atomically(con: sqlite3.Connection, sql: str, label: str) -> bool:
    """
    여러 문장을 한 트랜잭션으로 실행한다.

    묶는 이유 — SQLite 는 DDL 도 트랜잭션에 들어간다. DROP VIEW 와
    CREATE VIEW 사이의 빈 구간을 다른 연결(대표적으로 Metabase)이 보면
    `no such table: v_coin_ledger` 로 카드가 깨진다. 한 트랜잭션에 넣으면
    읽는 쪽은 옛 뷰 아니면 새 뷰만 보고 중간 상태를 보지 못한다.

    executescript 은 호출 직전에 열려 있던 트랜잭션을 커밋한 뒤 본문을
    그대로 SQLite 에 넘긴다. 그래서 BEGIN/COMMIT 을 본문에 써야 묶인다.
    """
    try:
        con.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
        return True
    except sqlite3.Error as e:
        try:
            con.executescript("ROLLBACK;")
        except sqlite3.Error:
            pass                       # 이미 롤백된 경우
        print(f"  [오류] {label} 실패: {e}", file=sys.stderr)
        return False

"""
NDJSON 세션 로그를 SQLite 에 적재한다.

SQLite 를 쓰는 이유
-------------------
Metabase 의 공식 내장 드라이버다. DuckDB 는 커뮤니티 드라이버(.jar 수동 설치)
뿐이고 Metabase 버전이 올라가면 깨진다. 또 DuckDB 는 쓰기 모드에서 한
프로세스만 파일을 열 수 있어, Metabase 가 파일을 붙들면 적재가 막힌다.

설계 원칙
---------
파라미터를 세로(long) 형태로 저장한다. 이벤트마다 파라미터 구성이 다르고,
게임에 계측을 추가하면 새 파라미터가 계속 생기기 때문이다. 가로로 펼친
테이블이면 이벤트 종류를 추가할 때마다 스키마를 바꿔야 하고, 그러면
"수집을 늘려도 도구는 그대로" 라는 전제가 깨진다.

조회 편의는 뷰로 해결한다(views.sql).

사용법
------
    python loader.py <로그 폴더> [--db playtest.sqlite] [--reset]

적재는 전체가 하나의 트랜잭션이다. Metabase 가 같은 파일을 열어둔 채로
돌려도 중간 상태가 보이지 않고, 실패하면 직전 데이터가 온전히 남는다.
--reset 도 파일을 지우지 않고 내용만 비운다(begin_load 주석 참고).

이벤트마다 만드는 뷰는 viewgen.py 가 맡는다. 여기서 부르지 않는 이유는
순서 때문이다 — 중복 복제된 행을 걸러낸 뷰를 만들려면 merge.py 가 먼저
판정을 끝내야 한다.

    loader.py  →  merge.py  →  viewgen.py  →  metrics.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from typing import Any, Iterator

# 이벤트 봉투(envelope)에 해당하는 키. 이 외의 키는 모두 파라미터로 본다.
ENVELOPE_KEYS = {
    "session_id", "player_id", "seq", "t_ms", "t_raw_ms",
    "wall_clock", "name", "tag", "tier", "build_version", "params",
}

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
    source      TEXT,
    PRIMARY KEY (session_id, player_id, seq, key)
);

CREATE TABLE IF NOT EXISTS load_log (
    source_file  TEXT,
    loaded_at    TIMESTAMP,
    line_count   INTEGER,
    error_count  INTEGER
);
"""

# SQLite 는 DuckDB 와 달리 인덱스가 없으면 매번 전체 스캔을 한다.
# 조회 패턴은 "이벤트를 이름으로 걸러낸 뒤 각 이벤트의 파라미터를 집는다" 이므로
# (session_id, player_id, seq, key) 복합 인덱스가 조인과 키 조건을 동시에 받는다.
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
    `no such table: v_purchases` 로 카드가 깨진다. 한 트랜잭션에 넣으면
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


def begin_load(con: sqlite3.Connection, reset: bool) -> None:
    """
    트랜잭션을 열고 스키마를 준비한다. COMMIT 하지 않고 돌아간다.

    커밋을 미루는 이유 — 이어지는 적재 INSERT 가 같은 트랜잭션에 들어간다.
    그래서 적재가 끝날 때까지 읽는 쪽(Metabase)은 옛 데이터를 온전히 보고,
    테이블이 비어 있는 구간도 절반만 들어찬 구간도 보지 못한다.
    30만 이벤트 적재가 7초 걸리는데, 그 7초를 노출하지 않는다.

    --reset 이 파일을 unlink 하지 않는 이유 — 파일을 열어둔 연결은 삭제된
    inode 를 계속 붙잡는다. 새 파일에 적재해도 Metabase 는 **오류 없이
    옛 데이터를 계속 보여준다.** 쿼리를 의심하게 되는데 원인은 파일이라
    찾기 어렵다. 그래서 파일은 유지하고 내용만 바꾼다.

    DELETE 가 아니라 DROP + 재생성인 이유: 스키마가 바뀌었을 때도
    --reset 이 제 역할을 해야 한다.

    뷰는 건드리지 않는다. 뷰를 여기서 drop 하면 재생성(apply_views)까지
    없는 상태가 되어 `no such table: v_purchases` 가 난다. SQLite 는 뷰가
    참조하는 테이블을 그냥 drop 하게 해주고, 뷰 교체는 views.sql 이
    자기 트랜잭션 안에서 통째로 한다.
    """
    sql = ["BEGIN IMMEDIATE;"]
    if reset:
        sql += [f'DROP TABLE IF EXISTS "{t}";' for t in OUR_TABLES]
    sql.append(SCHEMA)
    con.executescript("\n".join(sql))      # COMMIT 없음 — 트랜잭션이 열려 있다


def connect(db_path: str, fast: bool = False) -> sqlite3.Connection:
    """DB 를 연다. fast=True 면 적재용으로 내구성을 조금 낮춘다."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    if fast:
        # 적재는 언제든 처음부터 다시 돌릴 수 있다. 중간에 전원이 꺼져도
        # 원본 NDJSON 이 남아 있으므로 내구성을 거래해도 잃는 것이 없다.
        con.execute("PRAGMA synchronous = OFF")
        con.execute("PRAGMA cache_size = -64000")   # 64MB
    return con


def read_lines(path: pathlib.Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """NDJSON 을 한 줄씩 읽는다.

    깨진 줄은 건너뛰고 None 을 돌려준다. 게임이 비정상 종료하면 마지막 줄이
    잘린 채 남을 수 있는데, 그 한 줄 때문에 세션 전체를 버리면 안 된다.
    """
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield lineno, json.loads(raw)
            except json.JSONDecodeError:
                yield lineno, None


def split_param(value: Any) -> tuple[float | None, str | None, bool | None]:
    """파라미터 값을 (숫자, 문자, 불리언) 세 칼럼으로 나눈다."""
    # bool 검사가 먼저다. 파이썬에서 bool 은 int 의 하위 타입이다.
    if isinstance(value, bool):
        return None, None, value
    if isinstance(value, (int, float)):
        return float(value), None, None
    if value is None:
        return None, None, None
    return None, str(value), None


def load_file(con: sqlite3.Connection, path: pathlib.Path) -> dict[str, Any]:
    """파일 하나를 적재하고 요약을 돌려준다."""
    event_rows: list[tuple] = []
    param_rows: list[tuple] = []

    session_id = player_id = None
    session_meta: dict[str, Any] = {}
    duration_ms = dropped_count = None
    error_count = 0
    line_count = 0

    for lineno, ev in read_lines(path):
        line_count += 1
        if ev is None:
            error_count += 1
            print(f"  [경고] {path.name}:{lineno} JSON 파싱 실패 — 건너뜀")
            continue

        sid = ev.get("session_id")
        pid = ev.get("player_id")
        seq = ev.get("seq")
        if sid is None or pid is None or seq is None:
            error_count += 1
            print(f"  [경고] {path.name}:{lineno} 식별자 누락 — 건너뜀")
            continue

        session_id, player_id = sid, pid
        params = ev.get("params") or {}

        event_rows.append((
            sid, pid, seq,
            ev.get("t_ms"), ev.get("t_raw_ms"), ev.get("wall_clock"),
            ev.get("name"), ev.get("tag"), ev.get("tier"), ev.get("build_version"),
        ))

        for key, value in params.items():
            num, text, flag = split_param(value)
            param_rows.append((sid, pid, seq, key, num, text, flag))

        # 세션 메타는 전용 이벤트에서 뽑는다.
        if ev.get("name") == "session_started":
            session_meta = {
                "mode": params.get("mode"),
                "is_host": params.get("is_host"),
                "clock_offset_ms": params.get("clock_offset_ms"),
                "platform": params.get("platform"),
                "started_at": params.get("started_at"),
                "build_version": ev.get("build_version"),
            }
        elif ev.get("name") == "session_ended":
            duration_ms = params.get("duration_ms")
            dropped_count = params.get("dropped_count")

    if session_id is None:
        print(f"  [건너뜀] {path.name} — 유효한 이벤트가 없다")
        return {"events": 0, "errors": error_count, "lines": line_count}

    # 커밋하지 않는다. 호출자(main)가 열어둔 트랜잭션에 그대로 쌓이고,
    # 적재가 전부 끝난 뒤 한 번 커밋된다. 예전에는 `with con:` 으로 파일마다
    # 커밋했는데, 그러면 읽는 쪽(Metabase)이 절반만 들어찬 상태를 본다.
    # 파일 단위 원자성보다 적재 전체 단위가 강한 보장이다 — 중간에 실패하면
    # 일부 파일만 들어간 DB 가 남는 대신 직전 데이터가 온전히 남는다.
    con.executemany(
        "INSERT OR REPLACE INTO events"
        " (session_id, player_id, seq, t_ms, t_raw_ms, wall_clock,"
        "  name, tag, tier, build_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", event_rows
    )
    # 재적재 시 중복을 막기 위해 해당 세션의 파라미터를 먼저 비운다.
    # event_params 에는 유일키가 없어 INSERT OR REPLACE 가 듣지 않는다.
    con.execute(
        "DELETE FROM event_params WHERE session_id = ? AND player_id = ?",
        (session_id, player_id),
    )
    if param_rows:
        con.executemany(
            "INSERT INTO event_params VALUES (?,?,?,?,?,?,?)", param_rows
        )

    con.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id, player_id,
            session_meta.get("mode"), session_meta.get("is_host"),
            session_meta.get("clock_offset_ms"), session_meta.get("build_version"),
            session_meta.get("platform"), session_meta.get("started_at"),
            duration_ms, dropped_count,
            len(event_rows), path.name,
        ),
    )

    con.execute(
        "INSERT INTO load_log VALUES (?, datetime('now'), ?, ?)",
        (path.name, line_count, error_count),
    )

    status = "" if duration_ms is not None else "  ※ session_ended 없음 (비정상 종료)"
    print(f"  {path.name}: {session_id} / {player_id} — "
          f"이벤트 {len(event_rows)}건, 파라미터 {len(param_rows)}건{status}")

    return {"events": len(event_rows), "errors": error_count, "lines": line_count}


def apply_views(con: sqlite3.Connection, views_path: pathlib.Path) -> bool:
    """
    views.sql 을 적용한다.

    sqlite3 명령줄 도구를 거치지 않는 이유: PowerShell 은 `<` 리다이렉션을
    지원하지 않고(예약어로 잡아두고 구현하지 않았다), `Get-Content | sqlite3`
    는 Windows PowerShell 5.1 의 기본 출력 인코딩이 ASCII 라 한글 주석이
    깨진다. 파이썬이 파일을 UTF-8 로 직접 읽어 넘기면 둘 다 피한다.
    sqlite3.exe 설치 여부와도 무관해진다.
    """
    if not views_path.exists():
        print(f"  [건너뜀] {views_path.name} 이 없다 — 뷰 없이 진행한다")
        return False
    return run_atomically(con, views_path.read_text(encoding="utf-8"), "뷰 적용")


def main() -> int:
    ap = argparse.ArgumentParser(description="NDJSON 세션 로그를 SQLite 에 적재한다")
    ap.add_argument("log_dir", nargs="?",
                    help="*.ndjson 이 들어있는 폴더 (--views-only 면 생략 가능)")
    ap.add_argument("--db", default="playtest.sqlite", help="출력 DB 경로")
    ap.add_argument("--reset", action="store_true",
                    help="기존 내용을 비우고 새로 적재한다 (DB 파일은 유지한다)")
    ap.add_argument("--views", default=None,
                    help="적용할 뷰 스크립트 (기본: 이 스크립트 옆의 views.sql)")
    ap.add_argument("--no-views", action="store_true", help="뷰를 적용하지 않는다")
    ap.add_argument("--views-only", action="store_true",
                    help="적재 없이 뷰만 다시 적용한다")
    args = ap.parse_args()

    db_path = pathlib.Path(args.db)
    views_path = pathlib.Path(args.views) if args.views \
        else pathlib.Path(__file__).parent / "views.sql"

    # ----------------------------------------------------- 뷰만 다시 적용
    if args.views_only:
        if not db_path.exists():
            print(f"[오류] DB 가 없다: {db_path}", file=sys.stderr)
            return 1
        con = connect(str(db_path))
        print(f"뷰 적용 — {views_path.name} → {db_path}")
        ok = apply_views(con, views_path)
        con.close()
        print("완료" if ok else "실패")
        return 0 if ok else 1

    # ----------------------------------------------------------- 적재
    if not args.log_dir:
        print("[오류] 로그 폴더를 지정하라 (또는 --views-only)", file=sys.stderr)
        return 1

    log_dir = pathlib.Path(args.log_dir)
    if not log_dir.is_dir():
        print(f"[오류] 폴더가 아니다: {log_dir}", file=sys.stderr)
        return 1

    files = sorted(log_dir.glob("*.ndjson"))
    if not files:
        print(f"[오류] {log_dir} 에 .ndjson 파일이 없다", file=sys.stderr)
        return 1

    con = connect(str(db_path), fast=True)

    if args.reset:
        print("초기화 — 파일은 유지하고 내용만 비운다")

    totals = {"events": 0, "errors": 0, "lines": 0}
    try:
        begin_load(con, reset=args.reset)
        print(f"적재 시작 — {len(files)}개 파일 → {db_path}")
        for path in files:
            result = load_file(con, path)
            for k in totals:
                totals[k] += result[k]
        con.commit()            # 여기서 비로소 새 데이터가 읽는 쪽에 보인다
    except (sqlite3.Error, OSError) as e:
        con.rollback()          # 옛 데이터가 그대로 남는다
        print(f"[오류] 적재 실패 — 기존 데이터를 유지한다: {e}", file=sys.stderr)
        con.close()
        return 1

    # 인덱스는 적재 후에 만든다. 적재 중에 유지하는 것보다 빠르다.
    print("인덱스 생성 중...")
    con.executescript(INDEXES)
    con.execute("ANALYZE")      # 질의 계획기에 통계를 준다
    con.commit()

    # 뷰는 DROP/CREATE 라 매번 다시 만들어도 비용이 없고, --reset 뒤에는
    # 반드시 다시 만들어야 한다. 그래서 여기서 자동으로 적용한다.
    if not args.no_views:
        print(f"뷰 적용 중... ({views_path.name})")
        apply_views(con, views_path)

    con.close()

    print(f"\n완료 — 이벤트 {totals['events']}건, "
          f"읽은 줄 {totals['lines']}, 건너뛴 줄 {totals['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
현실적인 플레이테스트 규모에서 SQLite 가 버티는지 측정한다.

DuckDB 를 버리고 SQLite 로 통일할 때 유일하게 걱정되는 게 성능이다.
그런데 "느릴 것 같다" 는 추측으로 설계를 정할 수는 없으므로, 실제 질의
패턴을 그대로 돌려 시간을 재 본다.

    python bench.py --sessions 500 --events 1000
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sqlite3
import subprocess
import sys
import time

import loader

# 실제 전투 로그의 구성을 흉내낸다. 파라미터 수와 종류가 질의 비용을 좌우하므로
# 임의의 숫자가 아니라 D 단계가 실제로 남기는 모양을 쓴다.
EVENT_SHAPES = [
    ("turn_started",   "combat",  [("actor", "player"), ("turn_no", 1),
                                   ("player_hp", 120.0), ("monster_hp", 300.0)]),
    ("player_action",  "combat",  [("action", "attack"), ("skill_id", -1),
                                   ("slot_index", -1), ("turn_no", 1),
                                   ("decision_ms", 2400)]),
    ("damage_dealt",   "combat",  [("source", "attack"), ("skill_id", -1),
                                   ("damage", 85.0), ("is_critical", False),
                                   ("target", "monster"), ("player_hp_after", 120.0),
                                   ("monster_hp_after", 215.0), ("turn_no", 1)]),
    ("damage_taken",   "combat",  [("source", "attack"), ("skill_id", -1),
                                   ("damage", 40.0), ("is_critical", False),
                                   ("was_defending", False), ("player_hp_after", 80.0),
                                   ("monster_hp_after", 215.0), ("turn_no", 1)]),
    ("item_purchased", "economy", [("item_id", 7), ("amount", 1), ("price", 150)]),
    ("resource_changed", None,    [("resource_id", "gold"), ("delta", -150),
                                   ("reason", "item_purchase"), ("balance", 99000)]),
    ("battle_ended",   "combat",  [("monster_id", 3), ("is_victory", True),
                                   ("turn_count", 8), ("elapsed_ms", 42000),
                                   ("player_hp_remaining", 55.0),
                                   ("monster_hp_remaining", -12.0)]),
]


def generate(db_path: str, n_sessions: int, n_events: int) -> tuple[int, int]:
    for suffix in ("", "-wal", "-shm"):
        stale = pathlib.Path(db_path + suffix)
        if stale.exists():
            stale.unlink()

    con = loader.connect(db_path, fast=True)
    con.executescript(loader.SCHEMA)

    rng = random.Random(42)
    total_events = total_params = 0

    for s in range(n_sessions):
        session_id = f"room{1000 + s}_20261020"
        player_id = f"p{s % 4 + 1}"
        event_rows, param_rows = [], []

        for seq in range(1, n_events + 1):
            name, tag, params = EVENT_SHAPES[rng.randrange(len(EVENT_SHAPES))]
            t_ms = seq * 300
            event_rows.append((session_id, player_id, seq, t_ms, t_ms,
                               "2026-10-20T01:00:00.000Z", name, tag,
                               "critical", "0.1.0"))
            for key, value in params:
                if isinstance(value, bool):
                    param_rows.append((session_id, player_id, seq, key,
                                       None, None, value))
                elif isinstance(value, (int, float)):
                    # 값에 변화를 줘야 집계가 의미 있는 비용을 낸다
                    param_rows.append((session_id, player_id, seq, key,
                                       float(value) + rng.randrange(50), None, None))
                else:
                    param_rows.append((session_id, player_id, seq, key,
                                       None, value, None))

        with con:
            con.executemany(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", event_rows)
            con.executemany(
                "INSERT INTO event_params VALUES (?,?,?,?,?,?,?)", param_rows)
            con.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, player_id, "coop", s % 2 == 0, 0, "0.1.0",
                 "WindowsPlayer", "2026-10-20T01:00:00.000Z",
                 n_events * 300, 0, len(event_rows), "bench"))

        total_events += len(event_rows)
        total_params += len(param_rows)

    t0 = time.perf_counter()
    con.executescript(loader.INDEXES)
    con.execute("ANALYZE")
    con.commit()
    index_sec = time.perf_counter() - t0
    con.close()

    print(f"  인덱스 생성: {index_sec:.2f}초")
    return total_events, total_params


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=300)
    ap.add_argument("--events", type=int, default=1000)
    ap.add_argument("--db", default="bench.sqlite")
    args = ap.parse_args()

    here = pathlib.Path(__file__).parent

    print(f"합성 데이터 생성 — 세션 {args.sessions}개 × 이벤트 {args.events}건")
    t0 = time.perf_counter()
    n_events, n_params = generate(args.db, args.sessions, args.events)
    gen_sec = time.perf_counter() - t0
    size_mb = pathlib.Path(args.db).stat().st_size / 1024 / 1024
    print(f"  이벤트 {n_events:,}건, 파라미터 {n_params:,}행, "
          f"{size_mb:.1f}MB, 생성 {gen_sec:.1f}초\n")

    print("뷰 적용")
    con = sqlite3.connect(args.db)
    t0 = time.perf_counter()
    con.executescript((here / "views.sql").read_text(encoding="utf-8"))
    con.commit()
    print(f"  {time.perf_counter() - t0:.2f}초\n")

    print("지표 전체 계산 (정의 파일 30개 지표)")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(here / "metrics.py"), str(here / "hybridrpg.yaml"),
         "--db", args.db],
        capture_output=True, text=True,
    )
    metric_sec = time.perf_counter() - t0
    if proc.returncode != 0:
        print(proc.stdout[-2000:], proc.stderr[-2000:])
        return 1
    print(f"  {metric_sec:.1f}초\n")

    print("개별 질의 (Metabase 가 대시보드를 그릴 때 내는 종류)")
    queries = {
        "전투 승률":            "SELECT AVG(CASE WHEN is_victory THEN 1.0 ELSE 0 END) FROM v_battles",
        "행동별 결정시간 평균": "SELECT action, AVG(decision_ms) FROM v_actions GROUP BY action",
        "방어 효과 비교":       "SELECT was_defending, AVG(damage) FROM v_damage WHERE direction='taken' GROUP BY was_defending",
        "아이템별 구매 수":     "SELECT item_id, COUNT(*) FROM v_purchases GROUP BY item_id ORDER BY 2 DESC LIMIT 20",
        "사유별 골드 기여":     "SELECT reason, SUM(delta) FROM v_resource_ledger GROUP BY reason",
        "세션별 지표 조회":     "SELECT session_id, metric_name, value FROM v_metrics_session LIMIT 1000",
    }
    for label, sql in queries.items():
        t0 = time.perf_counter()
        rows = con.execute(sql).fetchall()
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"  {label:<22} {elapsed:>8.1f}ms  ({len(rows)}행)")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

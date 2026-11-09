"""
협동 세션의 여러 파일을 하나로 합친다.

참가자마다 파일이 하나씩 생긴다. 그냥 다 적재하면 같은 사건이 여러 번
들어간다. 특히 파티가 공유하는 자원은 **참가자 수만큼 부풀려진다**
(실측: 2인 세션에서 골드 지출이 정확히 2배).

이 단계가 하는 일은 둘이다.

  1. 사유 보정 — resource_reason_corrected 가 가리키는 행에 실제 사유를 붙인다
  2. 중복 판정 — 같은 사건의 여러 복사본 중 하나만 canonical 로 남긴다

**원본은 지우지 않는다.** events.canonical 을 0 으로 내릴 뿐이다. 클라이언트별
관측 시각 자체가 데이터이기 때문이다(체감 지연 측정에 쓴다). 파티 단위 집계는
canonical = 1 만 보면 되고, 클라이언트별 분석은 전부 보면 된다.

사용법
------
    python loader.py <로그폴더> --db playtest.sqlite --reset
    python merge.py --db playtest.sqlite
    python metrics.py hybridrpg.yaml --db playtest.sqlite
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from typing import Any

from db import connect,SCHEMA

# ---------------------------------------------------------------- 판정 규칙
#
# 게임마다 다른 지식이다. 원래는 게임 정의 파일에 있어야 하고, 지금은 여기 둔다.

# 파티가 공유하는 상태. 한 명이 일으킨 변화를 전원이 관측한다.
#
# 키에 balance 를 쓰는 이유: 서버가 보낸 공유 잔액이라 모든 클라이언트에서
# 같은 값이고, 거래가 이어져도 잔액은 매번 달라 사실상 일련번호 역할을 한다.
SHARED_STATE_KEYS: dict[str, tuple[str, ...]] = {
    "resource_changed": ("resource_id", "delta", "balance"),
    "gold_observed": ("balance",),
}

# 서버가 전원에게 뿌리는 알림. 각자 기록하면 중복이 생긴다.
#
# 키에는 서버가 내려준 값만 넣는다. elapsed_ms 처럼 클라이언트가 로컬에서
# 계산하는 값은 파일마다 달라서, 키에 넣으면 같은 사건이 갈라진다.
BROADCAST_KEYS: dict[str, tuple[str, ...]] = {
    "monster_killed": ("raw_id", "monster_id"),
    "player_downed": ("user_id",),
    "player_recovered": ("user_id",),
    "player_rescued": ("target_user_id",),
    "item_acquired": ("user_id", "item_id", "object_id"),
    "combat_entered": ("user_id", "monster_id"),
    "combat_joined": ("user_id",),
}

# 사유를 뒤늦게 알려주는 이벤트. 어댑터가 이중 계상을 막으면서 남긴다.
REASON_CORRECTION_EVENT = "resource_reason_corrected"


def ensure_columns(con: sqlite3.Connection) -> None:
    """예전 DB 에도 판정 칼럼을 붙인다."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    if "canonical" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN canonical INTEGER NOT NULL DEFAULT 1")
    if "dup_group" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN dup_group TEXT")


def load_events(con: sqlite3.Connection, session_id: str) -> dict[str, list[dict]]:
    """세션의 모든 플레이어 이벤트를 파라미터까지 붙여 읽는다."""
    params: dict[tuple, dict[str, Any]] = defaultdict(dict)
    for pid, seq, key, num, text, flag in con.execute(
        "SELECT player_id, seq, key, num_value, text_value, bool_value"
        " FROM event_params WHERE session_id = ?",
        (session_id,),
    ):
        params[(pid, seq)][key] = flag if flag is not None else (
            num if num is not None else text)

    out: dict[str, list[dict]] = defaultdict(list)
    for pid, seq, t_ms, wall, name in con.execute(
        "SELECT player_id, seq, t_ms, wall_clock, name FROM events"
        " WHERE session_id = ? ORDER BY player_id, seq",
        (session_id,),
    ):
        out[pid].append({"player_id": pid, "seq": seq, "t_ms": t_ms,
                         "wall": wall, "name": name,
                         "params": params.get((pid, seq), {})})
    return out


# ------------------------------------------------------------ 1. 사유 보정

def apply_reason_corrections(con, session_id: str,
                             ev_by_player: dict[str, list[dict]]) -> int:
    """
    resource_reason_corrected 가 가리키는 resource_changed 행에 실제 사유를 붙인다.

    인벤토리 동기화가 보상 응답보다 먼저 도착하면, 어댑터는 그 변동을
    "unknown" 으로 기록한 뒤 이중 계상을 막기 위해 보정 이벤트만 남긴다.
    그 사유를 여기서 되돌려 붙여야 중복 판정에서 어느 행을 남길지 정할 수 있다.
    """
    written = 0
    for pid, events in ev_by_player.items():
        corrections = [e for e in events if e["name"] == REASON_CORRECTION_EVENT]
        if not corrections:
            continue

        for corr in corrections:
            rid = corr["params"].get("resource_id")
            delta = corr["params"].get("delta")
            to_reason = corr["params"].get("to_reason")
            if to_reason is None:
                continue

            # 보정 이벤트 **앞쪽**에서 조건이 맞는 가장 가까운 행을 찾는다.
            target = None
            for e in events:
                if e["seq"] >= corr["seq"]:
                    break
                if (e["name"] == "resource_changed"
                        and e["params"].get("resource_id") == rid
                        and e["params"].get("delta") == delta
                        and e["params"].get("reason") == "unknown"):
                    target = e
            if target is None:
                print(f"    [경고] {pid} seq {corr['seq']} 의 보정 대상을 찾지 못했다")
                continue

            con.execute(
                "INSERT OR REPLACE INTO merge_overrides VALUES (?,?,?,?,?,?,?)",
                (session_id, pid, target["seq"], "reason", to_reason, None,
                 f"{REASON_CORRECTION_EVENT}@{corr['seq']}"),
            )
            target["params"]["reason"] = to_reason   # 뒤 단계가 쓰도록 메모리에도 반영
            written += 1

    return written


# ------------------------------------------------------------ 2. 중복 판정

def dedup_key(event: dict) -> tuple | None:
    """이 이벤트가 중복 판정 대상이면 키를, 아니면 None 을 돌려준다."""
    for table in (SHARED_STATE_KEYS, BROADCAST_KEYS):
        fields = table.get(event["name"])
        if fields is None:
            continue
        values = tuple(event["params"].get(f) for f in fields)
        if all(v is None for v in values):
            return None          # 키로 쓸 값이 하나도 없다 — 판정하지 않는다
        return (event["name"],) + values
    return None


def pick_canonical(group: list[dict], host: str | None) -> dict:
    """
    같은 사건의 복사본 중 남길 하나를 고른다.

    1순위: 사유를 아는 행. 행위자의 클라이언트만 왜 바뀌었는지 안다.
    2순위: 호스트. 게임 상태의 기준이 되는 쪽이다.
    3순위: player_id 순. 돌릴 때마다 결과가 바뀌지 않게 하기 위한 고정 규칙.
    """
    def rank(e: dict) -> tuple:
        reason = e["params"].get("reason")
        knows_reason = reason is not None and reason != "unknown"
        return (0 if knows_reason else 1,
                0 if e["player_id"] == host else 1,
                e["player_id"])

    return min(group, key=rank)


def dedup_session(con, session_id: str, ev_by_player: dict[str, list[dict]],
                  host: str | None) -> dict[str, int]:
    # 키별로 각 플레이어의 관측을 순서대로 모은다.
    #
    # 같은 키가 한 세션에 여러 번 나올 수 있다(같은 금액 거래가 반복되는 경우).
    # 시간 창으로 맞추면 시계 오차에 끌려다니므로, 파일 안에서의 등장 순서로
    # 짝을 짓는다. k 번째 등장끼리 같은 사건으로 본다.
    buckets: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for pid, events in ev_by_player.items():
        for e in events:
            k = dedup_key(e)
            if k is not None:
                buckets[k][pid].append(e)

    stats = {"groups": 0, "duplicates": 0, "mismatched": 0}
    marks: list[tuple] = []

    for key, by_player in buckets.items():
        counts = {p: len(v) for p, v in by_player.items()}
        depth = max(counts.values())

        if len(by_player) > 1 and len(set(counts.values())) > 1:
            # 한쪽이 덜 봤다. 동기화를 놓쳤거나 늦게 들어온 참가자다.
            stats["mismatched"] += 1
            print(f"    [주의] {key[0]} {key[1:]} 관측 수가 다르다: {counts}")

        for i in range(depth):
            group = [v[i] for v in by_player.values() if i < len(v)]
            if len(group) < 2:
                continue
            stats["groups"] += 1
            keep = pick_canonical(group, host)
            gid = f"{key[0]}|{'|'.join(str(x) for x in key[1:])}|{i}"
            for e in group:
                marks.append((1 if e is keep else 0, gid,
                              session_id, e["player_id"], e["seq"]))
                if e is not keep:
                    stats["duplicates"] += 1

    if marks:
        con.executemany(
            "UPDATE events SET canonical = ?, dup_group = ?"
            " WHERE session_id = ? AND player_id = ? AND seq = ?",
            marks,
        )
    return stats


# ------------------------------------------------------------------- 실행

def merge(con: sqlite3.Connection) -> int:
    con.executescript(SCHEMA)
    ensure_columns(con)

    # 다시 돌려도 같은 결과가 나오도록 판정을 초기화한다.
    con.execute("UPDATE events SET canonical = 1, dup_group = NULL")
    con.execute("DELETE FROM merge_overrides")

    sessions: dict[str, list[str]] = defaultdict(list)
    hosts: dict[str, str] = {}
    for sid, pid, is_host in con.execute(
        "SELECT session_id, player_id, is_host FROM sessions ORDER BY session_id, player_id"
    ):
        sessions[sid].append(pid)
        if is_host:
            hosts[sid] = pid

    multi = {s: p for s, p in sessions.items() if len(p) > 1}
    print(f"세션 {len(sessions)}개 (2인 이상 {len(multi)}개)")

    total = {"corrections": 0, "groups": 0, "duplicates": 0, "mismatched": 0}

    for sid, players in sessions.items():
        ev = load_events(con, sid)

        corrections = apply_reason_corrections(con, sid, ev)
        total["corrections"] += corrections

        if len(players) < 2:
            if corrections:
                print(f"  {sid}: 사유 보정 {corrections}건 (1인 세션 — 중복 판정 없음)")
            continue

        stats = dedup_session(con, sid, ev, hosts.get(sid))
        for k in ("groups", "duplicates", "mismatched"):
            total[k] += stats[k]

        print(f"  {sid}: 참가자 {len(players)}명 — "
              f"사유 보정 {corrections}건, 중복 그룹 {stats['groups']}개, "
              f"제외 {stats['duplicates']}행")

    con.commit()

    print(f"\n완료 — 사유 보정 {total['corrections']}건, "
          f"중복 그룹 {total['groups']}개, canonical 에서 제외 {total['duplicates']}행")
    if total["mismatched"]:
        print(f"       관측 수가 어긋난 사건 {total['mismatched']}건 (위 [주의] 참조)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="협동 세션의 중복을 판정한다")
    ap.add_argument("--db", default="playtest.sqlite")
    ap.add_argument("--show", action="store_true", help="판정 결과를 자원별로 보여준다")
    args = ap.parse_args()

    con = connect(args.db)
    con.execute("PRAGMA journal_mode = WAL")
    rc = merge(con)

    if args.show:
        print("\n자원 합계 — 판정 전후")
        print("-" * 58)
        rows = con.execute("""
            SELECT COALESCE(r.text_value, '?') AS resource_id,
                   SUM(d.num_value)                                  AS raw_total,
                   SUM(CASE WHEN e.canonical = 1 THEN d.num_value END) AS merged_total
            FROM events e
            JOIN event_params d ON d.session_id = e.session_id
                               AND d.player_id = e.player_id
                               AND d.seq = e.seq AND d.key = 'delta'
            LEFT JOIN event_params r ON r.session_id = e.session_id
                               AND r.player_id = e.player_id
                               AND r.seq = e.seq AND r.key = 'resource_id'
            WHERE e.name = 'resource_changed'
            GROUP BY 1 ORDER BY 1
        """).fetchall()
        for rid, raw, merged in rows:
            factor = (raw / merged) if merged else float("nan")
            print(f"  {rid:<10} 단순 합계 {raw:+12,.0f}   병합 후 {merged:+12,.0f}   ({factor:.2f}배)")

    con.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

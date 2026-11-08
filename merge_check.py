"""
협동 세션이 병합 가능한 상태인지 검사한다.

2인 세션을 돌린 직후, 두 파일을 적재하고 이것부터 실행한다.

  1. 두 파일이 같은 session_id 를 갖는가        ← 아니면 병합 설계를 다시 짜야 한다
  2. is_host 가 정확히 한쪽만 true 인가
  3. 어떤 이벤트가 두 파일에 다 들어오는가
  4. 파티 공유 자원이 이중으로 기록되는가       ← 합계가 두 배가 되는 문제
  5. 두 PC 의 시계가 얼마나 벌어져 있는가

사용법
------
    python loader.py <로그폴더> --db playtest.sqlite --reset
    python merge_check.py --db playtest.sqlite
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import statistics
import sys
from collections import defaultdict
from typing import Any

# 클라이언트가 로컬에서 계산하는 값들. 같은 사건이라도 파일마다 다르므로
# 중복 판정 키에서 빼야 한다.
LOCAL_ONLY_PARAMS = {
    "elapsed_ms", "decision_ms", "turn_no", "turn_count",
    "player_hp", "monster_hp", "player_hp_after", "monster_hp_after",
    "player_hp_remaining", "monster_hp_remaining",
    "usable_count", "total_count",
}

# 설계상 각자 기록하는 것들("사건의 주인" 규칙). 두 파일에 다 있어도 중복이 아니다.
OWNED_EVENTS = {
    "session_started", "session_ended", "game_started",
    "battle_started", "battle_ended", "battle_rewarded",
    "turn_started", "turn_skipped", "player_action", "skill_panel_opened",
    "damage_dealt", "damage_taken", "heal_applied", "status_applied",
    "item_purchased", "item_sold", "item_purchase_failed", "item_equipped",
    "resource_reason_corrected",
}

# 파티가 공유하는 상태. 한 명이 일으킨 변화를 모두가 관측하므로, 각 파일이
# 같은 사건을 한 번씩 기록한다. 합치면 참가자 수만큼 부풀려진다.
#
# 판정 키에 balance 를 쓴다. 서버가 보낸 공유 잔액이라 모든 클라이언트에서
# 같은 값이고, 연속된 거래라도 잔액은 매번 달라 충돌하지 않는다.
SHARED_STATE_EVENTS = {
    "resource_changed": ("resource_id", "delta", "balance"),
    "gold_observed": ("balance",),
}

# 시계 오차를 재기 위한 왕복 쌍. 한 플레이어의 사건과, 상대가 그것을 관측한
# 이벤트를 짝지운다. 게임마다 다르므로 원래는 게임 정의 파일에 있어야 한다.
#   (관측 이벤트, 관측 대상을 가리키는 파라미터, 당사자 쪽 원인 이벤트)
ROUNDTRIP_PAIRS = [
    ("player_downed", "user_id", "battle_ended"),
]


def parse_clock(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_events(con: sqlite3.Connection, session_id: str, player_id: str) -> list[dict]:
    rows = con.execute(
        "SELECT seq, t_ms, wall_clock, name FROM events"
        " WHERE session_id = ? AND player_id = ? ORDER BY seq",
        (session_id, player_id),
    ).fetchall()

    params: dict[int, dict[str, Any]] = defaultdict(dict)
    for seq, key, num, text, flag in con.execute(
        "SELECT seq, key, num_value, text_value, bool_value FROM event_params"
        " WHERE session_id = ? AND player_id = ?",
        (session_id, player_id),
    ):
        params[seq][key] = flag if flag is not None else (num if num is not None else text)

    return [
        {"seq": seq, "t_ms": t_ms, "wall": parse_clock(wall),
         "name": name, "params": params.get(seq, {})}
        for seq, t_ms, wall, name in rows
    ]


def shared_key(event: dict) -> tuple:
    """로컬 계산값을 뺀 나머지 파라미터로 만든 중복 판정 키."""
    return (event["name"],) + tuple(sorted(
        (k, v) for k, v in event["params"].items() if k not in LOCAL_ONLY_PARAMS
    ))


def state_key(event: dict) -> tuple | None:
    """공유 상태 이벤트의 판정 키. 해당 이벤트가 아니면 None."""
    fields = SHARED_STATE_EVENTS.get(event["name"])
    if fields is None:
        return None
    return (event["name"],) + tuple(event["params"].get(f) for f in fields)


# ------------------------------------------------------------------ 보고

def report_composition(con, session_id, players) -> list[str]:
    print("\n[1] 세션 구성")
    hosts = []
    for pid in players:
        is_host, duration, count, platform, src = con.execute(
            "SELECT is_host, duration_ms, event_count, platform, source_file"
            " FROM sessions WHERE session_id = ? AND player_id = ?",
            (session_id, pid),
        ).fetchone()
        if is_host:
            hosts.append(pid)
        dur = f"{duration / 1000:.1f}초" if duration is not None else "미종료"
        print(f"    {pid:<5} host={str(bool(is_host)):<5} 이벤트 {count:>4}건  "
              f"{dur:<9} {platform}")

    if len(hosts) == 1:
        print(f"    → 호스트 판정 정상 ({hosts[0]} 하나)")
    elif not hosts:
        print("    → [문제] 호스트가 없다. SetHost 호출 지점을 확인할 것")
    else:
        print(f"    → [문제] 호스트가 {len(hosts)}명이다: {', '.join(hosts)}")
    return hosts


def report_distribution(ev_by_player: dict[str, list[dict]]) -> tuple[list[str], list[str]]:
    print("\n[2] 이벤트 분포")
    players = list(ev_by_player)
    counts = {p: defaultdict(int) for p in players}
    for p, evs in ev_by_player.items():
        for e in evs:
            counts[p][e["name"]] += 1

    names = sorted({n for c in counts.values() for n in c})
    broadcast, shared = [], []

    head = "    {:<28}".format("이벤트") + "".join(f"{p:>6}" for p in players) + "   판정"
    print(head)
    print("    " + "-" * 28 + "-" * (6 * len(players)) + "   " + "-" * 26)
    for n in names:
        present = [p for p in players if counts[p][n]]
        if n in SHARED_STATE_EVENTS and len(present) > 1:
            verdict = "★ 공유 상태 — 중복"
            shared.append(n)
        elif n in OWNED_EVENTS:
            verdict = "각자 기록 (중복 아님)"
        elif len(present) > 1:
            verdict = "★ 브로드캐스트 — 중복"
            broadcast.append(n)
        else:
            verdict = "한쪽만"
        row = "    {:<28}".format(n) + "".join(f"{counts[p][n]:>6}" for p in players)
        print(f"{row}   {verdict}")

    return broadcast, shared


def report_shared_state(ev_by_player: dict[str, list[dict]], shared: list[str]) -> None:
    print("\n[3] 파티 공유 자원 — 합치면 몇 배가 되는가")
    if not shared:
        print("    공유 상태 이벤트가 두 파일에 겹치지 않는다. 단순 합집합이 맞다.")
        return

    players = list(ev_by_player)
    groups: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for p, evs in ev_by_player.items():
        for e in evs:
            k = state_key(e)
            if k is not None and e["name"] in shared:
                groups[k][p] = e

    dup = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"    고유 사건 {len(groups)}개 중 {len(dup)}개가 두 파일에 모두 있다")

    # 자원별 합계 비교
    totals_raw: dict[str, float] = defaultdict(float)
    totals_dedup: dict[str, float] = defaultdict(float)
    for k, v in groups.items():
        if k[0] != "resource_changed":
            continue
        rid, delta = k[1], k[2] or 0
        totals_dedup[rid] += delta
        totals_raw[rid] += delta * len(v)

    for rid in sorted(totals_raw):
        raw, ded = totals_raw[rid], totals_dedup[rid]
        factor = (raw / ded) if ded else float("nan")
        print(f"    {rid}: 단순 합계 {raw:+,.0f}  →  중복 제거 후 {ded:+,.0f}  ({factor:.1f}배)")

    # 사유 충돌 — 어느 쪽을 남길지
    conflicts = 0
    for k, v in dup.items():
        reasons = {p: e["params"].get("reason") for p, e in v.items()}
        known = [r for r in reasons.values() if r and r != "unknown"]
        if known:
            conflicts += 1
    if conflicts:
        print(f"\n    사유가 갈리는 사건 {conflicts}개 — 한쪽은 구체적 사유, 다른 쪽은 \"unknown\"")
        print("    → 병합 규칙: 사유를 아는 행을 남기고 unknown 행을 버린다.")
        print("      행위자의 클라이언트만 사유를 알고, 나머지는 잔액 변화만 본다.")
        print("      resource_reason_corrected 이벤트가 있으면 그 사유를 먼저 적용한다.")


def report_clock(ev_by_player: dict[str, list[dict]], broadcast: list[str]) -> None:
    print("\n[4] 시계 차이")
    players = list(ev_by_player)
    if len(players) != 2:
        print("    2인 세션에서만 측정한다.")
        return
    a, b = players
    ev_a, ev_b = ev_by_player[a], ev_by_player[b]

    # --- 왕복 쌍으로 시계 오차와 전달 지연을 분리한다 -----------------
    #
    # 한 방향 관측만으로는 둘을 나눌 수 없다. 관측 시각 차이 = 시계 오차 +
    # 전달 지연이고, 미지수가 둘인데 식이 하나이기 때문이다.
    # 양쪽이 서로를 관측한 쌍이 있으면 식이 둘이 되어 풀린다.
    estimates = []
    for obs_name, who_param, anchor_name in ROUNDTRIP_PAIRS:
        legs = []
        for src, dst in ((a, b), (b, a)):
            own = [e for e in ev_by_player[src] if e["name"] == anchor_name and e["wall"]]
            seen = [e for e in ev_by_player[dst]
                    if e["name"] == obs_name and e["wall"]
                    and f"p{int(e['params'].get(who_param, -1))}" == src]
            if not own or not seen:
                continue
            # 관측 직전의 당사자 사건을 원인으로 본다
            obs = seen[0]
            cause = max((e for e in own), key=lambda e: e["wall"])
            legs.append((src, dst, (obs["wall"] - cause["wall"]).total_seconds()))

        if len(legs) == 2:
            # leg1: dst 가 src 보다 g1 만큼 늦게 관측 (dst 시계 기준)
            # g1 = D - C,  g2 = D + C   (C = b 시계가 a 보다 앞선 양)
            (s1, _, g1), (s2, _, g2) = legs
            if s1 == a:
                offset = (g1 - g2) / 2.0
            else:
                offset = (g2 - g1) / 2.0
            delay = (g1 + g2) / 2.0
            estimates.append((obs_name, offset, delay, legs))

    if estimates:
        for name, offset, delay, legs in estimates:
            print(f"    왕복 쌍 '{name}' 로 분리")
            for src, dst, gap in legs:
                print(f"      {src} 의 사건 → {dst} 가 관측: {gap:+.3f}초")
            print(f"    → 시계 오차 약 {offset * 1000:+.0f}ms ({b} 가 {a} 보다 앞섬)")
            print(f"    → 전달 지연 약 {delay:.2f}초 (게임 로직 대기 포함)")
            window = max(200, int(abs(offset) * 1000 * 3))
            print(f"    → 권장 중복 제거 시간 창: ±{window}ms")
            print(f"    → JoinSession 의 hostOffsetMs: {int(offset * 1000)}")
            if abs(offset) > 1.0:
                print("    → [경고] 시계 오차가 1초를 넘는다. 두 PC 시계를 맞출 것")
    else:
        print("    서로를 관측한 왕복 쌍이 없어 시계 오차를 분리할 수 없다.")
        print("    → 병합은 wall_clock 정렬로만 한다.")

    # --- 한 방향 관측은 참고로만 ---------------------------------------
    if not broadcast:
        return
    index_b: dict[tuple, list[dict]] = defaultdict(list)
    for e in ev_b:
        if e["name"] in broadcast:
            index_b[shared_key(e)].append(e)

    deltas = []
    for e in ev_a:
        if e["name"] not in broadcast:
            continue
        pool = index_b.get(shared_key(e))
        if pool and e["wall"]:
            partner = pool.pop(0)
            if partner["wall"]:
                deltas.append((partner["wall"] - e["wall"]).total_seconds() * 1000)

    if deltas:
        med = statistics.median(deltas)
        print(f"\n    (참고) 브로드캐스트 {len(deltas)}건의 관측 시각 차이 중앙값 {med:+.0f}ms")
        print("    이 값은 시계 오차와 전달 지연이 섞여 있어 그대로 쓰면 안 된다.")


def check_session(con, session_id: str, players: list[str]) -> None:
    print(f"\n{'=' * 72}")
    print(f"세션 {session_id} — 플레이어 {len(players)}명")
    print("=" * 72)

    report_composition(con, session_id, players)
    ev_by_player = {p: load_events(con, session_id, p) for p in players}
    broadcast, shared = report_distribution(ev_by_player)
    report_shared_state(ev_by_player, shared)
    report_clock(ev_by_player, broadcast)


def main() -> int:
    ap = argparse.ArgumentParser(description="협동 세션 병합 가능 여부를 검사한다")
    ap.add_argument("--db", default="playtest.sqlite")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)

    grouped: dict[str, list[str]] = defaultdict(list)
    for sid, pid in con.execute(
        "SELECT session_id, player_id FROM sessions ORDER BY session_id, player_id"
    ):
        grouped[sid].append(pid)

    multi = {s: p for s, p in grouped.items() if len(p) > 1}
    print(f"세션 {len(grouped)}개 중 2인 이상 {len(multi)}개")

    if not multi:
        print("\n2인 이상 세션이 없다. 협동 병합은 아직 검증할 수 없다.")
        print("\n확인해야 할 것:")
        print("  · 두 PC 의 로그를 한 폴더에 모아 함께 적재했는가")
        print("  · 두 파일의 session_id 가 같은가 (파일명으로 바로 보인다)")
        for sid, pl in grouped.items():
            print(f"      {sid}  →  {', '.join(pl)}")
        return 1

    for sid, pl in multi.items():
        check_session(con, sid, pl)

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

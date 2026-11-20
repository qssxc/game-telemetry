"""
협동 세션이 병합 가능한 상태인지 검사한다.

2인 세션을 돌린 직후, 두 파일을 적재하고 이것부터 실행한다.

  1. 두 파일이 같은 session_id 를 갖는가        ← 아니면 병합 설계를 다시 짜야 한다
  2. is_host 가 정확히 한쪽만 true 인가
  3. 어떤 이벤트가 두 파일에 다 들어오는가
  4. 파티 공유 자원이 이중으로 기록되는가       ← 합계가 두 배가 되는 문제
  5. 두 PC 의 시계가 얼마나 벌어져 있는가

정의 파일을 주면 그 merge: 규칙을 기준으로 판정하고, 규칙이 없는 이벤트 중
**두 파일에 같은 내용으로 들어 있는 것**을 중복 후보로 짚어준다. 새 게임에
규칙을 처음 쓸 때는 정의 파일 없이 돌려 후보부터 본다.

사용법
------
    python loader.py <로그폴더> --db playtest.sqlite --reset
    python merge_check.py [game.yaml] --db playtest.sqlite
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import statistics
import sys
from collections import defaultdict
from typing import Any

import merge
from db import connect
from merge import Rules, RulesError, dedup_key, load_rules_file

def parse_clock(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_events(con: sqlite3.Connection, session_id: str,
                players: list[str]) -> dict[str, list[dict]]:
    """merge.load_events 로 읽고, 시계 비교를 위해 wall_clock 을 시각으로 바꾼다."""
    by_player = merge.load_events(con, session_id)
    for events in by_player.values():
        for e in events:
            e["wall"] = parse_clock(e["wall"])
    return {p: by_player.get(p, []) for p in players}


def content_key(event: dict) -> tuple:
    """이름과 파라미터 전부로 만든 키. 규칙이 없는 이벤트의 중복 후보를 찾는다.

    로컬에서 계산한 값(경과 시간 등)이 섞인 이벤트는 같은 사건이어도 여기서
    안 맞는다. 그래서 이 키로 찾은 것은 "확실한 후보" 이고, 못 찾았다고
    중복이 없다는 뜻은 아니다.
    """
    return (event["name"],) + tuple(sorted(
        (k, repr(v)) for k, v in event["params"].items()))


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


def report_distribution(ev_by_player: dict[str, list[dict]],
                        rules: Rules) -> tuple[list[str], list[str]]:
    print("\n[2] 이벤트 분포")
    players = list(ev_by_player)
    counts = {p: defaultdict(int) for p in players}
    # 규칙이 없는 이벤트에서, 다른 파일에 같은 내용으로 있는 행을 센다.
    seen_by: dict[tuple, set[str]] = defaultdict(set)
    for p, evs in ev_by_player.items():
        for e in evs:
            counts[p][e["name"]] += 1
            seen_by[content_key(e)].add(p)
    same_content: dict[str, int] = defaultdict(int)
    for key, who in seen_by.items():
        if len(who) > 1:
            same_content[key[0]] += 1

    names = sorted({n for c in counts.values() for n in c})
    broadcast, shared, candidates = [], [], []

    head = "    {:<28}".format("이벤트") + "".join(f"{p:>6}" for p in players) + "   판정"
    print(head)
    print("    " + "-" * 28 + "-" * (6 * len(players)) + "   " + "-" * 30)
    for n in names:
        present = [p for p in players if counts[p][n]]
        if len(present) < 2:
            verdict = "한쪽만"
        elif n in rules.shared_state:
            verdict = "★ 공유 상태 — 규칙 있음"
            shared.append(n)
        elif n in rules.broadcast:
            verdict = "★ 브로드캐스트 — 규칙 있음"
            broadcast.append(n)
        elif same_content.get(n):
            verdict = f"? 규칙 없음 — 같은 내용 {same_content[n]}건"
            candidates.append(n)
        else:
            verdict = "각자 기록 (규칙 없음)"
        row = "    {:<28}".format(n) + "".join(f"{counts[p][n]:>6}" for p in players)
        print(f"{row}   {verdict}")

    if candidates:
        print(f"\n    → 중복 후보 {len(candidates)}종: {', '.join(candidates)}")
        print("      두 파일에 파라미터까지 똑같은 행이 있다. 한 사건을 각자 기록한")
        print("      것이라면 정의 파일의 merge: 섹션에 규칙을 적어야 한다.")
        print("      (session_started 처럼 원래 각자 남기는 이벤트면 그대로 둔다)")
    return broadcast, shared


def report_shared_state(ev_by_player: dict[str, list[dict]], shared: list[str],
                        rules: Rules) -> None:
    print("\n[3] 파티 공유 상태 — 합치면 몇 배가 되는가")
    if not shared:
        print("    공유 상태 규칙에 걸리는 이벤트가 두 파일에 겹치지 않는다.")
        return

    # 같은 키가 한 세션에 여러 번 나올 수 있다(같은 금액 거래 반복).
    # merge.py 처럼 파일 안에서 k 번째 등장끼리 같은 사건으로 센다.
    # 키만으로 묶으면 반복된 거래가 한 건으로 뭉쳐 실제보다 적게 보인다.
    groups: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for p, evs in ev_by_player.items():
        seen: dict[tuple, int] = defaultdict(int)
        for e in evs:
            if e["name"] not in shared:
                continue
            k = dedup_key(e, rules)
            if k is not None:
                groups[(k, seen[k])][p] = e
                seen[k] += 1

    dup = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"    고유 사건 {len(groups)}개 중 {len(dup)}개가 여러 파일에 있다")

    by_event: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (k, _), v in groups.items():
        by_event[k[0]][0] += 1
        by_event[k[0]][1] += len(v)
    for name in sorted(by_event):
        uniq, raw = by_event[name]
        print(f"    {name}: 단순 합치면 {raw}행  →  실제 사건 {uniq}개  ({raw / uniq:.1f}배)")

    # 사유 충돌 — 어느 쪽을 남길지
    conflicts = 0
    for k, v in dup.items():
        reasons = [e["params"].get(rules.reason_param) for e in v.values()]
        if any(r and r != rules.unknown for r in reasons) and \
                any(not r or r == rules.unknown for r in reasons):
            conflicts += 1
    if conflicts:
        print(f"\n    사유가 갈리는 사건 {conflicts}개 — 한쪽만 사유를 안다")
        print("    → 병합 규칙: 사유를 아는 행을 남긴다. 행위자의 클라이언트만")
        print("      사유를 알고, 나머지는 변화만 본다.")


def report_clock(ev_by_player: dict[str, list[dict]], broadcast: list[str],
                 rules: Rules) -> None:
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
    for pair in rules.clock_pairs:
        obs_name = pair.observed
        legs = []
        for src, dst in ((a, b), (b, a)):
            own = sorted((e for e in ev_by_player[src]
                          if e["name"] == pair.cause and e["wall"]),
                         key=lambda e: e["wall"])
            seen = sorted((e for e in ev_by_player[dst]
                           if e["name"] == obs_name and e["wall"]
                           and pair.player_of(e) == src),
                          key=lambda e: e["wall"])
            if not own or not seen:
                continue
            # k 번째 원인을 k 번째 관측과 짝짓는다. "가장 늦은 원인" 이나
            # "관측 직전의 원인" 으로 고르면 안 된다 — 두 시계가 어긋나 있어
            # 직전·직후를 가를 기준이 없고, 한 사람이 두 번 죽으면 첫 관측이
            # 두 번째 죽음과 짝지어져 오차가 수십 초로 튄다.
            if len(own) != len(seen):
                print(f"    [주의] {src} 의 {pair.cause} {len(own)}건, "
                      f"{dst} 가 관측한 {obs_name} {len(seen)}건 — "
                      f"앞에서부터 {min(len(own), len(seen))}쌍만 쓴다")
            gaps = [(o["wall"] - c["wall"]).total_seconds()
                    for c, o in zip(own, seen)]
            legs.append((src, dst, statistics.median(gaps), len(gaps)))

        if len(legs) == 2:
            # C = b 의 시계가 a 보다 앞선 양, D = 전달 지연
            #   a → b 관측 (b 시계로 잰 관측 − a 시계로 잰 원인): g_ab = D + C
            #   b → a 관측 (a 시계로 잰 관측 − b 시계로 잰 원인): g_ba = D − C
            gap = {(s_, d_): g for s_, d_, g, _ in legs}
            g_ab, g_ba = gap[(a, b)], gap[(b, a)]
            offset = (g_ab - g_ba) / 2.0
            delay = (g_ab + g_ba) / 2.0
            estimates.append((obs_name, offset, delay, legs))

    if estimates:
        for name, offset, delay, legs in estimates:
            print(f"    왕복 쌍 '{name}' 로 분리")
            for src, dst, gap, n in legs:
                many = f" (쌍 {n}개의 중앙값)" if n > 1 else ""
                print(f"      {src} 의 사건 → {dst} 가 관측: {gap:+.3f}초{many}")
            print(f"    → 시계 오차 약 {offset * 1000:+.0f}ms ({b} 가 {a} 보다 앞섬)")
            print(f"    → 전달 지연 약 {delay:.2f}초 (게임 로직 대기 포함)")
            window = max(200, int(abs(offset) * 1000 * 3))
            print(f"    → 권장 중복 제거 시간 창: ±{window}ms")
            print(f"    → JoinSession 의 hostOffsetMs: {int(offset * 1000)}")
            if abs(offset) > 1.0:
                print("    → [경고] 시계 오차가 1초를 넘는다. 두 PC 시계를 맞출 것")
    else:
        if not rules.clock_pairs:
            print("    정의 파일에 merge.clock_pairs 가 없다 — 시계 오차를 재려면 적는다.")
        print("    서로를 관측한 왕복 쌍이 없어 시계 오차를 분리할 수 없다.")
        print("    → 병합은 wall_clock 정렬로만 한다.")

    # --- 한 방향 관측은 참고로만 ---------------------------------------
    if not broadcast:
        return
    index_b: dict[tuple, list[dict]] = defaultdict(list)
    for e in ev_b:
        if e["name"] in broadcast:
            k = dedup_key(e, rules)
            if k is not None:
                index_b[k].append(e)

    deltas = []
    for e in ev_a:
        if e["name"] not in broadcast:
            continue
        k = dedup_key(e, rules)
        pool = index_b.get(k) if k is not None else None
        if pool and e["wall"]:
            partner = pool.pop(0)
            if partner["wall"]:
                deltas.append((partner["wall"] - e["wall"]).total_seconds() * 1000)

    if deltas:
        med = statistics.median(deltas)
        print(f"\n    (참고) 브로드캐스트 {len(deltas)}건의 관측 시각 차이 중앙값 {med:+.0f}ms")
        print("    이 값은 시계 오차와 전달 지연이 섞여 있어 그대로 쓰면 안 된다.")


def check_session(con, session_id: str, players: list[str], rules: Rules) -> None:
    print(f"\n{'=' * 72}")
    print(f"세션 {session_id} — 플레이어 {len(players)}명")
    print("=" * 72)

    report_composition(con, session_id, players)
    ev_by_player = load_events(con, session_id, players)
    broadcast, shared = report_distribution(ev_by_player, rules)
    report_shared_state(ev_by_player, shared, rules)
    report_clock(ev_by_player, broadcast, rules)


def main() -> int:
    ap = argparse.ArgumentParser(description="협동 세션 병합 가능 여부를 검사한다")
    ap.add_argument("definition", nargs="?",
                    help="정의 파일 YAML. 생략하면 규칙 없이 중복 후보만 찾는다")
    ap.add_argument("--db", default="playtest.sqlite")
    args = ap.parse_args()

    rules = Rules()
    if args.definition:
        try:
            rules = load_rules_file(args.definition)
        except OSError as e:
            print(f"[오류] 정의 파일을 읽지 못했다: {e}", file=sys.stderr)
            return 1
        except RulesError as e:
            print(f"[규칙 오류] {e}", file=sys.stderr)
            return 1
    if rules.empty:
        print("merge 규칙 없이 본다 — 두 파일에 같은 내용으로 있는 이벤트를 후보로 짚는다")

    if not pathlib.Path(args.db).exists():
        print(f"[오류] DB 가 없다: {args.db}", file=sys.stderr)
        return 1
    con = connect(args.db)

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
        check_session(con, sid, pl, rules)

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

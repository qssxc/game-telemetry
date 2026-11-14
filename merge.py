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

**게임을 모른다.** 어떤 이벤트가 참가자마다 복제되는지는 게임마다 다르므로
정의 파일(YAML)의 `merge:` 섹션에서 읽는다. 섹션이 없으면 아무것도 판정하지
않는다 — 1인 게임은 그대로 두면 된다.

    merge:
      shared_state:                      # 파티가 공유하는 상태
        resource_changed:
          key: [resource_id, delta, balance]
          only: {resource_id: [gold]}    # 선택. 이 값일 때만 공유로 본다
        gold_observed: [balance]         # key 만 있으면 줄여 쓸 수 있다
      broadcast:                         # 서버가 전원에게 뿌리는 알림
        monster_killed: [raw_id, monster_id]
      reason_correction:                 # 선택. 사유를 뒤늦게 알려주는 이벤트
        event: resource_reason_corrected
        target: resource_changed
        match: [resource_id, delta]
      clock_pairs:                       # 선택. merge_check.py 의 시계 오차 측정용
        - observed: player_downed        #   상대가 관측한 이벤트
          who: user_id                   #   거기서 당사자를 가리키는 파라미터
          who_format: "p{}"              #   그 값을 player_id 로 바꾸는 틀 (기본 "{}")
          cause: battle_ended            #   당사자 쪽에 남는 원인 이벤트

사용법
------
    python loader.py <로그폴더> --db playtest.sqlite --reset
    python merge.py game.yaml --db playtest.sqlite
    python metrics.py game.yaml --db playtest.sqlite
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from db import connect,SCHEMA

# ---------------------------------------------------------------- 판정 규칙
#
# 게임마다 다른 지식이다. 코드에 두지 않고 정의 파일의 merge: 섹션에서 읽는다.
# 여기에 이벤트 이름을 딕셔너리로 박아 두면 다른 게임을 붙일 때마다 이
# 파일을 고쳐야 한다.

class RulesError(Exception):
    """merge: 섹션이 잘못된 경우. 무엇이 왜 틀렸는지 알려준다."""


@dataclass(frozen=True)
class EventRule:
    """이벤트 하나의 중복 판정 규칙.

    key   같은 사건인지 가르는 파라미터들. **모든 클라이언트에서 같은 값**인
          것만 넣는다. 서버가 내려준 값은 되고, elapsed_ms 처럼 클라이언트가
          로컬에서 계산한 값은 파일마다 달라 같은 사건이 갈라진다.
          공유 잔액(balance)은 좋은 키다 — 거래가 이어져도 매번 달라
          사실상 일련번호 역할을 한다.
    only  {파라미터: [허용 값...]}. 주어지면 이 조건에 맞는 행만 판정한다.
          한 이벤트가 공유 자원과 개인 자원을 함께 나를 때 쓴다. 이게 없으면
          두 사람이 우연히 같은 개인 변동(둘 다 경험치 +50, 잔액 50)을
          겪었을 때 같은 사건으로 판정되어 한쪽이 지표에서 빠진다.
    """
    key: tuple[str, ...]
    only: dict[str, frozenset] = field(default_factory=dict)


@dataclass(frozen=True)
class ReasonCorrection:
    """사유를 뒤늦게 알려주는 이벤트의 모양.

    event 가 나오면, 그 **앞쪽**에서 match 파라미터가 같고 사유가 unknown 인
    가장 가까운 target 행을 찾아 사유를 to_param 의 값으로 바꾼다.
    """
    event: str
    target: str
    match: tuple[str, ...]
    reason_param: str = "reason"
    to_param: str = "to_reason"
    unknown: str = "unknown"


@dataclass(frozen=True)
class ClockPair:
    """시계 오차를 재기 위한 왕복 쌍. merge_check.py 만 쓴다.

    한 플레이어에게 일어난 사건(cause)과, 상대가 그것을 관측한 이벤트
    (observed)를 짝지운다. 양쪽이 서로를 관측한 쌍이 있어야 시계 오차와
    전달 지연을 나눌 수 있다. who 파라미터의 값을 who_format 에 넣은 것이
    당사자의 player_id 와 같아야 한다 — user_id 가 2 이고 player_id 가
    "p2" 인 게임이면 who_format 은 "p{}" 다.
    """
    observed: str
    who: str
    cause: str
    who_format: str = "{}"

    def player_of(self, event: dict) -> str | None:
        value = event["params"].get(self.who)
        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)          # DB 의 숫자는 전부 REAL 이다: 2.0 → 2
        return self.who_format.format(value)


@dataclass(frozen=True)
class Rules:
    # 파티가 공유하는 상태. 한 명이 일으킨 변화를 전원이 관측한다.
    shared_state: dict[str, EventRule] = field(default_factory=dict)
    # 서버가 전원에게 뿌리는 알림. 각자 기록하면 중복이 생긴다.
    broadcast: dict[str, EventRule] = field(default_factory=dict)
    reason_correction: ReasonCorrection | None = None
    clock_pairs: tuple[ClockPair, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.shared_state or self.broadcast or self.reason_correction)

    def rule_for(self, event_name: str) -> EventRule | None:
        return self.shared_state.get(event_name) or self.broadcast.get(event_name)

    @property
    def reason_param(self) -> str:
        return self.reason_correction.reason_param if self.reason_correction else "reason"

    @property
    def unknown(self) -> str:
        return self.reason_correction.unknown if self.reason_correction else "unknown"


def _unknown_keys(got: dict, known: set[str], where: str) -> None:
    """모르는 키가 오면 멈춘다. metrics.py 와 같은 이유다 — `brodcast:` 같은
    오타를 조용히 무시하면 중복 제거가 빠진 숫자가 오류 없이 대시보드에 뜬다."""
    unknown = set(got) - known
    if not unknown:
        return
    hints = []
    for k in sorted(map(str, unknown)):
        near = difflib.get_close_matches(k, known, n=1, cutoff=0.6)
        hints.append(f"{k!r}" + (f" (혹시 {near[0]!r}?)" if near else ""))
    raise RulesError(f"{where} 에 모르는 키: {', '.join(hints)}\n"
                     f"  쓸 수 있는 키: {', '.join(sorted(known))}")


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or not all(isinstance(v, str) and v for v in value)):
        raise RulesError(f"{where} 는 파라미터 이름의 목록이어야 한다: {value!r}")
    return tuple(value)


def _norm(value: Any) -> Any:
    """YAML 의 값을 DB 에서 읽은 값과 비교할 수 있게 맞춘다.
    DB 의 숫자는 전부 REAL 이라 1 과 1.0 을 같게 봐야 한다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _event_rules(section: Any, where: str) -> dict[str, EventRule]:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise RulesError(f"{where} 는 '이벤트 이름: 규칙' 의 모음이어야 한다")
    out: dict[str, EventRule] = {}
    for event, spec in section.items():
        at = f"{where}.{event}"
        if isinstance(spec, list):                 # 줄여 쓴 꼴: 키 목록만
            out[str(event)] = EventRule(_str_list(spec, at))
            continue
        if not isinstance(spec, dict):
            raise RulesError(f"{at} 는 키 목록이거나 {{key: [...], only: {{...}}}} 여야 한다")
        _unknown_keys(spec, {"key", "only"}, at)
        if "key" not in spec:
            raise RulesError(f"{at} 에 key 가 없다")
        only_raw = spec.get("only") or {}
        if not isinstance(only_raw, dict):
            raise RulesError(f"{at}.only 는 '파라미터: [값...]' 의 모음이어야 한다")
        only = {}
        for param, allowed in only_raw.items():
            values = allowed if isinstance(allowed, list) else [allowed]
            only[str(param)] = frozenset(_norm(v) for v in values)
        out[str(event)] = EventRule(_str_list(spec["key"], f"{at}.key"), only)
    return out


def load_rules(definition: Any) -> Rules:
    """정의 파일(이미 읽은 dict)에서 merge: 섹션을 꺼낸다. 없으면 빈 규칙."""
    section = (definition or {}).get("merge") if isinstance(definition, dict) else None
    if section is None:
        return Rules()
    if not isinstance(section, dict):
        raise RulesError("merge 는 shared_state · broadcast · reason_correction 의 모음이어야 한다")
    _unknown_keys(section, {"shared_state", "broadcast", "reason_correction",
                            "clock_pairs"}, "merge")

    shared = _event_rules(section.get("shared_state"), "merge.shared_state")
    broadcast = _event_rules(section.get("broadcast"), "merge.broadcast")
    both = set(shared) & set(broadcast)
    if both:
        raise RulesError(f"shared_state 와 broadcast 양쪽에 있는 이벤트: {', '.join(sorted(both))}")

    correction = None
    rc = section.get("reason_correction")
    if rc is not None:
        at = "merge.reason_correction"
        if not isinstance(rc, dict):
            raise RulesError(f"{at} 는 event · target · match 를 가진 모음이어야 한다")
        _unknown_keys(rc, {"event", "target", "match",
                           "reason_param", "to_param", "unknown"}, at)
        for need in ("event", "target", "match"):
            if not rc.get(need):
                raise RulesError(f"{at} 에 {need} 가 없다")
        correction = ReasonCorrection(
            event=str(rc["event"]), target=str(rc["target"]),
            match=_str_list(rc["match"], f"{at}.match"),
            reason_param=str(rc.get("reason_param", "reason")),
            to_param=str(rc.get("to_param", "to_reason")),
            unknown=str(rc.get("unknown", "unknown")),
        )

    pairs = []
    raw_pairs = section.get("clock_pairs") or []
    if not isinstance(raw_pairs, list):
        raise RulesError("merge.clock_pairs 는 목록이어야 한다")
    for i, cp in enumerate(raw_pairs):
        at = f"merge.clock_pairs[{i}]"
        if not isinstance(cp, dict):
            raise RulesError(f"{at} 는 observed · who · cause 를 가진 모음이어야 한다")
        _unknown_keys(cp, {"observed", "who", "cause", "who_format"}, at)
        for need in ("observed", "who", "cause"):
            if not cp.get(need):
                raise RulesError(f"{at} 에 {need} 가 없다")
        fmt = str(cp.get("who_format", "{}"))
        try:
            fmt.format(0)
        except (IndexError, KeyError, ValueError):
            raise RulesError(f"{at}.who_format 은 '{{}}' 가 한 번 들어간 틀이어야 한다: {fmt!r}")
        pairs.append(ClockPair(str(cp["observed"]), str(cp["who"]), str(cp["cause"]), fmt))

    return Rules(shared, broadcast, correction, tuple(pairs))


def load_rules_file(path: str | pathlib.Path) -> Rules:
    import yaml
    return load_rules(yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")))


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
                             ev_by_player: dict[str, list[dict]],
                             rule: ReasonCorrection | None) -> int:
    """
    보정 이벤트가 가리키는 행에 실제 사유를 붙인다.

    예 — 인벤토리 동기화가 보상 응답보다 먼저 도착하면, 게임 쪽 어댑터는
    그 변동을 "unknown" 으로 기록한 뒤 이중 계상을 막기 위해 보정 이벤트만
    남긴다. 그 사유를 여기서 되돌려 붙여야
    중복 판정에서 어느 행을 남길지 정할 수 있다.
    """
    if rule is None:
        return 0
    written = 0
    for pid, events in ev_by_player.items():
        corrections = [e for e in events if e["name"] == rule.event]
        if not corrections:
            continue

        for corr in corrections:
            want = tuple(corr["params"].get(f) for f in rule.match)
            to_reason = corr["params"].get(rule.to_param)
            if to_reason is None:
                continue

            # 보정 이벤트 **앞쪽**에서 조건이 맞는 가장 가까운 행을 찾는다.
            target = None
            for e in events:
                if e["seq"] >= corr["seq"]:
                    break
                if (e["name"] == rule.target
                        and tuple(e["params"].get(f) for f in rule.match) == want
                        and e["params"].get(rule.reason_param) == rule.unknown):
                    target = e
            if target is None:
                print(f"    [경고] {pid} seq {corr['seq']} 의 보정 대상을 찾지 못했다")
                continue

            con.execute(
                "INSERT OR REPLACE INTO merge_overrides VALUES (?,?,?,?,?,?,?)",
                (session_id, pid, target["seq"], rule.reason_param, to_reason, None,
                 f"{rule.event}@{corr['seq']}"),
            )
            target["params"][rule.reason_param] = to_reason   # 뒤 단계가 쓰도록 메모리에도 반영
            written += 1

    return written


# ------------------------------------------------------------ 2. 중복 판정

def dedup_key(event: dict, rules: Rules) -> tuple | None:
    """이 이벤트가 중복 판정 대상이면 키를, 아니면 None 을 돌려준다."""
    rule = rules.rule_for(event["name"])
    if rule is None:
        return None
    for param, allowed in rule.only.items():
        if _norm(event["params"].get(param)) not in allowed:
            return None          # 공유 대상이 아니다 — 각자의 기록이 모두 진짜다
    values = tuple(event["params"].get(f) for f in rule.key)
    if all(v is None for v in values):
        return None              # 키로 쓸 값이 하나도 없다 — 판정하지 않는다
    return (event["name"],) + values


def pick_canonical(group: list[dict], host: str | None, rules: Rules) -> dict:
    """
    같은 사건의 복사본 중 남길 하나를 고른다.

    1순위: 사유를 아는 행. 행위자의 클라이언트만 왜 바뀌었는지 안다.
           (사유 파라미터가 없는 게임에서는 전부 동률이라 2순위로 넘어간다)
    2순위: 호스트. 게임 상태의 기준이 되는 쪽이다.
    3순위: player_id 순. 돌릴 때마다 결과가 바뀌지 않게 하기 위한 고정 규칙.
    """
    def rank(e: dict) -> tuple:
        reason = e["params"].get(rules.reason_param)
        knows_reason = reason is not None and reason != rules.unknown
        return (0 if knows_reason else 1,
                0 if e["player_id"] == host else 1,
                e["player_id"])

    return min(group, key=rank)


def dedup_session(con, session_id: str, ev_by_player: dict[str, list[dict]],
                  host: str | None, rules: Rules) -> dict[str, int]:
    # 키별로 각 플레이어의 관측을 순서대로 모은다.
    #
    # 같은 키가 한 세션에 여러 번 나올 수 있다(같은 금액 거래가 반복되는 경우).
    # 시간 창으로 맞추면 시계 오차에 끌려다니므로, 파일 안에서의 등장 순서로
    # 짝을 짓는다. k 번째 등장끼리 같은 사건으로 본다.
    buckets: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for pid, events in ev_by_player.items():
        for e in events:
            k = dedup_key(e, rules)
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
            keep = pick_canonical(group, host, rules)
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

def merge(con: sqlite3.Connection, rules: Rules | None = None) -> int:
    rules = rules or Rules()
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
    if multi and not (rules.shared_state or rules.broadcast):
        # 조용히 넘어가면 안 되는 경우다. 공유 자원이 참가자 수만큼 부풀려진
        # 숫자가 오류 없이 대시보드에 뜬다.
        print(f"  [경고] 2인 이상 세션이 {len(multi)}개 있는데 중복 판정 규칙이 없다. "
              f"참가자마다 복제되는 이벤트가 있다면 합계가 부풀려진다 — "
              f"정의 파일에 merge: 섹션을 적어라 (README 참고)")

    total = {"corrections": 0, "groups": 0, "duplicates": 0, "mismatched": 0}

    for sid, players in sessions.items():
        ev = load_events(con, sid)

        corrections = apply_reason_corrections(con, sid, ev, rules.reason_correction)
        total["corrections"] += corrections

        if len(players) < 2:
            if corrections:
                print(f"  {sid}: 사유 보정 {corrections}건 (1인 세션 — 중복 판정 없음)")
            continue

        stats = dedup_session(con, sid, ev, hosts.get(sid), rules)
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
    ap.add_argument("definition", nargs="?",
                    help="정의 파일 YAML. merge: 섹션에서 판정 규칙을 읽는다. "
                         "생략하면 규칙 없이 돈다(1인 게임)")
    ap.add_argument("--db", default="playtest.sqlite")
    ap.add_argument("--show", action="store_true",
                    help="판정 전후의 합계를 이벤트별로 보여준다")
    args = ap.parse_args()

    rules = Rules()
    if args.definition:
        if not pathlib.Path(args.definition).exists():
            print(f"[오류] 정의 파일이 없다: {args.definition}", file=sys.stderr)
            return 1
        try:
            rules = load_rules_file(args.definition)
        except RulesError as e:
            print(f"[규칙 오류] {e}", file=sys.stderr)
            return 1

    con = connect(args.db)
    con.execute("PRAGMA journal_mode = WAL")
    rc = merge(con, rules)

    if args.show:
        # 이벤트 이름도 파라미터 이름도 모르는 채로 보여줄 수 있는 것은 행 수다.
        # 합계가 몇 배 부풀려져 있었는지는 이 비율과 같다.
        print("\n판정 전후 — 중복이 걸러진 이벤트")
        print("-" * 58)
        rows = con.execute("""
            SELECT name, COUNT(*), SUM(canonical)
            FROM events
            WHERE name IN (SELECT DISTINCT name FROM events WHERE canonical = 0)
            GROUP BY name ORDER BY name
        """).fetchall()
        if not rows:
            print("  걸러진 행이 없다")
        for name, raw, kept in rows:
            factor = (raw / kept) if kept else float("nan")
            print(f"  {name:<26} 전체 {raw:>7,}행   병합 후 {kept:>7,}행   ({factor:.2f}배)")

    con.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

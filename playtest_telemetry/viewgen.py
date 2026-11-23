"""
이벤트마다 조회용 뷰를 만들어 낸다. **게임을 모른다.**

왜 필요한가
-----------
파라미터를 세로(long)로 저장하는 덕에 게임에 이벤트를 추가해도 테이블은
그대로다. 대신 조회가 불편해지고, 그 불편을 뷰가 받아낸다. 그런데 그
뷰를 사람이 손으로 쓰면 결국 "계측을 늘릴 때마다 손이 간다" 로 되돌아간다.

뷰의 모양은 기계적이다. 이벤트 이름 하나와 그 이벤트가 가진 파라미터
목록만 있으면 쿼리가 정해진다. 그리고 그 정보는 이미 DB 안에 있다.
그래서 읽어서 써낸다.

무엇을 알아내는가
---------------
  · 어떤 이벤트가 있는가            events.name
  · 각 이벤트가 어떤 파라미터를 갖는가  event_params_merged.key
  · 그 파라미터가 숫자인가 글자인가 참/거짓인가
  · 숫자라면 정수인가 실수인가       소수부가 있는 행이 하나라도 있으면 실수

마지막 항목이 손으로 쓸 때 제일 자주 틀리는 곳이다. damage 는 33.04 라
INTEGER 로 자르면 안 되고, turn_count 는 잘라야 읽기 좋다. 데이터를 보면
틀릴 일이 없다.

무엇을 못 하는가
--------------
계산이 들어간 뷰는 못 만든다. 누적 잔액(예: ptel init 이 만드는 views.sql 의 v_coin_ledger),
두 이벤트를 한 축으로 묶기 같은 것은 사람이 무엇을 보고 싶은지 정해야
나온다. 그런 뷰는 <게임 폴더>/views.sql 에
손으로 쓰고, 여기서는 --skip-existing 으로 건너뛴다.

merge.py 뒤에 돌려야 한다
------------------------
협동 세션에서 공유 자원과 브로드캐스트 이벤트는 참가자 파일마다 복제된다.
거르지 않고 합치면 참가자 수만큼 부풀려지는데, **오류는 나지 않고 숫자만
틀리며 1인 세션에서는 멀쩡하다.** 그래서 걸러진 뷰를 기본으로 만든다.

어떤 이벤트가 복제되는지는 merge.py 가 판정해 canonical 칼럼에 남긴다.
여기서는 그 결과만 보므로 이벤트 이름을 알 필요가 없다 — 이 파일이 어느
게임에도 붙는 이유다. 다만 merge.py 가 먼저 돌아야 알 수 있다.

    loader.py  →  merge.py  →  viewgen.py  →  metrics.py

적재 직후에 돌리면 canonical 이 전부 1 이라 필터가 붙지 않는다. 틀린 뷰가
되는 것은 아니고(그 시점엔 중복이 표시되지 않았을 뿐) merge 뒤에 다시
돌리면 필터가 붙는다.

사용법
-----
    python -m playtest_telemetry.viewgen --db playtest.sqlite --skip-existing mygame/views.sql --apply --no-file

PowerShell 에서도 리다이렉션 없이 쓸 수 있게 --out 과 --apply 를 둔다.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys

from .db import connect, run_atomically

# events 테이블이 이미 들고 있는 칼럼. 파라미터 이름이 이것과 겹치면
# 칼럼 이름이 충돌하므로 접미사를 붙인다.
ENVELOPE = ("session_id", "player_id", "seq", "t_ms", "wall_clock",
            "name", "tag", "tier", "build_version", "canonical", "dup_group")

# 뷰에 항상 넣는 봉투 칼럼. canonical 은 협동 세션에서 중복 행을 걸러낼 때
# 필요하고, 1인 세션에서는 전부 1 이라 방해가 되지 않는다.
CARRY = ("session_id", "player_id", "seq", "t_ms", "wall_clock", "canonical")

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 자동 생성한 뷰에 남기는 표시. 뷰의 SQL 안(AS 뒤)에 넣어야 sqlite_master 에
# 함께 저장된다 — CREATE 앞의 주석은 저장되지 않는다. 이 표시가 있는 뷰만
# "옛 자동 뷰" 로 보고 지운다. 손으로 쓴 뷰에는 없으니 건드리지 않는다.
MARKER = "/* viewgen */"


def quote(ident: str) -> str:
    """SQL 식별자를 큰따옴표로 감싼다.

    파라미터 이름이 order · group · index · from · to · default 처럼 SQL
    예약어면 `AS order` 가 문법 오류가 된다. 뷰 생성은 한 트랜잭션이라
    이벤트 하나 때문에 모든 이벤트의 뷰가 만들어지지 않는다.
    """
    return '"' + ident.replace('"', '""') + '"'


def event_names(con: sqlite3.Connection) -> list[str]:
    """뷰를 만들 수 있는 이벤트 이름.

    이벤트 이름은 뷰 이름(v_<이름>)과 WHERE 절의 문자열에 그대로 들어간다.
    `ui.click` 같은 이름이 하나 섞이면 `CREATE VIEW v_ui.click` 이 되어
    SQLite 가 v_ui 를 데이터베이스 이름으로 읽고, 한 트랜잭션으로 묶인
    뷰 생성 전체가 실패한다. 파라미터 키와 같은 규칙으로 먼저 거른다.
    걸러진 이벤트도 events 와 event_flat 에서는 그대로 보인다.
    """
    out = []
    for (name,) in con.execute(
            "SELECT DISTINCT name FROM events WHERE name IS NOT NULL ORDER BY name"):
        if IDENT.match(name):
            out.append(name)
        else:
            print(f"  [건너뜀] 이벤트 {name!r} — 뷰 이름으로 쓸 수 없다")
    return out


def param_types(con: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """
    이벤트별 파라미터와 그 타입을 알아낸다.

    타입은 선언이 아니라 **실제 들어 있는 값**으로 정한다. 세로 저장은
    한 칼럼에 한 종류만 넣으므로, 어느 칼럼이 채워져 있는지를 보면 된다.
    """
    rows = con.execute("""
        SELECT e.name, p.key,
               SUM(p.num_value  IS NOT NULL),
               SUM(p.text_value IS NOT NULL),
               SUM(p.bool_value IS NOT NULL),
               SUM(p.num_value IS NOT NULL
                   AND p.num_value <> CAST(p.num_value AS INTEGER))
        FROM events e
        JOIN event_params_merged p
          ON p.session_id = e.session_id
         AND p.player_id  = e.player_id
         AND p.seq        = e.seq
        GROUP BY e.name, p.key
        ORDER BY e.name, p.key
    """).fetchall()

    out: dict[str, list[tuple[str, str]]] = {}
    for name, key, n_num, n_text, n_bool, n_frac in rows:
        if not key or not IDENT.match(key):
            print(f"  [건너뜀] {name}.{key!r} — 칼럼 이름으로 쓸 수 없다")
            continue
        # 한 키에 여러 타입이 섞이면 가장 많이 나온 쪽을 택한다. 섞이는 것
        # 자체가 계측의 문제 신호이므로 알려준다.
        counts = {"num": n_num or 0, "text": n_text or 0, "bool": n_bool or 0}
        kind = max(counts, key=counts.get)
        if sum(1 for v in counts.values() if v) > 1:
            print(f"  [경고] {name}.{key} 에 여러 타입이 섞여 있다 {counts} "
                  f"— {kind} 로 읽는다")
        if kind == "num":
            kind = "real" if (n_frac or 0) > 0 else "int"
        out.setdefault(name, []).append((key, kind))
    return out


EXPR = {
    "int":  "CAST({a}.num_value AS INTEGER)",
    "real": "{a}.num_value",
    "text": "{a}.text_value",
    "bool": "{a}.bool_value",
}


def dup_events(con: sqlite3.Connection) -> set[str]:
    """중복으로 걸러진 행(canonical = 0)이 실제로 있는 이벤트.

    merge.py 의 설정을 읽지 않는다. **데이터만 본다.** 어떤 이벤트가 협동
    세션에서 참가자 수만큼 복제되는지는 게임마다 다르고, 그 지식은 정의 파일의
    merge: 섹션에 있다. 그걸 여기서 또 읽으면 이 파일이 게임을 알게 된다.
    대신 merge.py 가 남긴 결과(canonical)를 보면 이름을 몰라도 알 수 있다.

    그래서 이 함수는 merge.py 가 **돌아간 뒤에** 불려야 뜻이 있다.
    적재 직후에는 canonical 이 전부 1 이라 빈 집합이 나온다.
    """
    return {r[0] for r in con.execute(
        "SELECT DISTINCT name FROM events WHERE canonical = 0 AND name IS NOT NULL")}


def build_view(event: str, params: list[tuple[str, str]], prefix: str,
               dedup: bool = False, suffix: str = "") -> str:
    """
    dedup=True 면 canonical = 1 만 보는 뷰를 만든다.

    왜 기본으로 거르는가 — 협동 세션에서 공유 자원과 브로드캐스트 이벤트는
    참가자 파일마다 복제된다. 거르지 않고 합치면 정확히 참가자 수만큼
    부풀려진다(실측: 골드 -9,650 vs 실제 -7,900, 몬스터 처치 2건 vs 1건).
    **오류가 나지 않고 숫자만 틀리며, 1인 세션에서는 멀쩡하다.** 조회하는
    사람이 매번 기억해야 하는 종류의 규칙이 아니다.

    원본은 지우지 않는다. 같은 이벤트의 _all 뷰가 걸러지지 않은 전부를 준다.
    """
    view = quote(f"{prefix}{event}{suffix}")
    cols = [f"    e.{c}" for c in CARRY]
    joins = []
    # SQLite 의 칼럼 이름은 대소문자를 가리지 않는다. hp 와 HP 가 함께 있으면
    # 뒤쪽이 "HP:1" 이라는 이상한 이름이 되므로 미리 접미사를 붙인다.
    used = {c.lower() for c in CARRY}

    for i, (key, kind) in enumerate(params):
        alias = f"p{i}"
        label = f"{key}_param" if key in ENVELOPE else key
        n = 2
        base = label
        while label.lower() in used:
            label = f"{base}_{n}"
            n += 1
        used.add(label.lower())
        cols.append(f"    {EXPR[kind].format(a=alias):<34} AS {quote(label)}")
        joins.append(
            f"LEFT JOIN event_params_merged {alias}\n"
            f"       ON {alias}.session_id = e.session_id\n"
            f"      AND {alias}.player_id  = e.player_id\n"
            f"      AND {alias}.seq        = e.seq\n"
            f"      AND {alias}.key        = '{key}'"
        )

    note = "" if params else "    -- 파라미터가 없는 이벤트다. 봉투만 보여준다.\n"
    if dedup:
        head = (f"-- {event} ({len(params)}개 파라미터) — viewgen.py 가 만들었다.\n"
                f"-- 협동 세션에서 참가자마다 복제되는 이벤트라 canonical = 1 만 본다.\n"
                f"-- 거르지 않은 전부가 필요하면 {prefix}{event}_all 을 쓴다.\n")
        where = f"WHERE e.name = '{event}'\n  AND e.canonical = 1;\n"
    elif suffix == "_all":
        head = (f"-- {event} — 거르지 않은 전부. 중복 복제 행이 섞여 있다.\n"
                f"-- 집계에 쓰면 참가자 수만큼 부풀려진다. 병합 검증용이다.\n")
        where = f"WHERE e.name = '{event}';\n"
    else:
        head = (f"-- {event} ({len(params)}개 파라미터) — viewgen.py 가 만들었다. "
                f"손으로 고치지 말 것.\n")
        where = f"WHERE e.name = '{event}';\n"

    return (
        head
        + f"DROP VIEW IF EXISTS {view};\n"
        f"CREATE VIEW {view} AS {MARKER}\n"
        f"{note}SELECT\n" + ",\n".join(cols) + "\n"
        "FROM events e\n"
        + ("".join(j + "\n" for j in joins))
        + where
    )


def covered_events(path: pathlib.Path) -> set[str]:
    """이미 손으로 쓴 뷰가 다루는 이벤트 이름.

    `WHERE e.name = 'x'` 과 `WHERE e.name IN ('a','b')` 두 모양을 읽는다.
    여기 걸리는 이벤트는 생성하지 않는다 — 손으로 쓴 쪽에는 누적 합계나
    중복 제거처럼 기계가 알 수 없는 것이 들어 있기 때문이다.
    """
    if not path.exists():
        return set()
    sql = path.read_text(encoding="utf-8")
    found = set(re.findall(r"e\.name\s*=\s*'([^']+)'", sql))
    for group in re.findall(r"e\.name\s+IN\s*\(([^)]*)\)", sql, re.I):
        found |= set(re.findall(r"'([^']+)'", group))
    return found


def generate(con: sqlite3.Connection, skip: set[str], prefix: str = "v_",
             ) -> tuple[list[str], list[tuple[str, int, bool]], list[str]]:
    """DB 를 읽어 이벤트별 뷰의 SQL 을 만든다.

    돌려주는 것: (SQL 조각들, [(뷰 이름, 파라미터 수, 중복 제거 여부)],
                  지울 옛 자동 뷰 이름들)
    skip 에 든 이벤트는 건너뛴다 — 손으로 쓴 뷰가 이미 다루는 이벤트다.
    중복 복제가 확인된 이벤트는 걸러진 뷰와 _all 뷰 둘을 만든다.

    이번에 만들지 않는 자동 뷰는 지운다. 이벤트가 로그에서 사라졌거나,
    중복이 없어져 _all 이 필요 없어졌거나, 손으로 쓴 뷰가 그 이벤트를
    맡게 된 경우다. 남겨 두면 Metabase 에 같은 데이터를 보는 뷰가 둘 보이거나
    아무도 갱신하지 않는 뷰가 남는다. MARKER 가 있는 뷰만 지운다.

    viewgen.py 와 pipeline.py 가 같이 쓴다.
    """
    names = event_names(con)
    types = param_types(con)
    dups = dup_events(con)

    parts: list[str] = []
    made: list[tuple[str, int, bool]] = []
    for name in names:
        if name in skip:
            continue
        params = types.get(name, [])
        dedup = name in dups
        parts.append(build_view(name, params, prefix, dedup=dedup))
        made.append((f"{prefix}{name}", len(params), dedup))
        if dedup:
            # 원본은 지우지 않는다. 걸러지기 전을 볼 수 있어야 병합이
            # 제대로 됐는지 확인할 수 있다.
            parts.append(build_view(name, params, prefix, suffix="_all"))
            made.append((f"{prefix}{name}_all", len(params), False))

    keep = {v for v, _, _ in made}
    stale = sorted(
        name for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
        if sql and MARKER in sql and name not in keep)
    parts = [f"DROP VIEW IF EXISTS {quote(v)};\n" for v in stale] + parts
    return parts, made, stale


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m playtest_telemetry.viewgen",
        description="이벤트마다 조회용 뷰를 만들어 낸다")
    ap.add_argument("--db", default="playtest.sqlite")
    ap.add_argument("--out", default="views_generated.sql",
                    help="써낼 파일 (--apply 만 쓰면 파일은 안 만든다)")
    ap.add_argument("--prefix", default="v_", help="뷰 이름 앞에 붙일 것")
    ap.add_argument("--skip-existing", default=None, metavar="VIEWS.SQL",
                    help="이 파일이 이미 다루는 이벤트는 건너뛴다")
    ap.add_argument("--apply", action="store_true",
                    help="파일로 쓰는 대신(또는 함께) DB 에 바로 반영한다")
    ap.add_argument("--no-file", action="store_true", help="파일을 쓰지 않는다")
    args = ap.parse_args(argv)

    db = pathlib.Path(args.db)
    if not db.exists():
        print(f"[오류] DB 가 없다: {db}", file=sys.stderr)
        return 1

    skip = covered_events(pathlib.Path(args.skip_existing)) if args.skip_existing else set()
    if skip:
        print(f"손으로 쓴 뷰가 다루는 이벤트 {len(skip)}개는 건너뛴다: "
              f"{', '.join(sorted(skip))}")

    con = connect(str(db))
    try:
        parts, made, stale = generate(con, skip, args.prefix)
    except sqlite3.Error as e:
        print(f"[오류] DB 를 읽지 못했다: {e}\n"
              f"  event_params_merged 뷰가 필요하다. ptel run 으로 먼저 적재하라.",
              file=sys.stderr)
        con.close()
        return 1

    deduped = sorted(n[len(args.prefix):] for n, _, d in made if d)
    if deduped:
        print(f"중복 복제가 확인된 이벤트 {len(deduped)}개는 canonical = 1 만 본다: "
              f"{', '.join(deduped)}")
    else:
        print("중복 복제된 행이 없다 — 필터 없이 만든다. "
              "(협동 세션을 적재하고 병합을 돌린 뒤 다시 생성하면 필터가 붙는다)")

    if not made:
        print("[오류] 만들 뷰가 없다. 로그를 적재했는지 확인하라.", file=sys.stderr)
        con.close()
        return 1
    if stale:
        print(f"더 이상 만들지 않는 옛 자동 뷰 {len(stale)}개를 지운다: {', '.join(stale)}")

    header = (
        "-- viewgen.py 가 만든 파일이다. 직접 고치지 말 것 — 다시 생성하면 덮어쓴다.\n"
        "--\n"
        "-- 이벤트 하나당 뷰 하나. 칼럼과 타입은 DB 에 실제로 들어 있는 값에서\n"
        "-- 알아냈다. 계측을 늘린 뒤 다시 돌리면 새 이벤트의 뷰가 따라온다.\n"
        "--\n"
        "-- 누적 잔액이나 중복 제거처럼 사람이 정해야 하는 뷰는 게임 폴더의 views.sql 에 쓴다.\n\n"
    )
    sql = header + "\n".join(parts)

    if not args.no_file:
        pathlib.Path(args.out).write_text(sql, encoding="utf-8")
        print(f"\n{args.out} — 뷰 {len(made)}개")

    if args.apply:
        # 한 트랜잭션으로 묶는다. DROP 과 CREATE 사이를 읽는 쪽(Metabase)이
        # 보면 `no such table` 로 카드가 깨진다.
        if not run_atomically(con, sql, "뷰 적용"):
            con.close()
            return 1
        print(f"{db} 에 적용 완료")

    width = max(len(n) for n, _, _ in made)
    for n, k, d in made:
        print(f"  {n:<{width}}  파라미터 {k}개" + ("   [중복 제거됨]" if d else ""))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

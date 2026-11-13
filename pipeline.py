"""
적재부터 Metabase 까지 한 명령으로 돌린다.

단계마다 따로 돌려도 되지만(README 참고) 순서를 틀리면 조용히 잘못된 결과가
나온다. 특히 viewgen 은 merge 뒤여야 한다 — 중복 복제된 행을 걸러낸 뷰를
만들려면 merge 가 먼저 판정을 끝내야 하고, 적재 직후에 돌리면 canonical 이
전부 1 이라 필터가 안 붙는다. 그 순서를 여기 한 곳에 박아둔다.

    1. 적재      loader    NDJSON → events / event_params, 기본 뷰
    2. 병합      merge     협동 세션의 중복 판정 (canonical)
    3. 이벤트 뷰 viewgen   이벤트마다 뷰. 중복 이벤트는 걸러진 뷰 + _all
    4. 지표      metrics   정의 파일 → metric_results
    5. 카드      metabase  (--push 일 때만)

한 프로세스 안에서 **커넥션 하나**로 돈다. 프로세스를 다섯 번 띄우지 않고,
한 단계가 실패하면 거기서 멈춘다.

읽는 쪽에서 보이는 것
-------------------
각 단계는 그 자체로는 원자적이다(적재는 전부 아니면 전무, 지표는 지웠다
다시 쓰는 것이 한 트랜잭션). 다만 **단계 사이**는 커밋되므로, 돌아가는 동안
대시보드를 보고 있으면 몇 초간 "새 이벤트 + 옛 지표" 같은 중간 상태가
보인다. 전부를 한 트랜잭션으로 묶으려면 merge 와 metrics 가 자기 커밋을
하지 않도록 고쳐야 하는데, 지금 규모(30만 이벤트에 10초)에서는 그만한
값어치가 없다고 보고 두었다.

사용법
-----
    python pipeline.py <로그폴더>
    python pipeline.py <로그폴더> --reset --push
    python pipeline.py --skip-load            # 로그는 그대로, 뒷단계만 다시
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import sqlite3
import sys
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))


class StepFailed(Exception):
    """단계 하나가 실패했다. 메시지는 사람이 읽고 고칠 수 있게 쓴다."""


# 단계별 상세 출력에서 이것만 통과시킨다. 나머지는 --verbose 로 본다.
# 지표 31개가 한 줄씩 찍히면 정작 봐야 할 요약이 묻힌다.
KEEP = ("[경고]", "[오류]", "[실패]", "[건너뜀]", "[중단]", "※")


def run_step(n: int, total: int, title: str, fn, verbose: bool) -> None:
    print(f"\n[{n}/{total}] {title}")
    t0 = time.monotonic()
    buf = io.StringIO()
    ctx = contextlib.nullcontext() if verbose else contextlib.redirect_stdout(buf)
    try:
        with ctx:
            note = fn()
    except BaseException:
        # 실패했으면 삼켰던 출력을 전부 보여준다. 원인이 거기 있다.
        if not verbose and buf.getvalue():
            print(buf.getvalue().rstrip())
        raise
    if not verbose:
        for line in buf.getvalue().splitlines():
            if any(k in line for k in KEEP):
                print(f"      {line.strip()}")
    print(f"      {time.monotonic() - t0:.1f}초" + (f" — {note}" if note else ""))


# ------------------------------------------------------------------ 단계

def do_load(con, log_dir: pathlib.Path, reset: bool, views: pathlib.Path) -> str:
    import loader
    files = sorted(log_dir.glob("*.ndjson"))
    if not files:
        raise StepFailed(f"{log_dir} 에 .ndjson 파일이 없다")

    totals = {"events": 0, "errors": 0, "lines": 0}
    try:
        loader.begin_load(con, reset=reset)
        for path in files:
            r = loader.load_file(con, path)
            for k in totals:
                totals[k] += r[k]
        con.commit()
    except (sqlite3.Error, OSError) as e:
        con.rollback()           # 옛 데이터가 그대로 남는다
        raise StepFailed(f"적재 실패 — 기존 데이터를 유지한다: {e}") from e

    con.executescript(loader.INDEXES)
    con.execute("ANALYZE")
    con.commit()

    if not loader.apply_views(con, views):
        raise StepFailed(f"{views.name} 적용 실패")

    skipped = f", 건너뛴 줄 {totals['errors']}" if totals["errors"] else ""
    return f"파일 {len(files)}개, 이벤트 {totals['events']}건{skipped}"


def do_merge(con) -> str:
    import merge
    if merge.merge(con) != 0:          # 0 이 성공. 건수가 아니다.
        raise StepFailed("병합 실패 — 위 메시지를 보라")
    n = con.execute("SELECT COUNT(*) FROM events WHERE canonical = 0").fetchone()[0]
    kinds = con.execute(
        "SELECT COUNT(DISTINCT name) FROM events WHERE canonical = 0").fetchone()[0]
    if not n:
        return "중복 없음 (1인 세션만 있거나 merge.py 설정이 비어 있다)"
    return f"중복 {n}행 제외 ({kinds}종 이벤트)"


def do_viewgen(con, views: pathlib.Path, prefix: str) -> str:
    import loader
    import viewgen
    names = viewgen.event_names(con)
    types = viewgen.param_types(con)
    skip = viewgen.covered_events(views)
    dups = viewgen.dup_events(con)

    parts, made, deduped = [], 0, 0
    for name in names:
        if name in skip:
            continue
        params = types.get(name, [])
        is_dup = name in dups
        parts.append(viewgen.build_view(name, params, prefix, dedup=is_dup))
        made += 1
        if is_dup:
            parts.append(viewgen.build_view(name, params, prefix, suffix="_all"))
            made += 1
            deduped += 1

    if not parts:
        raise StepFailed("만들 뷰가 없다 — 적재가 됐는지 확인하라")
    if not loader.run_atomically(con, "\n".join(parts), "이벤트 뷰 생성"):
        raise StepFailed("이벤트 뷰 생성 실패")

    note = f"뷰 {made}개"
    if deduped:
        note += f" (중복 이벤트 {deduped}종은 canonical=1 만 본다: {', '.join(sorted(dups & set(names) - skip))})"
    return note


def do_metrics(con, yaml_path: pathlib.Path) -> str:
    import yaml as _yaml
    import metrics
    if not yaml_path.exists():
        raise StepFailed(f"지표 정의 파일이 없다: {yaml_path}")
    definition = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    try:
        written = metrics.compute(con, definition)
    except metrics.DefinitionError as e:
        # 정의가 틀렸으면 아무것도 쓰지 않는다. 절반만 계산된 결과를 남기면
        # 그래프가 조용히 틀린 값을 그린다.
        con.rollback()
        raise StepFailed(f"정의 오류 — {e}") from e
    con.commit()
    return f"metric_results {written}행"


def do_push(db_path: pathlib.Path, yaml_path: pathlib.Path, collection: str) -> str:
    import metabase_api
    rc = metabase_api.main([
        "--db-file", str(db_path), "--push",
        "--from-yaml", str(yaml_path), "--collection", collection,
    ])
    if rc != 0:
        raise StepFailed("Metabase 반영 실패 — 위 메시지를 보라")
    return "카드 반영 완료"


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser(
        description="적재 → 병합 → 뷰 → 지표 (→ Metabase) 를 한 번에 돌린다")
    ap.add_argument("log_dir", nargs="?", help="*.ndjson 이 들어있는 폴더")
    ap.add_argument("--db", default="playtest.sqlite")
    ap.add_argument("--yaml", default=None, help="지표 정의 (기본: hybridrpg.yaml)")
    ap.add_argument("--views", default=None, help="기본 뷰 (기본: views.sql)")
    ap.add_argument("--reset", action="store_true",
                    help="기존 내용을 비우고 새로 적재한다 (DB 파일은 유지한다)")
    ap.add_argument("--skip-load", action="store_true",
                    help="적재를 건너뛴다. 이미 적재된 DB 로 뒷단계만 다시 돌릴 때")
    ap.add_argument("--push", action="store_true", help="Metabase 카드까지 반영한다")
    ap.add_argument("--collection", default="플레이테스트 지표")
    ap.add_argument("--prefix", default="v_")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="각 단계의 상세 출력을 전부 보여준다")
    args = ap.parse_args()

    db_path = pathlib.Path(args.db)
    views = pathlib.Path(args.views) if args.views else HERE / "views.sql"
    yml = pathlib.Path(args.yaml) if args.yaml else HERE / "hybridrpg.yaml"

    if not args.skip_load and not args.log_dir:
        print("[오류] 로그 폴더를 지정하라 (또는 --skip-load)", file=sys.stderr)
        return 1
    if args.skip_load and not db_path.exists():
        print(f"[오류] --skip-load 인데 DB 가 없다: {db_path}", file=sys.stderr)
        return 1

    import loader
    con = loader.connect(str(db_path), fast=True)
    total = 5 if args.push else 4
    started = time.monotonic()

    try:
        n = 0
        if args.skip_load:
            total -= 1
            print(f"적재 건너뜀 — 기존 {db_path} 를 쓴다")
        else:
            n += 1
            run_step(n, total, f"적재 — {args.log_dir} → {db_path}",
                     lambda: do_load(con, pathlib.Path(args.log_dir),
                                     args.reset, views), args.verbose)

        n += 1
        run_step(n, total, "협동 세션 병합",
                 lambda: do_merge(con), args.verbose)

        n += 1
        run_step(n, total, "이벤트 뷰 생성",
                 lambda: do_viewgen(con, views, args.prefix), args.verbose)

        n += 1
        run_step(n, total, f"지표 계산 — {yml.name}",
                 lambda: do_metrics(con, yml), args.verbose)

        con.close()

        if args.push:
            n += 1
            run_step(n, total, "Metabase 카드 반영",
                     lambda: do_push(db_path, yml, args.collection), args.verbose)

    except StepFailed as e:
        print(f"\n[중단] {e}", file=sys.stderr)
        try:
            con.close()
        except sqlite3.Error:
            pass
        return 1

    print(f"\n전체 완료 — {time.monotonic() - started:.1f}초")
    if not args.push:
        print("Metabase 까지 반영하려면 --push 를 붙인다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

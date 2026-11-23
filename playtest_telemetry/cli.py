"""
`ptel` 명령. 분석 도구의 입구다.

    ptel init <폴더>                     게임 폴더의 틀을 만든다
    ptel run <로그폴더> --game <폴더>     적재 → 병합 → 뷰 → 지표 (→ Metabase)
    ptel check --game <폴더>             협동 세션이 병합 가능한 상태인지 본다
    ptel push --game <폴더>              Metabase 카드만 생성하거나 갱신한다

`ptel` 이 PATH 에 없으면 `python -m playtest_telemetry` 로 같은 명령을 쓴다.
각 단계를 따로 돌리는 모듈(`python -m playtest_telemetry.loader` 등)도 그대로 있다.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

from . import __version__

TEMPLATES = pathlib.Path(__file__).parent / "templates"
TEMPLATE_FILES = ("game.yaml", "views.sql", "cards.py")


# ------------------------------------------------------------------ init

def cmd_init(args: argparse.Namespace) -> int:
    folder = pathlib.Path(args.folder)
    if folder.exists() and not folder.is_dir():
        print(f"[오류] 폴더가 아니라 파일이다: {folder}", file=sys.stderr)
        return 1

    existing = [name for name in TEMPLATE_FILES if (folder / name).exists()]
    if existing and not args.force:
        # 손으로 고친 정의 파일을 예시로 덮어쓰면 되돌릴 방법이 없다.
        print(f"[오류] {folder} 에 이미 있다: {', '.join(existing)}\n"
              f"  덮어쓰려면 --force 를 붙인다", file=sys.stderr)
        return 1

    folder.mkdir(parents=True, exist_ok=True)
    for name in TEMPLATE_FILES:
        shutil.copyfile(TEMPLATES / name, folder / name)
        print(f"  만듦  {folder / name}")

    print(f"\n{folder}/game.yaml 을 열어 자기 게임의 이벤트 이름으로 바꾼다.\n"
          f"views.sql 과 cards.py 는 선택이다. 필요 없으면 지운다.\n\n"
          f"  ptel run <로그폴더> --game {folder}")
    return 0


# ------------------------------------------------------------------ run

def cmd_run(args: argparse.Namespace) -> int:
    from . import pipeline
    return pipeline.run(args)


# ------------------------------------------------------------------ check

def cmd_check(args: argparse.Namespace) -> int:
    from . import merge_check, pipeline
    try:
        game = pipeline.resolve_game(args.game, args.yaml)
    except pipeline.GameError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    return merge_check.main([str(game.yaml), "--db", args.db])


# ------------------------------------------------------------------ push

def cmd_push(args: argparse.Namespace) -> int:
    from . import pipeline
    try:
        game = pipeline.resolve_game(args.game, args.yaml)
    except pipeline.GameError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    if not pathlib.Path(args.db).exists():
        print(f"[오류] DB 가 없다: {args.db} — ptel run 으로 먼저 만든다", file=sys.stderr)
        return 1
    return pipeline.push_cards(pathlib.Path(args.db), game, args.collection,
                               dry_run=args.dry_run)


# ------------------------------------------------------------------ 입구

def build_parser() -> argparse.ArgumentParser:
    from . import pipeline

    ap = argparse.ArgumentParser(
        prog="ptel",
        description="Playtest Telemetry 분석 도구. "
                    "`ptel <명령> --help` 로 각 명령의 옵션을 본다.")
    ap.add_argument("--version", action="version", version=f"ptel {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="<명령>")

    p = sub.add_parser("init", help="게임 폴더의 틀을 만든다",
                       description="game.yaml, views.sql, cards.py 세 파일을 예시 내용으로 만든다. "
                                   "필수는 game.yaml 하나다.")
    p.add_argument("folder", metavar="폴더", help="만들 게임 폴더")
    p.add_argument("--force", action="store_true", help="같은 이름의 파일이 있어도 덮어쓴다")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="적재, 병합, 뷰 생성, 지표 계산을 차례로 돌린다",
                       description="적재 → 협동 세션 병합 → 이벤트 뷰 생성 → 지표 계산 "
                                   "(--push 면 Metabase 카드까지)")
    pipeline.add_run_arguments(p)
    p.set_defaults(func=cmd_run)

    def game_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--game", default=None, metavar="폴더", help="게임 폴더 (필수, 또는 --yaml)")
        p.add_argument("--yaml", default=None, metavar="파일", help="게임 폴더 대신 정의 파일 하나")
        p.add_argument("--db", default="playtest.sqlite", metavar="파일",
                       help="결과 DB 파일 (기본: playtest.sqlite)")

    p = sub.add_parser("check", help="협동 세션이 병합 가능한 상태인지 본다",
                       description="2인 이상 세션을 적재한 뒤 돌린다. 호스트가 한 명인지, 어떤 "
                                   "이벤트가 여러 파일에 겹쳐 있는지, 병합 규칙이 그것을 다 덮는지를 보여준다.")
    game_options(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("push", help="Metabase 카드만 생성하거나 갱신한다",
                       description="ptel run --push 의 마지막 단계만 따로 돌린다. "
                                   "METABASE_URL 과 METABASE_API_KEY 환경변수를 읽는다.")
    game_options(p)
    p.add_argument("--collection", default="플레이테스트 지표", metavar="이름",
                   help="카드를 담을 Metabase 컬렉션 (기본: 플레이테스트 지표)")
    p.add_argument("--dry-run", action="store_true", help="보내지 않고 무엇이 바뀔지만 보여준다")
    p.set_defaults(func=cmd_push)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

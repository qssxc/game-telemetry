"""
Metabase 연결 모듈 — 1단계: 서버 확인, 인증, 데이터베이스 목록.

왜 필요한가
-----------
지표 정의(YAML)를 고치면 새 지표가 나온다. 그런데 그게 제대로 나왔는지는
눈으로 봐야 알 수 있고, 보는 곳이 Metabase 다. 지표를 하나 추가할 때마다
Metabase 에서 카드를 손으로 만들면 "정의만 고치면 된다" 는 말이 거짓이 된다.
그래서 카드 생성까지 자동화한다. 이 파일은 그 첫 단계다.

단계
----
  1단계 (이 파일)  서버 확인 · 인증 · DB 목록          ← 연결이 되는지부터
  2단계            카드 하나 만들기
  3단계            지표 카드 전체 생성/갱신 (upsert)

의존성을 늘리지 않는다
--------------------
requests 를 쓰지 않고 표준 라이브러리 urllib 로 짰다. 이 프로젝트의 외부
의존성은 pyyaml 하나뿐이고, 받는 사람이 설치할 것을 늘리지 않는 편이 낫다.

인증 방식 두 가지
---------------
  API 키   권장. 관리자 → 설정 → 인증 → API 키에서 만든다.
           만료가 없고 사람 계정과 분리된다. 헤더: x-api-key
  아이디/비밀번호
           POST /api/session 으로 세션 토큰을 받는다. 토큰에 만료가 있어
           오래 도는 작업에는 맞지 않는다. 헤더: X-Metabase-Session

사용법
-----
PowerShell (Windows 기본)

    $env:METABASE_URL     = "http://localhost:3000"
    $env:METABASE_API_KEY = "mb_xxxxxxxx"
    python metabase_api.py --db-file playtest.sqlite

    # 아이디/비밀번호로 할 때
    $env:METABASE_USER     = "me@example.com"
    $env:METABASE_PASSWORD = "..."

bash / zsh (macOS · Linux)

    export METABASE_URL=http://localhost:3000
    export METABASE_API_KEY=mb_xxxxxxxx
    python metabase_api.py --db-file playtest.sqlite

PowerShell 에는 export 가 없다. $env:NAME = "값" 으로 설정하고, bash 처럼
`VAR=값 명령` 한 줄에 붙이는 문법도 없으니 먼저 설정한 뒤 실행한다.
설정한 값은 그 창에서만 산다 — 창을 새로 열면 다시 설정해야 한다.

비밀값은 인자로 받지 않고 환경변수로 받는다. 명령줄 인자는 셸 히스토리와
프로세스 목록에 남는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class MetabaseError(Exception):
    """Metabase 와 이야기하다 실패했다. 메시지는 사람이 읽고 고칠 수 있게 쓴다."""


class Metabase:
    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self._token: str | None = None

        # 자격증명이 없어도 만들 수 있다. health() 로 서버가 떠 있는지만
        # 확인하는 쓰임이 있고, 인증 실패와 서버 미기동을 갈라놓아야 한다.
        # 인증이 필요한 요청을 보낼 때 비로소 막는다.
        if not api_key and user and password:
            self._token = self._login(user, password)

    # ------------------------------------------------------------ 저수준

    def _request(
        self, method: str, path: str, body: Any = None, authed: bool = True
    ) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authed:
            if self._api_key:
                headers["x-api-key"] = self._api_key
            elif self._token:
                headers["X-Metabase-Session"] = self._token
            else:
                raise MetabaseError(
                    "인증 정보가 없다. METABASE_API_KEY 또는 "
                    "METABASE_USER + METABASE_PASSWORD 를 설정하라."
                )

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, headers=headers, method=method
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise MetabaseError(self._explain_http(e, method, path)) from e
        except urllib.error.URLError as e:
            raise MetabaseError(
                f"{self.url} 에 닿지 못했다 ({e.reason}).\n"
                "  · Metabase 가 떠 있는지\n"
                "  · URL 의 호스트/포트가 맞는지 (기본 포트 3000)\n"
                "  · 컨테이너 안에서 돌린다면 localhost 가 아니라 "
                "서비스 이름이어야 한다"
            ) from e

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # HTML 이 돌아오면 거의 항상 /api 가 아닌 곳을 찍은 것이다.
            raise MetabaseError(
                f"{method} {path} 가 JSON 이 아닌 응답을 돌려줬다. "
                f"URL 에 경로가 덧붙어 있지 않은지 보라 (받은 앞부분: {raw[:80]!r})"
            ) from None

    def _explain_http(
        self, e: urllib.error.HTTPError, method: str, path: str
    ) -> str:
        """상태 코드별로 실제 원인을 짚어준다. 디버그 왕복을 줄이는 것이 목적이다."""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            detail = ""

        hint = {
            400: "요청 본문이 Metabase 가 기대하는 모양이 아니다.",
            401: ("인증이 거부됐다. API 키가 폐기됐거나 "
                  "세션 토큰이 만료됐을 수 있다."),
            403: ("권한이 없다. 이 계정이 관리자인지 확인하라 — "
                  "DB 등록과 카드 생성은 관리자 권한이 필요하다."),
            404: (f"{path} 가 없다. Metabase 버전이 달라 경로가 바뀌었을 수 있다."),
        }.get(e.code, "")

        return (f"{method} {path} → HTTP {e.code} {e.reason}"
                + (f"\n  {hint}" if hint else "")
                + (f"\n  서버 응답: {detail}" if detail else ""))

    def _login(self, user: str, password: str) -> str:
        r = self._request(
            "POST", "/api/session",
            {"username": user, "password": password}, authed=False,
        )
        if not isinstance(r, dict) or "id" not in r:
            raise MetabaseError(
                f"로그인 응답에 세션 토큰이 없다. 받은 것: {r!r}"
            )
        return str(r["id"])

    # ------------------------------------------------------------ 1단계 기능

    def health(self) -> dict[str, Any]:
        """인증 없이 서버가 떠 있는지만 본다.

        인증 실패와 서버 미기동을 갈라놓기 위해 따로 둔다. 둘이 섞이면
        "비밀번호가 틀렸나" 를 한참 들여다보게 된다.
        """
        r = self._request("GET", "/api/health", authed=False)
        return r if isinstance(r, dict) else {"status": r}

    def databases(self) -> list[dict[str, Any]]:
        """등록된 데이터베이스 목록.

        응답 모양이 버전에 따라 다르다. 0.46 이전은 배열을 그대로 주고,
        이후는 {"data": [...], "total": n} 으로 감싼다. 둘 다 받는다.
        """
        r = self._request("GET", "/api/database")
        if isinstance(r, dict):
            r = r.get("data", [])
        if not isinstance(r, list):
            raise MetabaseError(f"DB 목록이 배열이 아니다: {type(r).__name__}")
        return r

    def find_database(self, db_file: str) -> dict[str, Any] | None:
        """SQLite 파일 경로로 등록된 DB 를 찾는다.

        Metabase 는 등록 시 받은 경로를 details.db 에 그대로 보관한다.
        컨테이너 안의 경로라 호스트 경로와 다를 수 있으므로, 전체 경로가
        아니라 파일 이름으로 맞춰본다.
        """
        want = os.path.basename(db_file)
        for d in self.databases():
            if d.get("engine") != "sqlite":
                continue
            got = (d.get("details") or {}).get("db") or ""
            if os.path.basename(got) == want:
                return d
        return None

    # ------------------------------------------------------------ 2단계: 컬렉션

    def collections(self) -> list[dict[str, Any]]:
        r = self._request("GET", "/api/collection")
        if isinstance(r, dict):
            r = r.get("data", [])
        return [c for c in r if isinstance(c, dict)]

    def ensure_collection(self, name: str) -> dict[str, Any]:
        """이름의 컬렉션을 찾고, 없으면 만든다.

        생성한 카드를 한곳에 모으기 위해서다. 루트에 흩뿌리면 사람이 만든
        카드와 섞여서, 다시 돌릴 때 무엇을 덮어써도 되는지 알 수 없게 된다.
        """
        for c in self.collections():
            if c.get("name") == name and not c.get("archived"):
                return c
        # color 는 구버전에서 필수였다. 신버전은 무시하므로 넣어두는 편이 안전하다.
        return self._request(
            "POST", "/api/collection",
            {"name": name, "description": "metrics.py 가 생성한 카드", "color": "#509EE3"},
        )

    # ------------------------------------------------------------ 2단계: 카드

    def cards(self) -> list[dict[str, Any]]:
        r = self._request("GET", "/api/card")
        if isinstance(r, dict):
            r = r.get("data", [])
        return [c for c in r if isinstance(c, dict)]

    def find_card(self, name: str, collection_id: int | None) -> dict[str, Any] | None:
        for c in self.cards():
            if c.get("archived"):
                continue
            if c.get("name") == name and c.get("collection_id") == collection_id:
                return c
        return None

    def upsert_card(
        self,
        name: str,
        sql: str,
        database_id: int,
        collection_id: int | None = None,
        display: str = "table",
        visualization_settings: dict[str, Any] | None = None,
        description: str | None = None,
        preserve_visuals: bool = True,
    ) -> tuple[dict[str, Any], str]:
        """같은 이름의 카드가 있으면 갱신하고, 없으면 만든다.

        갱신(PUT)인 이유 — 지우고 다시 만들면 카드 id 가 바뀐다. 그러면
        대시보드에 올려둔 카드가 떨어지고, 사람이 손으로 다시 붙여야 한다.
        지표 정의를 고칠 때마다 그러면 자동화의 뜻이 없다.

        preserve_visuals — 갱신할 때는 SQL 만 바꾸고 차트 모양(display,
        visualization_settings)은 그대로 둔다. 여기서 주는 모양은 "처음
        만들 때의 기본값" 이다. 사람이 Metabase 에서 축을 바꾸거나 색을
        고쳐놨는데 지표를 하나 추가했다고 그게 날아가면, 두 번째부터는
        아무도 이 도구를 돌리지 않는다.

        돌려주는 두 번째 값은 "created" 또는 "updated" 다.
        """
        existing = self.find_card(name, collection_id)

        body = {
            "name": name,
            "dataset_query": build_native_query(sql, database_id, name),
            "display": display,
            "visualization_settings": visualization_settings or {},
            "collection_id": collection_id,
        }
        if description:
            body["description"] = description

        if existing and preserve_visuals:
            body["display"] = existing.get("display") or body["display"]
            body["visualization_settings"] = (
                existing.get("visualization_settings") or body["visualization_settings"]
            )

        if existing:
            return self._request("PUT", f"/api/card/{existing['id']}", body), "updated"
        return self._request("POST", "/api/card", body), "created"


# ---------------------------------------------------------------- 쿼리 조립


TAG_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
OPTIONAL_RE = re.compile(r"\[\[(.*?)\]\]", re.S)


def build_template_tags(sql: str, card_name: str) -> dict[str, Any]:
    """SQL 안의 {{변수}} 를 찾아 Metabase 가 요구하는 선언을 만든다.

    Metabase 는 쿼리에 {{x}} 를 쓰면 template-tags 에 x 의 선언이 있어야
    카드를 받는다. 손으로 쓰면 지표가 늘 때마다 같이 늘어나므로 SQL 에서
    뽑아낸다.

    필수/선택 판정 — [[...]] 안에서만 쓰인 변수는 선택이다. Metabase 는
    값이 비면 그 블록을 통째로 지운다. 블록 밖에 있는 변수는 값이 없으면
    쿼리 자체가 성립하지 않으므로 필수다.
    """
    inside = set()
    for block in OPTIONAL_RE.findall(sql):
        inside |= set(TAG_RE.findall(block))
    outside = set(TAG_RE.findall(OPTIONAL_RE.sub("", sql)))

    tags: dict[str, Any] = {}
    for var in sorted(inside | outside):
        tags[var] = {
            # id 를 난수로 만들면 돌릴 때마다 카드가 "바뀐 것"이 된다.
            # 카드 이름과 변수 이름으로 고정해, 같은 입력이면 같은 선언이 나오게 한다.
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{card_name}/{var}")),
            "name": var,
            "display-name": var.replace("_", " ").title(),
            "type": "text",
            "required": var in outside,
        }
    return tags


def build_native_query(sql: str, database_id: int, card_name: str) -> dict[str, Any]:
    """네이티브 SQL 카드의 dataset_query 를 만든다."""
    return {
        "type": "native",
        "database": database_id,
        "native": {"query": sql.strip(),
                   "template-tags": build_template_tags(sql, card_name)},
    }


# ---------------------------------------------------------------- CLI


def client_from_env(url: str | None = None) -> Metabase:
    """환경변수로 클라이언트를 만든다."""
    url = url or os.environ.get("METABASE_URL", "http://localhost:3000")
    return Metabase(
        url,
        api_key=os.environ.get("METABASE_API_KEY"),
        user=os.environ.get("METABASE_USER"),
        password=os.environ.get("METABASE_PASSWORD"),
    )


def main(argv: list[str] | None = None) -> int:
    """argv 를 받는 이유 — pipeline.py 가 한 프로세스 안에서 부른다.
    None 이면 평소처럼 명령줄을 읽는다."""
    ap = argparse.ArgumentParser(
        description="Metabase 연결을 확인하고 데이터베이스 목록을 보여준다"
    )
    ap.add_argument("--url", default=None,
                    help="Metabase 주소 (기본: $METABASE_URL 또는 localhost:3000)")
    ap.add_argument("--db-file", default=None,
                    help="이 SQLite 파일이 등록돼 있는지 확인한다")
    ap.add_argument("--push", action="store_true",
                    help="카드를 Metabase 에 생성/갱신한다 "
                         "(cards.py 의 게임 전용 카드 + --from-yaml 로 생성한 카드)")
    ap.add_argument("--collection", default="플레이테스트 지표",
                    help="카드를 담을 컬렉션 이름")
    ap.add_argument("--dry-run", action="store_true",
                    help="보내지 않고 무엇이 바뀔지만 보여준다")
    ap.add_argument("--from-yaml", default=None,
                    help="지표 정의 YAML 에서 지표마다 카드를 만든다 "
                         "(예: hybridrpg.yaml)")
    args = ap.parse_args(argv)

    url = args.url or os.environ.get("METABASE_URL", "http://localhost:3000")

    # ① 서버가 떠 있는가 — 인증 전에 본다
    try:
        status = Metabase(url).health().get("status", "?")
        print(f"서버      {url} — {status}")
    except MetabaseError as e:
        print(f"[실패] 서버 확인\n{e}", file=sys.stderr)
        return 1

    # ② 인증이 되는가
    try:
        mb = client_from_env(url)
    except MetabaseError as e:
        print(f"[실패] 인증\n{e}", file=sys.stderr)
        return 1
    if mb._api_key:
        print("인증      API 키")
    elif mb._token:
        print("인증      아이디/비밀번호")
    else:
        print("[실패] 인증 정보가 없다. METABASE_API_KEY 또는 "
              "METABASE_USER + METABASE_PASSWORD 를 설정하라.", file=sys.stderr)
        return 1

    # ③ 무엇이 등록돼 있는가
    try:
        dbs = mb.databases()
    except MetabaseError as e:
        print(f"[실패] DB 목록\n{e}", file=sys.stderr)
        return 1

    print(f"\n등록된 데이터베이스 {len(dbs)}개")
    for d in dbs:
        path = (d.get("details") or {}).get("db") or ""
        print(f"  id={d.get('id'):<4} {d.get('engine','?'):<10} "
              f"{d.get('name','?')}" + (f"   ({path})" if path else ""))

    db = None
    if args.db_file:
        db = mb.find_database(args.db_file)
        print()
        if db:
            print(f"{os.path.basename(args.db_file)} → id={db['id']} ({db.get('name')})")
        else:
            print(f"{os.path.basename(args.db_file)} 는 Metabase 에 등록돼 있지 않다. "
                  "관리자 → 데이터베이스에서 먼저 추가하라.")

    if not args.push:
        return 0

    # ------------------------------------------------------------- 카드 밀어넣기
    if not db:
        print("\n[실패] --push 에는 --db-file 로 대상 DB 를 지정해야 한다.",
              file=sys.stderr)
        return 1

    try:
        import cardgen
    except ImportError as e:
        print(f"\n[실패] cardgen.py 를 불러오지 못했다: {e}", file=sys.stderr)
        return 1

    todo: dict[str, tuple[str, str]] = {}
    sample_params: dict[str, Any] = {}

    # 게임 전용 카드는 선택이다. 새 게임은 지표 정의 YAML 만으로 시작할 수
    # 있어야 하고, 그 게임을 위한 손수 쓴 카드는 나중 일이다.
    try:
        import cards
        todo.update({n: (s, cards.DISPLAY.get(n, "table"))
                     for n, s in cards.CARDS.items()})
        sample_params = dict(getattr(cards, "SAMPLE_PARAMS", {}))
        print(f"\ncards.py — 게임 전용 카드 {len(todo)}개")
    except ImportError:
        print("\ncards.py 없음 — 지표 정의에서 생성한 카드만 올린다")

    if args.from_yaml:
        gen = cardgen.from_metrics(args.from_yaml)
        print(f"{os.path.basename(args.from_yaml)} 에서 카드 {len(gen)}개 생성")
        todo.update(gen)

    if not todo:
        print("\n[실패] 올릴 카드가 없다. --from-yaml 로 지표 정의를 주거나 "
              "cards.py 를 두어라.", file=sys.stderr)
        return 1

    # 보내기 전에 로컬 SQLite 로 쿼리를 돌려본다.
    # 깨진 SQL 을 Metabase 에 올려두면 대시보드에서 빨간 카드로 발견하게 되는데,
    # 그때는 어느 변경이 깼는지 알기 어렵다. 여기서 먼저 걸러낸다.
    checked, broken = {}, 0
    local = args.db_file if os.path.exists(args.db_file) else None
    if local:
        import sqlite3
        con = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
        print(f"\n로컬 검사 — {local}")
    else:
        print(f"\n로컬 검사 건너뜀 — {args.db_file} 가 여기 없다")

    for name, (sql, display) in todo.items():
        if not local:
            checked[name] = (sql, display)
            continue
        try:
            # 필수 변수에는 표본값을 넣어 돌려본다. 선택 변수는 비워 둔다
            # — Metabase 가 값 없이 열었을 때와 같은 상태다.
            con.execute(cardgen.render(sql, sample_params)).fetchone()
            checked[name] = (sql, display)
        except Exception as e:
            broken += 1
            print(f"  [깨짐] {name}: {e}")

    if broken:
        print(f"\n[중단] {broken}개 카드의 SQL 이 로컬에서 실패했다. "
              "Metabase 에 올리지 않는다.", file=sys.stderr)
        return 1
    if local:
        print(f"  {len(checked)}개 모두 통과")

    col = mb.ensure_collection(args.collection)
    print(f"\n컬렉션   {col['name']} (id={col['id']})")

    if args.dry_run:
        for name in checked:
            state = "갱신" if mb.find_card(name, col["id"]) else "생성"
            print(f"  [미실행] {state}  {name}")
        return 0

    made = {"created": 0, "updated": 0}
    for name, (sql, display) in checked.items():
        try:
            card, how = mb.upsert_card(name, sql, db["id"], col["id"], display=display)
            made[how] += 1
            print(f"  {'생성' if how == 'created' else '갱신'}  "
                  f"{name}   (card id={card['id']})")
        except MetabaseError as e:
            print(f"  [실패] {name}\n{e}", file=sys.stderr)
            return 1
    print(f"\n완료 — 생성 {made['created']}개, 갱신 {made['updated']}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())

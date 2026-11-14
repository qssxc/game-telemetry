"""
지표 정의에서 Metabase 카드를 만든다. **게임을 모른다.**

이 파일이 중립 층이다. 읽는 것은 지표 정의 YAML 과, 어느 게임에나 똑같이
존재하는 지표 뷰(v_metrics_session · v_metrics_party · v_metrics_dimension)
뿐이다. 게임 고유의 뷰(v_battles, v_purchases ...)나 이벤트 이름은 쓰지 않는다.

그래서 새 게임에 붙일 때 이 파일은 손대지 않는다. 필요한 것은 그 게임의
지표 정의 YAML 하나뿐이다. 손으로 쓴 게임 전용 카드는 선택이다.

구성
----
    cardgen.py               (여기) 중립 — 지표 정의 → 카드
    views.sql                중립 — 기본 뷰와 지표 뷰
    games/<게임>/cards.py    게임 전용 — 손으로 쓴 카드 (선택)
    games/<게임>/views.sql   게임 전용 — 손으로 쓴 뷰 (선택)
"""

from __future__ import annotations

import re
from typing import Any

# 지표 이름은 SQL 에 문자열로 박힌다. 따옴표가 섞인 이름이 들어오면 쿼리가
# 깨지거나, 더 나쁘게는 엉뚱한 쿼리가 된다. 모양을 먼저 확인한다.
# \w 는 한글도 받으므로 한글 지표 이름은 통과한다.
NAME_RE = re.compile(r"^\w+$")


def session_label(col: str = "session_id") -> str:
    """세션 id 를 축에 쓸 짧은 이름으로 바꾸는 SQL 식.

    밑줄 앞부분만 떼어낸다. session_id 가 `room9234_20261027` 처럼
    방 이름 뒤에 날짜가 붙는 꼴이면 축에 붙는 글자가 길어서다.

    **밑줄이 없으면 통째로 쓴다.** 이게 없으면 다른 게임에서 조용히 깨진다 —
    `gp-tokyo-01` 같은 id 는 INSTR 이 0 을 돌려주고 SUBSTR(...,1,-1) 이 빈
    문자열이 되어, 축에 세션 이름이 사라진다. 서로 다른 두 경기가 같은 칸에
    겹쳐 보이는데 오류는 나지 않는다. 이 도구가 이미 한 번 밟은 종류의
    함정이다(세션을 넘나들며 같은 player_id 가 섞이던 문제).
    """
    return (f"CASE WHEN INSTR({col}, '_') > 1"
            f" THEN SUBSTR({col}, 1, INSTR({col}, '_') - 1)"
            f" ELSE {col} END")


def render(sql: str, params: dict[str, Any]) -> str:
    """Metabase 의 선택적 절 치환을 흉내낸다.

    Metabase 는 변수가 비면 [[ ]] 블록을 통째로 지우고, 값이 들어오면
    괄호만 벗긴 뒤 {{var}} 를 값으로 바꾼다. 카드를 올리기 전에 로컬에서
    같은 상태로 돌려보려면 이 치환이 필요하다.
    """
    def block(m):
        body = m.group(1)
        names = re.findall(r"\{\{\s*(\w+)\s*\}\}", body)
        if any(params.get(n) in (None, "") for n in names):
            return ""
        return body
    out = re.sub(r"\[\[(.*?)\]\]", block, sql, flags=re.S)

    def var(m):
        v = params.get(m.group(1))
        if v is None:
            raise KeyError(f"필수 변수 {m.group(1)} 에 값이 없다")
        return "'" + str(v).replace("'", "''") + "'"
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", var, out)


def from_metrics(yaml_path: str) -> dict[str, tuple[str, str]]:
    """지표 정의 파일에서 카드를 만든다.

    돌려주는 모양: {카드이름: (sql, 차트종류)}

    지표의 scope 와 by 에 따라 세 가지로 갈린다.
      scope: party        세션당 한 행 (참가자 수를 같이 보여준다)
      by: 있음            축으로 쪼갠 카드
      그 외               세션 × 플레이어 카드
    """
    import yaml
    with open(yaml_path, encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}

    out: dict[str, tuple[str, str]] = {}
    for m in spec.get("metrics") or []:
        name = m.get("name")
        if not name:
            continue
        if not NAME_RE.match(str(name)):
            print(f"  [건너뜀] 지표 이름에 쓸 수 없는 문자: {name!r}")
            continue

        scope = m.get("scope", "session")
        by = m.get("by")

        if scope == "party" and not by:
            # 파티 지표는 세션당 한 행이다. 참가자 수를 같이 내보낸다 —
            # 1인 세션과 4인 세션의 파티 합계를 나란히 놓으면 오해하기 쉽다.
            out[f"파티 · {name}"] = (f"""
SELECT {session_label()} AS "세션",
       party_size        AS "인원",
       ROUND(value, 2)   AS "{name}"
FROM v_metrics_party
WHERE metric_name = '{name}'
  [[AND session_id = {{{{session_id}}}}]]
ORDER BY 1
""", "bar")
        elif by:
            out[f"지표 · {name} ({by})"] = (f"""
SELECT dimension_value      AS "{by}",
       ROUND(SUM(value), 2) AS "{name}"
FROM v_metrics_dimension
WHERE metric_name = '{name}'
  [[AND session_id = {{{{session_id}}}}]]
GROUP BY 1
ORDER BY 2 DESC
""", "bar")
        else:
            # 세션과 플레이어를 함께 축에 쓴다. player_id 는 세션 안에서만
            # 의미가 있어서, 플레이어만으로 묶으면 다른 세션의 기록이 한
            # 사람으로 합쳐진다.
            out[f"지표 · {name}"] = (f"""
SELECT {session_label()} || ' · ' || player_id AS "세션 · 플레이어",
       ROUND(value, 2)                         AS "{name}"
FROM v_metrics_session
WHERE metric_name = '{name}'
  [[AND session_id = {{{{session_id}}}}]]
ORDER BY 1
""", "bar")
    return out

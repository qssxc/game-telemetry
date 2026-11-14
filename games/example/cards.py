"""
게임 전용 Metabase 카드의 틀. **선택 파일이다 — 필요 없으면 지운다.**

지표 정의(game.yaml)에 적은 지표는 카드가 자동으로 만들어진다(cardgen.py).
여기에는 그걸로 안 되는 카드만 손으로 쓴다 — 이 게임의 뷰(views.sql)나
이벤트별 자동 뷰(v_<이벤트이름>)를 직접 조회하는 카드.

pipeline.py --push 가 이 파일을 읽는다. 따로 올릴 때는

    python metabase_api.py --db-file playtest.sqlite --push \\
        --from-yaml games/mygame/game.yaml --cards games/mygame/cards.py

읽는 이름은 셋이다.

    CARDS          {카드 이름: SQL}. 필수.
    DISPLAY        {카드 이름: 차트 종류}. 없으면 table.
                   처음 만들 때의 기본값이다 — Metabase 에서 모양을 바꾸면
                   다시 올려도 그 모양이 유지된다.
    SAMPLE_PARAMS  {변수 이름: 표본값}. 올리기 전 로컬 검사에서 필수 변수
                   ({{변수}} 가 [[ ]] 밖에 있는 것)에 넣어 돌려본다.

SQL 안의 [[ ... {{변수}} ]] 는 Metabase 의 선택적 필터다. 변수가 비면 그
블록이 통째로 사라진다.

아래는 예시다. 뷰·칼럼 이름을 자기 게임 것으로 바꾼다.
"""

CARDS = {}

CARDS['코인 잔액 추이'] = """
SELECT t_ms / 1000.0     AS "경과(초)",
       cumulative_delta  AS "누적 변동"
FROM v_coin_ledger
WHERE canonical = 1
  [[AND session_id = {{session_id}}]]
ORDER BY session_id, seq
"""

DISPLAY = {
    '코인 잔액 추이': 'line',
}

SAMPLE_PARAMS = {}

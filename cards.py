"""
HybridRPG 전용 카드. **이 파일은 게임을 안다.**

여기 적힌 SQL 은 v_battles, v_purchases, v_party_ledger 처럼 이 게임을 위해
손으로 쓴 뷰와 monster_name 같은 이 게임의 파라미터를 쓴다. 다른 게임에
그대로 가져다 쓸 수 없고, 그게 맞다 — "전투 HP 추이" 는 전투가 있는
게임에서만 뜻이 있다.

범용 카드 생성기는 cardgen.py 다. 그쪽은 지표 정의 YAML 과 중립 지표 뷰만
읽으므로 어느 게임에나 붙는다. **새 게임을 붙일 때 필요한 것은 그 게임의
YAML 하나이고, 이 파일은 없어도 된다.**

단독 실행하면 카드를 Metabase 에 올리지 않고 로컬 SQLite 로 돌려본다.
Metabase 의 선택적 절([[...]])을 흉내내어 필터 있음/없음 상태를 모두 본다.

    python cards.py playtest.sqlite
"""
import sqlite3, sys

from cardgen import render

CARDS = {}

CARDS['1. 행동 결정 시간'] = """
SELECT action                  AS "행동",
       COUNT(*)                AS "횟수",
       ROUND(AVG(decision_ms)) AS "평균 결정시간(ms)"
FROM v_actions
WHERE decision_ms >= 0
  [[AND session_id = {{session_id}}]]
GROUP BY action
ORDER BY 3 DESC
"""

CARDS['2. 전투 결과'] = """
SELECT b.session_id                                   AS "세션",
       b.player_id                                    AS "플레이어",
       COALESCE(m.text_value, '?')                    AS "몬스터",
       CASE WHEN b.is_victory THEN '승리' ELSE '패배' END AS "결과",
       b.turn_count                                   AS "턴",
       ROUND(b.elapsed_ms / 1000.0, 1)                AS "소요(초)",
       ROUND(b.player_hp_remaining, 1)                AS "잔여HP"
FROM v_battles b
LEFT JOIN event_params_merged m
       ON m.session_id = b.session_id
      AND m.player_id  = b.player_id
      AND m.key        = 'monster_name'
      AND m.seq = (SELECT MAX(e2.seq) FROM events e2
                    WHERE e2.session_id = b.session_id
                      AND e2.player_id  = b.player_id
                      AND e2.name = 'battle_started'
                      AND e2.seq < b.seq)
[[WHERE b.session_id = {{session_id}}]]
ORDER BY b.session_id, b.seq
"""

CARDS['3. 재화 흐름'] = """
SELECT reason      AS "사유",
       COUNT(*)    AS "건수",
       SUM(delta)  AS "합계"
FROM v_party_ledger
WHERE resource_id = 'gold'
  [[AND session_id = {{session_id}}]]
GROUP BY reason
ORDER BY 3
"""

CARDS['4. 골드 잔액 추이'] = """
SELECT "세션", "경과(초)", "잔액"
FROM (
  SELECT session_id              AS "세션",
         ROUND(t_ms / 1000.0, 1) AS "경과(초)",
         balance_reported        AS "잔액",
         ROW_NUMBER() OVER (PARTITION BY session_id, ROUND(t_ms / 1000.0, 1)
                            ORDER BY t_ms DESC) AS rn
  FROM v_resource_ledger
  WHERE canonical = 1
    AND resource_id = 'gold'
    AND balance_reported IS NOT NULL
    [[AND session_id = {{session_id}}]]
)
WHERE rn = 1
ORDER BY "세션", "경과(초)"
"""

CARDS['5. 부위별 장착'] = """
SELECT COALESCE(category, 'unknown') AS "부위",
       COUNT(*)                      AS "장착 횟수",
       COUNT(DISTINCT item_id)       AS "아이템 종류"
FROM v_equips
[[WHERE session_id = {{session_id}}]]
GROUP BY 1
ORDER BY 2 DESC
"""

# 지표 이름을 적어두지 않는다. 예전에는 8개를 IN (...) 으로 박아뒀는데,
# 그러면 YAML 에 지표를 추가해도 이 카드에 안 나온다. "정의만 고치면 된다" 는
# 말이 깨지는 자리였다. 지금은 있는 지표를 전부 보여주고, 많으면 metric 필터로
# 좁힌다 — 이 카드는 YAML 이 바뀌어도 다시 밀어넣을 필요가 없다.
CARDS['6. 세션별 지표'] = """
SELECT CASE WHEN INSTR(session_id, '_') > 1
            THEN SUBSTR(session_id, 1, INSTR(session_id, '_') - 1)
            ELSE session_id END
         || ' · ' || player_id  AS "세션 · 플레이어",
       metric_name              AS "지표",
       ROUND(value, 2)          AS "값"
FROM v_metrics_session
WHERE 1 = 1
  [[AND session_id = {{session_id}}]]
  [[AND metric_name = {{metric}}]]
ORDER BY 2, 1
"""

CARDS['7. 전투 HP 추이'] = """
WITH battle AS (
  SELECT e.session_id, e.player_id, e.seq AS start_seq,
         (SELECT text_value FROM event_params_merged m
           WHERE m.session_id = e.session_id AND m.player_id = e.player_id
             AND m.seq = e.seq AND m.key = 'monster_name') AS monster
  FROM events e
  WHERE e.name = 'battle_started'
)
SELECT CAST(tn.num_value AS INTEGER) AS "턴",
       ph.num_value                  AS "플레이어 HP",
       mh.num_value                  AS "몬스터 HP"
FROM events e
JOIN battle b
  ON b.session_id = e.session_id AND b.player_id = e.player_id
 AND b.start_seq = (SELECT MAX(b2.start_seq) FROM battle b2
                     WHERE b2.session_id = e.session_id
                       AND b2.player_id  = e.player_id
                       AND b2.start_seq  < e.seq)
JOIN event_params_merged tn ON tn.session_id=e.session_id AND tn.player_id=e.player_id AND tn.seq=e.seq AND tn.key='turn_no'
JOIN event_params_merged ph ON ph.session_id=e.session_id AND ph.player_id=e.player_id AND ph.seq=e.seq AND ph.key='player_hp'
JOIN event_params_merged mh ON mh.session_id=e.session_id AND mh.player_id=e.player_id AND mh.seq=e.seq AND mh.key='monster_hp'
WHERE e.name = 'turn_started'
  AND b.monster = {{monster}}
  [[AND e.session_id = {{session_id}}]]
ORDER BY e.seq
"""

CARDS['8. 파티 지출 기여도'] = """
SELECT CASE WHEN INSTR(l.session_id, '_') > 1
            THEN SUBSTR(l.session_id, 1, INSTR(l.session_id, '_') - 1)
            ELSE l.session_id END
         || ' · ' || l.recorded_by          AS "세션 · 플레이어",
       -SUM(l.delta)                        AS "지출",
       ROUND(-SUM(l.delta) * 100.0 /
             (SELECT -SUM(l2.delta) FROM v_party_ledger l2
               WHERE l2.session_id  = l.session_id
                 AND l2.resource_id = 'gold'
                 AND l2.delta < 0), 1)      AS "파티 지출 비중(%)"
FROM v_party_ledger l
WHERE l.resource_id = 'gold' AND l.delta < 0
  [[AND l.session_id = {{session_id}}]]
GROUP BY l.session_id, l.recorded_by
ORDER BY l.session_id, 2 DESC
"""


# 필수 변수의 표본값. 카드를 밀어넣기 전에 로컬에서 돌려볼 때 쓴다.
# 선택 변수(session_id)는 일부러 넣지 않는다 — Metabase 에서 필터를 비운
# 상태와 같게 만들어, 그 상태에서도 쿼리가 성립하는지 보기 위해서다.
SAMPLE_PARAMS = {'monster': '동굴 거대 박쥐'}

# 카드를 처음 만들 때의 차트 종류. 적지 않으면 table 이다.
# 이건 "처음 만들 때의 기본값" 일 뿐이다 — 한 번 만들어진 뒤 Metabase 에서
# 모양을 바꾸면, 다시 밀어넣어도 그 모양이 유지된다(upsert_card 의 preserve_visuals).
DISPLAY = {
    '1. 행동 결정 시간': 'bar',
    '3. 재화 흐름':      'bar',
    '4. 골드 잔액 추이':  'line',
    '5. 부위별 장착':    'bar',
    '7. 전투 HP 추이':   'line',
}


def main() -> int:
    con = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else 'user_playtest.sqlite')
    scenarios = [
        ('필터 없음 (전체)', {'monster': '동굴 거대 박쥐'}),
        ('세션 = room9234_20261027', {'session_id': 'room9234_20261027',
                                      'monster': '동굴 거대 박쥐'}),
        ('세션 = room1742_20261019', {'session_id': 'room1742_20261019',
                                      'monster': '방랑 약탈자'}),
    ]
    bad = 0
    for label, params in scenarios:
        print(f'\n══ {label}')
        for name, sql in CARDS.items():
            try:
                n = len(con.execute(render(sql, params)).fetchall())
                print(f'   {name:<22} {n:>3}행')
            except Exception as e:
                print(f'   {name:<22} [오류] {e}')
                bad += 1
    print(f'\n실패 {bad}건')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())

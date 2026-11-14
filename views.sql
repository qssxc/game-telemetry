-- 기본 뷰 (SQLite). **게임을 모른다.**
--
-- 어느 게임에나 똑같이 있는 것만 둔다 — 병합을 반영한 파라미터, 이벤트를
-- JSON 으로 묶은 뷰, 지표 뷰. 이벤트 이름이나 파라미터 이름은 여기 나오지 않는다.
--
--   · 이벤트마다 하나씩 있는 뷰       viewgen.py 가 DB 를 읽어 만든다
--   · 그 게임에서만 뜻이 있는 뷰      games/<게임>/views.sql 에 손으로 쓴다
--                                    (누적 잔액, 두 이벤트를 한 축으로 묶기 등)
--
-- loader.py 가 이 파일과 게임 뷰를 한 트랜잭션으로 적용한다.
--
-- SQLite 에서 지킨 것
--   * floor() 를 쓰지 않는다. 빌드에 따라 없을 수 있어 CAST 비교로 대신한다.
--   * 상관 서브쿼리 대신 LEFT JOIN 을 쓴다.

-- 병합 결과를 반영한 파라미터. 아래 뷰들은 전부 이것을 본다.
--
-- merge.py 가 계산한 값(merge_overrides)을 원본 위에 덮어 보여준다. 원본
-- event_params 는 그대로 남아 있어, 게임이 실제로 뭘 기록했는지 언제든 볼 수 있다.
-- merge.py 를 돌리지 않았으면 merge_overrides 가 비어 있어 원본과 같다.
DROP VIEW IF EXISTS event_params_merged;
CREATE VIEW event_params_merged AS
SELECT
    p.session_id,
    p.player_id,
    p.seq,
    p.key,
    COALESCE(o.num_value,  p.num_value)  AS num_value,
    COALESCE(o.text_value, p.text_value) AS text_value,
    p.bool_value
FROM event_params p
LEFT JOIN merge_overrides o
       ON o.session_id = p.session_id
      AND o.player_id  = p.player_id
      AND o.seq        = p.seq
      AND o.key        = p.key;


-- 파라미터를 JSON 객체로 묶은 이벤트. 임의 조회에 쓴다.
--     SELECT * FROM event_flat WHERE json_extract(params, '$.item_id') = 19;
DROP VIEW IF EXISTS event_flat;
CREATE VIEW event_flat AS
SELECT
    e.session_id,
    e.player_id,
    e.seq,
    e.t_ms,
    e.wall_clock,
    e.name,
    e.tag,
    e.tier,
    json_group_object(
        COALESCE(p.key, ''),
        COALESCE(
            -- 정수인 값은 1.0 이 아니라 1 로 보이게 한다
            CASE WHEN p.num_value = CAST(p.num_value AS INTEGER)
                 THEN CAST(p.num_value AS INTEGER)
                 ELSE p.num_value END,
            p.text_value,
            p.bool_value
        )
    ) AS params
FROM events e
LEFT JOIN event_params_merged p
       ON p.session_id = e.session_id
      AND p.player_id  = e.player_id
      AND p.seq        = e.seq
GROUP BY e.session_id, e.player_id, e.seq, e.t_ms, e.wall_clock,
         e.name, e.tag, e.tier;


-- 지표를 세션 정보와 붙인 뷰. BI 도구에 그대로 붙이기 좋다.
DROP VIEW IF EXISTS v_metrics_session;
CREATE VIEW v_metrics_session AS
SELECT
    m.session_id,
    m.player_id,
    s.mode,
    s.is_host,
    s.started_at,
    s.duration_ms,
    m.metric_name,
    m.value
FROM metric_results m
LEFT JOIN sessions s ON s.session_id = m.session_id
                    AND s.player_id  = m.player_id
WHERE m.scope = 'session' AND m.dimension_key = '';


-- 축으로 쪼갠 지표. 축 이름이 칼럼이 아니라 값으로 들어 있어, 축을
-- 추가해도 이 뷰를 고칠 필요가 없다.
DROP VIEW IF EXISTS v_metrics_dimension;
CREATE VIEW v_metrics_dimension AS
SELECT
    m.session_id,
    m.player_id,
    m.metric_name,
    m.dimension_key,
    m.dimension_value,
    m.value
FROM metric_results m
WHERE m.dimension_key != '';


-- 파티 범위 지표 (scope: party). 세션 하나당 한 행이고 player_id 는 없다.
--
-- v_metrics_session 과 따로 두는 이유 — 섞으면 "플레이어별 합계" 를 낼 때
-- 파티 행이 한 번 더 더해진다. 같은 지갑이 두 번 세어지는 것이고, 이건
-- 이 프로젝트에서 이미 한 번 밟은 함정이다(병합 전 -3,500 vs 실제 -1,750).
DROP VIEW IF EXISTS v_metrics_party;
CREATE VIEW v_metrics_party AS
SELECT
    m.session_id,
    m.metric_name,
    m.value,
    (SELECT COUNT(*) FROM sessions s WHERE s.session_id = m.session_id) AS party_size
FROM metric_results m
WHERE m.scope = 'party' AND m.dimension_key = '';


-- 전역 지표 한 눈에.
DROP VIEW IF EXISTS v_metrics_global;
CREATE VIEW v_metrics_global AS
SELECT metric_name, value, computed_at
FROM metric_results
WHERE scope = 'global' AND dimension_key = '';

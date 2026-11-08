-- 조회용 뷰 (SQLite)
--
-- 저장은 세로(long)로 하고 조회는 뷰로 푼다. 이렇게 하면 게임에 계측을
-- 추가해도 테이블 구조는 그대로이고, 새 이벤트를 보고 싶을 때 뷰 하나만
-- 더 쓰면 된다.
--
--     sqlite3 playtest.sqlite < views.sql
--
-- Metabase 는 이 뷰들을 테이블처럼 본다. 동기화 후 그대로 끌어 쓰면 된다.
--
-- SQLite 에서 지킨 것
--   * floor() 를 쓰지 않는다. 빌드에 따라 없을 수 있어 CAST 비교로 대신한다.
--   * 상관 서브쿼리 대신 LEFT JOIN 을 쓴다. 파라미터마다 조인이 하나씩
--     늘지만 ix_params_lookup 인덱스를 타므로 이벤트 수에 선형이다.

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


-- 구매 내역. price 가 -1 이면 가격을 알 수 없다는 표시다.
DROP VIEW IF EXISTS v_purchases;
CREATE VIEW v_purchases AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms, e.wall_clock,
    CAST(i.num_value AS INTEGER) AS item_id,
    CAST(a.num_value AS INTEGER) AS amount,
    CAST(c.num_value AS INTEGER) AS price
FROM events e
LEFT JOIN event_params_merged i ON i.session_id = e.session_id
                        AND i.player_id = e.player_id
                        AND i.seq = e.seq AND i.key = 'item_id'
LEFT JOIN event_params_merged a ON a.session_id = e.session_id
                        AND a.player_id = e.player_id
                        AND a.seq = e.seq AND a.key = 'amount'
LEFT JOIN event_params_merged c ON c.session_id = e.session_id
                        AND c.player_id = e.player_id
                        AND c.seq = e.seq AND c.key = 'price'
WHERE e.name = 'item_purchased';


-- 장착 내역.
--
-- ⚠ slot_index 를 분석에 쓰지 말 것. 이 값은 "클라이언트가 요청한 슬롯" 이고
--   실제로 어느 슬롯에 들어갔는지가 아니다. 인벤토리에서 장착하는 경로가
--   항상 0 을 보내기 때문에(InventoryItemSlotUI) 전부 0 으로 기록되지만,
--   서버는 빈 슬롯을 찾아 0번·1번에 번갈아 넣는다. 기록과 실제가 다르다.
--
--   부위는 category 로 본다. 실제 슬롯 배치가 필요해지면 요청 시점이 아니라
--   인벤토리 동기화(서버가 보낸 실제 배치)에서 따로 기록해야 한다.
DROP VIEW IF EXISTS v_equips;
CREATE VIEW v_equips AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms,
    CAST(i.num_value AS INTEGER) AS item_id,
    CAST(s.num_value AS INTEGER) AS slot_index,
    g.text_value                 AS category
FROM events e
LEFT JOIN event_params_merged i ON i.session_id = e.session_id
                        AND i.player_id = e.player_id
                        AND i.seq = e.seq AND i.key = 'item_id'
LEFT JOIN event_params_merged s ON s.session_id = e.session_id
                        AND s.player_id = e.player_id
                        AND s.seq = e.seq AND s.key = 'slot_index'
LEFT JOIN event_params_merged g ON g.session_id = e.session_id
                        AND g.player_id = e.player_id
                        AND g.seq = e.seq AND g.key = 'category'
WHERE e.name = 'item_equipped';


-- 재화 원장. 변동량과 사유를 그대로 남겼으므로 사유별 기여를 분리할 수 있다.
-- 잔액은 변동량을 누적해 복원한다 — 잔액을 기록하지 않아도 되는 이유다.
DROP VIEW IF EXISTS v_resource_ledger;
CREATE VIEW v_resource_ledger AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms, e.wall_clock,
    e.canonical, e.dup_group,
    r.text_value                 AS resource_id,
    CAST(d.num_value AS INTEGER) AS delta,
    n.text_value                 AS reason,
    CAST(b.num_value AS INTEGER) AS balance_reported,
    SUM(CAST(d.num_value AS INTEGER)) OVER (
        PARTITION BY e.session_id, e.player_id, r.text_value
        ORDER BY e.seq
    )                            AS cumulative_delta
FROM events e
LEFT JOIN event_params_merged r ON r.session_id = e.session_id
                        AND r.player_id = e.player_id
                        AND r.seq = e.seq AND r.key = 'resource_id'
LEFT JOIN event_params_merged d ON d.session_id = e.session_id
                        AND d.player_id = e.player_id
                        AND d.seq = e.seq AND d.key = 'delta'
LEFT JOIN event_params_merged n ON n.session_id = e.session_id
                        AND n.player_id = e.player_id
                        AND n.seq = e.seq AND n.key = 'reason'
LEFT JOIN event_params_merged b ON b.session_id = e.session_id
                        AND b.player_id = e.player_id
                        AND b.seq = e.seq AND b.key = 'balance'
WHERE e.name = 'resource_changed';


-- 전투 결과. D 단계(BattleManager 계측)를 적용하면 채워진다.
DROP VIEW IF EXISTS v_battles;
CREATE VIEW v_battles AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms,
    CAST(m.num_value AS INTEGER) AS monster_id,
    v.bool_value                 AS is_victory,
    CAST(t.num_value AS INTEGER) AS turn_count,
    CAST(x.num_value AS INTEGER) AS elapsed_ms,
    h.num_value                  AS player_hp_remaining
FROM events e
LEFT JOIN event_params_merged m ON m.session_id = e.session_id
                        AND m.player_id = e.player_id
                        AND m.seq = e.seq AND m.key = 'monster_id'
LEFT JOIN event_params_merged v ON v.session_id = e.session_id
                        AND v.player_id = e.player_id
                        AND v.seq = e.seq AND v.key = 'is_victory'
LEFT JOIN event_params_merged t ON t.session_id = e.session_id
                        AND t.player_id = e.player_id
                        AND t.seq = e.seq AND t.key = 'turn_count'
LEFT JOIN event_params_merged x ON x.session_id = e.session_id
                        AND x.player_id = e.player_id
                        AND x.seq = e.seq AND x.key = 'elapsed_ms'
LEFT JOIN event_params_merged h ON h.session_id = e.session_id
                        AND h.player_id = e.player_id
                        AND h.seq = e.seq AND h.key = 'player_hp_remaining'
WHERE e.name = 'battle_ended';


-- 행동 선택. decision_ms 가 이 도구의 간판 지표다.
DROP VIEW IF EXISTS v_actions;
CREATE VIEW v_actions AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms,
    a.text_value                 AS action,
    CAST(s.num_value AS INTEGER) AS skill_id,
    CAST(t.num_value AS INTEGER) AS turn_no,
    CAST(d.num_value AS INTEGER) AS decision_ms
FROM events e
LEFT JOIN event_params_merged a ON a.session_id = e.session_id
                        AND a.player_id = e.player_id
                        AND a.seq = e.seq AND a.key = 'action'
LEFT JOIN event_params_merged s ON s.session_id = e.session_id
                        AND s.player_id = e.player_id
                        AND s.seq = e.seq AND s.key = 'skill_id'
LEFT JOIN event_params_merged t ON t.session_id = e.session_id
                        AND t.player_id = e.player_id
                        AND t.seq = e.seq AND t.key = 'turn_no'
LEFT JOIN event_params_merged d ON d.session_id = e.session_id
                        AND d.player_id = e.player_id
                        AND d.seq = e.seq AND d.key = 'decision_ms'
WHERE e.name = 'player_action';


-- 주고받은 피해. was_defending 으로 갈라 보면 방어 버튼의 값어치가 나온다.
DROP VIEW IF EXISTS v_damage;
CREATE VIEW v_damage AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms,
    CASE WHEN e.name = 'damage_dealt' THEN 'dealt' ELSE 'taken' END AS direction,
    s.text_value                 AS source,
    CAST(k.num_value AS INTEGER) AS skill_id,
    g.num_value                  AS damage,
    c.bool_value                 AS is_critical,
    f.bool_value                 AS was_defending,
    CAST(t.num_value AS INTEGER) AS turn_no
FROM events e
LEFT JOIN event_params_merged s ON s.session_id = e.session_id
                        AND s.player_id = e.player_id
                        AND s.seq = e.seq AND s.key = 'source'
LEFT JOIN event_params_merged k ON k.session_id = e.session_id
                        AND k.player_id = e.player_id
                        AND k.seq = e.seq AND k.key = 'skill_id'
LEFT JOIN event_params_merged g ON g.session_id = e.session_id
                        AND g.player_id = e.player_id
                        AND g.seq = e.seq AND g.key = 'damage'
LEFT JOIN event_params_merged c ON c.session_id = e.session_id
                        AND c.player_id = e.player_id
                        AND c.seq = e.seq AND c.key = 'is_critical'
LEFT JOIN event_params_merged f ON f.session_id = e.session_id
                        AND f.player_id = e.player_id
                        AND f.seq = e.seq AND f.key = 'was_defending'
LEFT JOIN event_params_merged t ON t.session_id = e.session_id
                        AND t.player_id = e.player_id
                        AND t.seq = e.seq AND t.key = 'turn_no'
WHERE e.name IN ('damage_dealt', 'damage_taken');


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


-- 파티 단위 재화 원장. 중복을 걷어낸 canonical 행만 본다.
--
-- 협동 세션에서 골드는 파티가 공유하므로, 한 명이 쓴 돈이 모든 참가자
-- 파일에 기록된다. 두 파일을 그냥 합치면 지출이 정확히 2배가 된다
-- (실측: 단순 합계 -3,500 / 실제 -1,750). 이 뷰는 그걸 걷어낸다.
--
-- 클라이언트별로 보고 싶으면 v_resource_ledger 를 canonical 조건 없이 쓴다.
DROP VIEW IF EXISTS v_party_ledger;
CREATE VIEW v_party_ledger AS
SELECT
    session_id, wall_clock, player_id AS recorded_by,
    resource_id, delta, reason, balance_reported
FROM v_resource_ledger
WHERE canonical = 1;

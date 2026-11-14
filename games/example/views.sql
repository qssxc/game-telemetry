-- 게임 전용 뷰의 틀. **선택 파일이다 — 필요 없으면 지운다.**
--
-- 대부분의 조회는 이 파일 없이 된다. viewgen.py 가 이벤트마다 뷰를 하나씩
-- (v_<이벤트이름>) 만들어 주기 때문이다. 여기에는 기계가 만들 수 없는 것만
-- 쓴다 — 누적 합계, 두 이벤트를 한 축으로 묶기처럼 "무엇을 보고 싶은지" 를
-- 사람이 정해야 하는 뷰.
--
-- 지킬 것
--   * DROP VIEW IF EXISTS 를 앞에 둔다. 매번 다시 적용된다.
--   * 파라미터는 event_params 가 아니라 event_params_merged 에서 읽는다.
--     (merge 가 보정한 값이 반영된 뷰다. 기본 뷰에 있다)
--   * `WHERE e.name = '이벤트'` 로 쓴다. viewgen.py 가 이 모양을 읽어 그
--     이벤트의 자동 뷰를 건너뛴다. 별칭이 e 가 아니면 못 읽는다.
--   * BEGIN / COMMIT 을 쓰지 않는다. loader.py 가 통째로 한 트랜잭션에 넣는다.
--
-- 아래는 예시다. 이벤트·파라미터 이름을 자기 게임 것으로 바꾼다.

-- 코인 원장. 변동량을 누적해 잔액을 복원한다.
DROP VIEW IF EXISTS v_coin_ledger;
CREATE VIEW v_coin_ledger AS
SELECT
    e.session_id, e.player_id, e.seq, e.t_ms, e.wall_clock, e.canonical,
    c.text_value                 AS currency,
    CAST(d.num_value AS INTEGER) AS delta,
    SUM(CAST(d.num_value AS INTEGER)) OVER (
        PARTITION BY e.session_id, e.player_id, c.text_value
        ORDER BY e.seq
    )                            AS cumulative_delta
FROM events e
LEFT JOIN event_params_merged c ON c.session_id = e.session_id
                               AND c.player_id  = e.player_id
                               AND c.seq = e.seq AND c.key = 'currency'
LEFT JOIN event_params_merged d ON d.session_id = e.session_id
                               AND d.player_id  = e.player_id
                               AND d.seq = e.seq AND d.key = 'delta'
WHERE e.name = 'coins_changed';

# 변경 내역

## 0.1.0 — 2026-11-21

처음 배포하는 버전이다. 분석 도구와 수집기를 이 저장소 하나에서 설치한다.

### 설치와 명령
- 분석 도구를 패키지로 묶었다. `pip install git+https://github.com/qssxc/game-telemetry` 로 설치한다.
- `ptel` 명령: `init`, `run`, `check`, `push`, `--version`. `python -m playtest_telemetry` 로도 쓴다.
- 단계별 모듈을 `python -m playtest_telemetry.loader` · `.merge` · `.viewgen` · `.metrics` 로 돌린다.
- 수집기를 Unity 패키지로 묶었다. Package Manager 에 `https://github.com/qssxc/game-telemetry.git?path=unity` 를 넣는다. Smoke Test 를 Samples 에서 가져온다.

### 분석 도구
- 협동 세션의 `t_ms` 를 호스트의 시간축으로 맞춘다. `clock_pairs` 가 있으면 시계 오차를 재서 뺀다. `merge: { align_time: false }` 로 끈다. 내역은 `clock_alignment` 테이블에 남는다.
- 축(`by`)으로 쪼갠 지표의 전역값을 원본에서 다시 계산한다(`v_metrics_global_dimension`). `avg` 축 카드는 이 값을 보여준다.
- `scope: global` 수식이 지표당 한 행만 쓴다.
- 범위가 다른 지표를 수식에서 이름만으로 부르면 멈추고 `party.` 를 쓰라고 알려준다.
- 정의 파일의 맨 위가 목록이거나 비었거나 YAML 문법이 틀리면 읽을 수 있는 오류로 멈춘다.
- 한 참가자의 로그가 두 파일에 나뉘었거나 한 파일에 여러 세션이 섞였으면 적재를 멈춘다.
- 파라미터 이름이 SQL 예약어(`order`, `from` 등)여도 이벤트 뷰가 만들어진다. 쓰지 않게 된 옛 자동 뷰는 지운다.
- `ptel check` 의 시계 측정이 사건이 여러 번 있을 때도 맞는다.

### 수집기
- 세션 id 에 영문·숫자가 아닌 글자가 있으면 파일 이름에 해시를 붙인다. 서로 다른 판이 한 파일에 섞이지 않는다.
- 재접속해 같은 파일에 이어 쓸 때 `t_ms` 도 이어 간다.
- `uint` · `ulong` · `ushort` · `sbyte` · `decimal` 을 JSON 숫자로 기록한다.

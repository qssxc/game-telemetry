# 분석 파이프라인

게임이 남긴 NDJSON 을 받아 질의 가능한 형태로 만들고, 정의 파일에 적힌
파생지표를 계산한다. 게임을 모른다 — 게임 고유 지식은 전부 정의 파일에 있다.

```
세션 로그(.ndjson)          참가자마다 파일 하나
      ↓  loader.py          적재 + 뷰 → sessions / events / event_params
      ↓  merge.py           협동 병합  → canonical, merge_overrides
      ↓  metrics.py         지표 계산  → metric_results
      ↓  metabase_api.py    카드 생성/갱신 → Metabase
```

## 어디까지가 게임과 무관한가

새 게임에 붙일 때 **손대지 않는 파일**과 **그 게임을 위해 쓰는 파일**이 갈린다.

| 중립 — 그대로 쓴다 | 게임 전용 — 새로 쓴다 |
|---|---|
| `loader.py` `metrics.py` `viewgen.py` | 지표 정의 YAML (`game.yaml`) |
| `cardgen.py` `metabase_api.py` `merge.py` | 그 YAML 의 `merge:` 섹션 (협동 게임만) |
| `views.sql` 의 기본 뷰·지표 뷰 층 | `views.sql` 의 게임 전용 뷰 층 |
| | `cards.py` (손으로 쓴 카드, **선택**) |

> 어떤 이벤트가 협동 세션에서 참가자마다 복제되는지는 게임마다 다르다.
> 그 지식은 `merge.py` 코드가 아니라 **정의 파일의 `merge:` 섹션**에 적는다
> ([아래](#협동-세션-병합-mergepy)). 1인 게임은 섹션을 쓰지 않으면 된다.
> 2인 이상 세션이 있는데 섹션이 없으면 `[경고]` 가 뜬다 — 그대로 두면
> 공유 자원이 참가자 수만큼 부풀려진 숫자가 오류 없이 나오기 때문이다.

**새 게임을 붙이는 데 필요한 최소한은 지표 정의 YAML 하나다.** 중립 파일만
가져다 놓고 YAML 을 쓰면 적재부터 Metabase 카드까지 간다. `cards.py` 는 그
게임에서만 뜻이 있는 카드("전투 HP 추이" 같은)를 나중에 손으로 더할 때 쓴다.

레이싱 게임 로그(세션 id `gp-tokyo-01`, 이벤트 `lap_completed`)로 확인했다.
중립 파일만으로 `v_lap_completed` `v_pit_stop` `v_crash` 뷰와 카드 5장까지
코드 수정 없이 갔다.

### 이벤트별 뷰는 만들어진다 (`viewgen.py`)

이벤트 하나당 뷰 하나를 **DB 를 읽어서** 써낸다. `merge.py` 뒤에 돌린다.

### 중복 복제는 뷰가 막는다

협동 세션에서 공유 자원과 브로드캐스트 이벤트는 참가자 파일마다 복제된다.
거르지 않고 합치면 **참가자 수만큼 부풀려진다.** 실측으로 골드 -9,650 vs
실제 -7,900, 몬스터 처치 2건 vs 1건이었다. 오류는 나지 않고 숫자만 틀리며,
**1인 세션에서는 멀쩡하다.** 조회하는 사람이 매번 기억할 종류의 규칙이 아니다.

그래서 `canonical = 0` 인 행이 실제로 있는 이벤트는 걸러진 뷰를 기본으로
만들고, 거르지 않은 전부는 `_all` 뷰로 따로 낸다.

```
v_monster_killed       1건   ← canonical = 1 만. 기본으로 이걸 쓴다
v_monster_killed_all   2건   ← 원본. 병합이 제대로 됐는지 볼 때
```

중복이 없는 이벤트에는 필터도 `_all` 도 붙지 않는다. 1인 게임이면 아무것도
달라지지 않는다.

**이 판단에 이벤트 이름이 쓰이지 않는다.** 어떤 이벤트가 복제되는지는
정의 파일의 `merge:` 섹션이 알고 있고, `viewgen.py` 는 그 결과(`canonical` 칼럼)만 본다.
그래서 설정이 없는 게임에서도 맞게 동작한다 — 중복 판정이 안 되면
`canonical = 0` 이 없고, 필터도 안 붙는다.

칼럼 이름은 그 이벤트가 실제로 가진 파라미터에서, 타입은 실제로 들어 있는
값에서 알아낸다. 소수부가 있는 행이 하나라도 있으면 REAL, 없으면 INTEGER 로
자른다 — `damage`(33.04)와 `turn_count`(19)를 가르는 지점이고, 손으로 쓸 때
가장 자주 틀리는 곳이다.

계산이 들어간 뷰는 만들지 못한다. 누적 잔액(`v_resource_ledger`), 중복 제거
(`v_party_ledger`), 두 이벤트를 한 축으로 묶기(`v_damage`) 는 무엇을 보고
싶은지 사람이 정해야 한다. 그런 뷰는 `views.sql` 에 두면 생성기가 비켜 간다
(`WHERE e.name = ...` 을 읽어 해당 이벤트를 건너뛴다).

따로 돌리거나 파일로 받고 싶으면

```bash
# 파일로 받기 (views_generated.sql)
python viewgen.py --db playtest.sqlite --skip-existing views.sql

# 파일 없이 DB 에 바로
python viewgen.py --db playtest.sqlite --skip-existing views.sql --apply --no-file
```

`--skip-existing` 을 빼면 손으로 쓴 뷰가 다루는 이벤트까지 만든다. 이름이
달라서(`v_battles` vs `v_battle_ended`) 덮어쓰지는 않지만 같은 데이터를 보는
뷰가 둘이 된다.

## 저장소를 SQLite 로 한 이유

후보는 SQLite 와 DuckDB 였다. 분석 질의만 보면 DuckDB 가 유리하지만
**Metabase 연결 단계에서 결정이 갈린다.**

| | SQLite | DuckDB |
|---|---|---|
| Metabase 드라이버 | **공식 내장** | 커뮤니티 `.jar` 수동 설치 |
| 버전 올라갈 때 | 그대로 동작 | 드라이버가 깨질 수 있다 |
| 동시 접근 | 읽기 다중 가능 | 쓰기 중 한 프로세스만 |

마지막 줄이 특히 문제다. DuckDB 는 Metabase 가 파일을 붙들고 있으면
적재기가 쓰려다 막힌다. 플레이테스트를 받으면서 대시보드를 켜 둘 수 없다.

성능 걱정은 재 봤다(`bench.py`). 합성 데이터 **세션 300개 × 이벤트 1,000건**
(이벤트 30만, 파라미터 164만 행, 216MB) 에서

| 작업 | 시간 |
|---|---|
| 인덱스 생성 | 3.8초 |
| 지표 30개 전체 계산 | 4.7초 |
| 대시보드 질의 (승률, 결정시간, 방어효과 등) | 155~404ms |

플레이테스트 현실 규모는 이보다 작다. 테스터 50명 × 3세션 × 700이벤트 ≈
이벤트 10만 건이다. 여기서는 성능이 선택 기준이 되지 않는다.

가장 느린 질의는 `v_resource_ledger` (LEFT JOIN 4개, 404ms) 다. 더 빠르게
해야 하면 `event_params` 에 값까지 포함한 커버링 인덱스를 추가하면 되지만,
인덱스 크기가 두 배가 되므로 지금은 하지 않았다.

## 실행

```bash
pip install pyyaml          # 이게 전부다. sqlite3 는 파이썬에 내장되어 있다.

# 한 명령으로 전부
python pipeline.py <로그폴더> --reset --push
```

단계를 따로 돌리고 싶으면 아래처럼 한다. **순서가 중요하다.**

```bash
# 1. 적재 (기본 뷰까지 만든다)
python loader.py <로그폴더> --db playtest.sqlite --reset

# 2. 협동 세션 병합 (1인 세션만 있어도 돌려두면 된다 — 아무것도 바뀌지 않는다)
python merge.py game.yaml --db playtest.sqlite --show

# 3. 이벤트마다 뷰 생성 — 반드시 2번 뒤에
python viewgen.py --db playtest.sqlite --skip-existing views.sql --apply --no-file

# 4. 지표 계산
python metrics.py game.yaml --db playtest.sqlite --show

# 5. Metabase 카드 생성/갱신 (지표마다 카드 한 장까지 원하면 --from-yaml)
python metabase_api.py --db-file playtest.sqlite --push --from-yaml game.yaml
```

**3번이 2번 뒤인 것이 중요하다.** 협동 세션에서 참가자마다 복제되는 이벤트를
걸러낸 뷰를 만들려면 `merge.py` 가 먼저 판정을 끝내야 한다. 적재 직후에 돌리면
`canonical` 이 전부 1 이라 필터가 붙지 않는다. `pipeline.py` 는 이 순서를
한 곳에 박아둔 것이다.

```
$ python pipeline.py testlogs --reset --push

[1/5] 적재 — testlogs → playtest.sqlite
      0.4초 — 파일 14개, 이벤트 600건
[2/5] 협동 세션 병합
      0.1초 — 중복 17행 제외 (3종 이벤트)
[3/5] 이벤트 뷰 생성
      0.1초 — 뷰 26개 (중복 이벤트 2종은 canonical=1 만 본다: gold_observed, monster_killed)
[4/5] 지표 계산 — game.yaml
      0.2초 — metric_results 327행
[5/5] Metabase 카드 반영
      1.1초 — 카드 반영 완료
```

단계별 상세 출력은 기본으로 접어둔다. 경고와 오류는 접히지 않고, 전부 보려면
`-v` 를 붙인다. 한 단계가 실패하면 그 단계의 출력을 전부 펴서 보여주고 멈춘다.

`--skip-load` 는 적재를 건너뛰고 뒷단계만 다시 돌린다. 지표 정의만 고쳤을 때
쓴다. 30만 이벤트(세션 50개 × 4인) 기준 전체 23.6초, 적재를 건너뛰면 9.6초다.

### 돌아가는 동안 대시보드에 보이는 것

각 단계는 그 자체로 원자적이다 — 적재는 전부 아니면 전무이고, 지표는 지웠다
다시 쓰는 것이 한 트랜잭션이라 비어 있는 중간 상태가 보이지 않는다. 지표
정의가 틀리면 아무것도 쓰지 않고 직전 값이 남는다.

다만 **단계 사이**는 커밋된다. 돌아가는 동안 대시보드를 켜 두면 몇 초간
"새 이벤트 + 옛 지표" 같은 상태가 보인다. 전부를 한 트랜잭션으로 묶으려면
`merge.py` 와 `metrics.py` 가 자기 커밋을 하지 않도록 고쳐야 하는데, 지금
규모에서는 그만한 값어치가 없다고 보고 두었다.

4번은 `METABASE_URL` 과 `METABASE_API_KEY` 환경변수를 읽는다. PowerShell 에는
`export` 가 없으니 `$env:METABASE_URL = "http://localhost:3000"` 으로 설정한다.
자세한 것은 `metabase_api.py` 머리말과 `METABASE.md` 에 있다.

2인 세션을 처음 받았다면 병합 전에 `merge_check.py` 로 상태를 먼저 본다.
정의 파일 없이 돌리면 두 파일에 같은 내용으로 들어 있는 이벤트를 **중복
후보**로 짚어준다. `merge:` 규칙을 처음 쓸 때 여기서 시작한다.

```bash
python merge_check.py --db playtest.sqlite                 # 후보 찾기
python merge_check.py game.yaml --db playtest.sqlite  # 규칙이 다 덮는지 확인
```

`views.sql` 만 고쳤을 때는 다시 적재할 필요 없이

```bash
python loader.py --views-only --db playtest.sqlite
```

### `sqlite3 ... < views.sql` 을 쓰지 않는 이유

PowerShell 은 `<` 리다이렉션을 지원하지 않는다. 예약어로 잡아두고 구현하지
않아서, 명령이 실행되기 전 파싱 단계에서 막힌다.

```
'<' 연산자는 나중에 사용하도록 예약되어 있습니다.
```

`Get-Content .\views.sql | sqlite3 .\playtest.sqlite` 로 우회할 수는 있지만,
Windows PowerShell 5.1 은 파이프로 넘길 때 기본 출력 인코딩이 ASCII 라
`views.sql` 안의 한글 주석이 깨진다.

그래서 `loader.py` 가 파일을 UTF-8 로 직접 읽어 적용한다. 리다이렉션도,
인코딩도, `sqlite3.exe` 설치 여부도 문제가 되지 않는다.

sqlite3 명령줄 도구로 직접 하고 싶다면 자체 내장 명령을 쓰면 된다.

```powershell
sqlite3 .\playtest.sqlite ".read views.sql"
```

로그 폴더는 플레이테스터들의 파일을 한곳에 모아둔 곳이다. Windows 기준
각 PC 의 다음 위치에 생긴다.

```
%USERPROFILE%\AppData\LocalLow\<회사명>\<게임명>\telemetry\
```

### Metabase 연결

관리자 → 데이터베이스 → 데이터베이스 추가 → **SQLite** → 파일 경로 입력.

Metabase 를 Docker 로 돌리면 `.sqlite` 파일이 컨테이너 안에서 보여야 한다.
볼륨으로 마운트하고, 컨테이너 기준 경로를 넣는다.

```bash
docker run -d -p 3000:3000 \
  -v /host/path/to/data:/data \
  --name metabase metabase/metabase
# Metabase 에 입력할 경로: /data/playtest.sqlite
```

연결 후 **동기화**를 돌리면 뷰도 테이블처럼 잡힌다.

카드는 손으로 만들지 않는다. `metabase_api.py --push` 가 `cardgen.py` 로
정의 파일에서 생성한 카드와 (있으면) `cards.py` 의 게임 전용 카드를 만들고, 다시 돌리면 **지우지 않고 갱신한다** —
카드 id 가 유지돼야 대시보드에 올려둔 것이 떨어지지 않는다. 사람이 Metabase
에서 바꾼 차트 모양도 덮어쓰지 않는다. 보내기 전에 로컬 SQLite 로 쿼리를
먼저 돌려보고, 깨진 것이 하나라도 있으면 아무것도 올리지 않는다.

## 저장 구조

파라미터를 **세로(long)** 로 저장한다.

| 테이블 | 내용 |
|---|---|
| `sessions` | 세션 한 건 = 한 행. 모드, 호스트 여부, 길이, 폐기 건수 |
| `events` | 이벤트 봉투. 시간, 이름, 태그, 등급 |
| `event_params` | 파라미터 한 개 = 한 행. `num_value` / `text_value` / `bool_value` |
| `metric_results` | 계산된 지표. 축은 `dimension_key` / `dimension_value` |

가로로 펼친 테이블이면 게임에 이벤트를 하나 추가할 때마다 스키마를
바꿔야 한다. 그러면 "수집을 늘려도 도구는 그대로" 라는 전제가 깨진다.
조회 불편은 `views.sql` 의 뷰로 해결한다.

`metric_results` 의 축도 같은 이유로 칼럼이 아니라 값이다. 아이템별에서
몬스터별로 축을 바꾸거나 새 축을 추가할 때 테이블을 건드리지 않는다.

칼럼의 선언 타입(`TIMESTAMP`, `BOOLEAN`)은 SQLite 가 거의 무시하지만
**Metabase 가 칼럼 종류를 판별할 때 이 선언을 읽는다.** 그래서 전부
`TEXT` 로 뭉개지 않고 의미에 맞게 선언했다.

## 정의 파일

`game.yaml` 이 "무엇을 지표로 볼지" 를 정한다. 게임에는 들어가지 않는다.
저장소에는 틀인 `game.example.yaml` 만 있다 — 복사해서 자기 게임에 맞게 고친다.
`game.yaml` 은 `.gitignore` 에 있어 올라가지 않는다. 다른 이름을 쓰려면
`pipeline.py --yaml 이름.yaml` 로 지정한다.

```bash
cp game.example.yaml game.yaml      # PowerShell: Copy-Item game.example.yaml game.yaml
```

고치고 `metrics.py` 를 다시 돌리면 새 지표가 나온다. **게임을 다시 빌드할
필요가 없다** — 이게 이 구조의 요점이다.

```yaml
- name: battle_count
  type: count
  event: battle_ended

- name: battle_won
  type: count
  event: battle_ended
  where:
    is_victory: true

- name: win_rate
  type: formula
  formula: battle_won / battle_count
```

| 종류 | 설명 |
|---|---|
| `count` | 조건에 맞는 이벤트 수 |
| `sum` `avg` `min` `max` | 이벤트의 `param` 집계 |
| `formula` | 앞서 선언한 지표들로 만드는 수식 (`+ - * /`) |

보조 항목

- `by` — 그 파라미터를 축으로 쪼갠다 (아이템별, 카테고리별 ...)
- `where` — 파라미터 조건. 연산자 `eq ne lt lte gt gte`
- `default` — 해당 이벤트가 0건일 때 채울 값
- `scope` — 묶는 단위. `session`(기본) `party` `global`

여기 없는 키를 적으면 **멈추고 알려준다.** 조용히 무시하면 "고쳤는데 값이
안 바뀐다" 가 되고, 그걸 사람이 알아차릴 방법이 없다.

### 범위 (`scope`)

| 값 | 묶는 단위 | 결과의 키 |
|---|---|---|
| `session` | 세션 × 플레이어 | `session_id` ○ `player_id` ○ |
| `party` | 세션 (참가자를 합침) | `session_id` ○ `player_id` NULL |
| `global` | 전부 | 둘 다 NULL |

`party` 값은 세션 결과를 더하지 않고 **원본에서 다시 집계한다.** 합계는
어느 쪽이든 같지만 평균이 다르다 — 참가자마다 표본 수가 달라, 세션 평균을
다시 평균내면 적게 산 사람과 많이 산 사람이 같은 무게를 갖는다.

수식에서 `party.지표이름` 으로 같은 세션의 파티 값을 가져올 수 있다.
협동 게임에서 묻고 싶은 것은 대개 "파티 전체 중 내 몫" 의 모양이다.

```yaml
- name: party_gold_spent      # 파티 전체 지출
  scope: party
  type: sum
  event: resource_changed
  param: delta
  where: { resource_id: gold, delta: { lt: 0 } }

- name: my_spend_share        # 내 지출 ÷ 같은 세션의 파티 지출
  type: formula
  formula: gold_spent / party.party_gold_spent
```

## 설계상 결정한 것들

### 수식에 `eval()` 을 쓰지 않는다

정의 파일은 사람이 손으로 쓰는 설정이다. 거기에 임의의 파이썬이 실행되면
안 된다. `ast` 로 파싱해 사칙연산과 지표 이름만 허용한다.

### "값 없음" 과 0 을 구분한다

전투를 한 번도 안 한 세션의 평균 전투 시간은 0 이 아니라 **값이 없는** 것이다.
0 으로 채우면 전체 평균이 아래로 끌려간다. 반대로 지출 합계는 거래가 없으면
0 이 맞다.

어느 쪽인지는 지표를 정의한 사람만 안다. 그래서 `default` 를 정의 파일에서
선언하게 했고, 선언하지 않으면 그 행을 적재하지 않는다. 수식도 참조 지표에
값이 없으면 결과 없음이 된다.

0 으로 나누는 경우도 같다. 결과를 0 으로 두면 "전투를 안 했다" 와
"승률이 0%" 가 구분되지 않으므로 값 없음으로 처리한다.

### 오타와 "데이터 없음" 을 구분한다

수식이 모르는 이름을 참조하면 **멈추고** 어느 이름인지 알려준다. 정의 파일의
잘못이기 때문이다. 반면 선언은 되어 있으나 그 범위에 데이터가 없는 경우는
흔한 일이므로 조용히 건너뛴다.

정의가 틀린 경우 계산 결과를 하나도 쓰지 않고 되돌린다. 절반만 계산된
결과가 남으면 대시보드가 조용히 틀린 값을 그린다.

### 깨진 줄 하나로 세션을 버리지 않는다

게임이 비정상 종료하면 마지막 줄이 잘린 채 남을 수 있다. `loader.py` 는 그
줄만 건너뛰고 경고를 출력한다. `load_log` 테이블에 건너뛴 줄 수가 남는다.

### 전역 평균은 세션 평균의 평균이 아니다

세션별 평균을 다시 평균하면 세션 크기가 반영되지 않아 값이 틀어진다.
전역값은 원본 이벤트에서 다시 계산한다.

### SQLite 에서 `floor()` 를 쓰지 않는다

`floor()` 는 SQLite 빌드 옵션에 따라 없을 수 있다. Metabase 가 들고 있는
JDBC 드라이버의 SQLite 빌드는 우리가 고를 수 없으므로,
`num_value = CAST(num_value AS INTEGER)` 비교로 대신한다.

### 인덱스는 적재가 끝난 뒤 만든다

적재 중에 인덱스를 유지하는 것보다 빠르다. `loader.py` 가 마지막에
`CREATE INDEX` 와 `ANALYZE` 를 돌린다.

## 협동 세션 병합 (merge.py)

참가자마다 파일이 하나씩 생긴다. 그냥 다 적재하면 같은 사건이 여러 번 들어간다.
특히 **파티가 공유하는 자원은 참가자 수만큼 부풀려진다** — 실측 2인 세션에서
골드 지출이 정확히 2배였다(단순 합계 -3,500 / 실제 -1,750).

`merge.py` 가 하는 일은 둘이다.

### 1. 사유 보정

인벤토리 동기화가 보상 응답보다 먼저 도착하면, 어댑터는 그 변동을 `unknown`
으로 기록한 뒤 이중 계상을 막으려고 `resource_reason_corrected` 만 남긴다.
병합 단계가 그 사유를 원래 행에 되돌려 붙인다.

### 2. 중복 판정

무엇을 중복으로 볼지는 정의 파일에 적는다. `merge.py` 는 이벤트 이름을 모른다.

```yaml
merge:
  shared_state:                        # 파티가 공유하는 상태
    resource_changed:
      key: [resource_id, delta, balance]
      only: { resource_id: [gold] }    # 선택 — 이 값일 때만 공유로 본다
    gold_observed: [balance]           # key 만 있으면 목록으로 줄여 쓴다
  broadcast:                           # 서버가 전원에게 뿌리는 알림
    monster_killed: [raw_id, monster_id]
    player_downed: [user_id]
  reason_correction:                   # 선택 — 위 "1. 사유 보정" 의 모양
    event: resource_reason_corrected
    target: resource_changed
    match: [resource_id, delta]
```

- **`key`** 에는 모든 클라이언트에서 같은 값만 넣는다. `elapsed_ms` 처럼
  클라이언트가 로컬에서 계산한 값을 넣으면 같은 사건이 갈라진다.
- **`only`** 는 한 이벤트가 공유 자원과 개인 자원을 함께 나를 때 쓴다. 없으면
  두 사람이 우연히 같은 개인 변동(둘 다 경험치 +50)을 겪었을 때 한쪽이
  지표에서 빠진다. 반대로 공유 자원을 `only` 에서 빠뜨리면 그 자원이
  부풀려진다.
- 모르는 키(`brodcast:` 같은 오타)가 있으면 멈추고 알려준다.

| 분류 | 예 | 판정 키 |
|---|---|---|
| 파티 공유 상태 | `resource_changed`, `gold_observed` | `resource_id`, `delta`, `balance` |
| 서버 브로드캐스트 | `monster_killed`, `player_downed` | 서버가 내려준 값만 |
| 각자 기록 | 전투·구매·장착 전부 | 판정하지 않는다 |

키에 `balance` 를 쓰는 이유는 서버가 보낸 공유 잔액이라 모든 클라이언트에서
같은 값이기 때문이다. 같은 키가 반복될 수 있으므로(같은 금액 거래가 여러 번),
시간 창 대신 **파일 안에서의 등장 순서**로 짝을 짓는다. 시계 오차에 끌려다니지
않는다.

남길 행은 이 순서로 고른다.

1. **사유를 아는 행** — 행위자의 클라이언트만 왜 바뀌었는지 안다
2. 호스트
3. `player_id` 순 (돌릴 때마다 같은 결과가 나오도록)

### 원본은 지우지 않는다

`events.canonical` 을 0 으로 내릴 뿐이다. 클라이언트별 관측 시각 자체가
데이터이기 때문이다 — 같은 사건을 두 PC 가 언제 봤는지의 차이로 체감 지연을
잴 수 있다.

- 파티 단위 집계 → `canonical = 1` (지표 엔진이 자동으로 건다)
- 클라이언트별 분석 → 조건 없이 전부

계산된 값은 `merge_overrides` 에 따로 쌓이고, `event_params_merged` 뷰가
원본 위에 덮어 보여준다. 원본 `event_params` 는 손대지 않는다.

### 시계 오차 측정

한 방향 관측만으로는 시계 오차와 전달 지연을 분리할 수 없다. 관측 시각 차이
= 오차 + 지연이고, 미지수가 둘인데 식이 하나다.

**양쪽이 서로를 관측한 쌍**이 있으면 풀린다. `merge_check.py` 가 이걸 한다.
실측 예:

```
p3 의 사망 → p5 가 관측: +4.894초
p5 의 사망 → p3 가 관측: +4.560초
→ 시계 오차 +167ms, 전달 지연 4.73초
```

4.7초는 게임 로직(`ReturnToMapRoutine` 의 2.5초 대기)이지 네트워크가 아니다.
한 방향만 보고 "시계가 5.6초 어긋났다" 고 판단하면 멀쩡한 PC 시계를 맞추느라
시간을 버린다.

## 알아둘 제약

### `player_id` 는 세션 안에서만 의미가 있다

`player_id` 는 `p{HostID}` 이고, ProudNet 의 HostID 는 **접속할 때마다 새로
배정되는 번호**다. 세션이 다르면 같은 `p3` 라도 같은 사람이라는 보장이 없다.

따라서 플레이어를 묶는 모든 집계는 **`session_id` 와 함께** 해야 한다.

```sql
-- 틀림: 다른 세션의 기록이 한 사람으로 합쳐진다
GROUP BY player_id

-- 맞음
GROUP BY session_id, player_id
```

`metric_results` 와 `v_metrics_session` 이 두 칼럼을 모두 들고 있는 것도 이 때문이다.

`scope = 'party'` 인 행은 `player_id` 가 NULL 이다. 특정 플레이어의 값이 아니기
때문이다. 그래서 파티 지표는 `v_metrics_session` 이 아니라 `v_metrics_party`
로 본다 — 섞으면 플레이어별 합계에 파티 행이 한 번 더 더해진다.

세션을 넘는 플레이어 추적(2회차에 더 잘했는가, 숙련되면 결정 시간이 줄어드는가)은
**현재 구조로는 불가능하다.** 영속 식별자가 필요하면 게임이 직접 넘겨야 한다.
Steam 기반 게임이라면 SteamID 를 해시해서 쓰면 되고, 지금 구조에서는
`GameTelemetry.BeginSession` 의 `playerId` 인자에 넣어주면 된다.

## 아직 안 된 것

- **영속 플레이어 식별자** — 위 제약 참조. 세션 간 비교가 필요해지면 먼저 해결해야 한다.
- **`views.sql` 의 게임 전용 층 분리** — 이벤트별 뷰는 `viewgen.py` 가 만들지만,
  손으로 쓴 게임 전용 뷰(`v_battles` 등)가 같은 파일에 있어 다른 게임에도 딸려간다.
  빈 뷰가 생길 뿐 해는 없지만, 중립 층과 갈라두는 편이 맞다.
- **시계 보정 적용** — `clock_offset_ms` 는 여전히 0 으로 기록된다.
  측정값(위 +167ms)을 `JoinSession` 의 `hostOffsetMs` 에 넣으면 `t_ms` 가 정렬된다.
- **행 단위 계산식** — `player_hp / player_max_hp` 처럼 같은 행의 두 파라미터를
  나누는 지표를 정의할 수 없다.

## 나중에 저장소를 바꿀 때

세로 저장 구조는 특정 DB 기능에 기대지 않는다. Postgres 나 MySQL 로 옮기면
`loader.py` 의 `connect` 와 `SCHEMA` 만 고치면 되고, `views.sql` 에서 손댈 곳은
`json_group_object` 를 쓴 `event_flat` 하나다.

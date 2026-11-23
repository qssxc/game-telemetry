# Playtest Telemetry 사용 가이드

버전 0.1.0 기준 · 2026-11-24 수정

> 도구를 고치거나 내부 구조가 궁금하면 [설계 노트](docs/design.md)를 본다. 무엇이 어디에 있고 왜 그렇게 만들었는지 적혀 있다.

## 1. 개요

Playtest Telemetry는 플레이테스트에서 일어난 일을 숫자로 보기 위한 도구다. 게임 안에서 사건을 파일로 남기는 **수집기**와, 그 파일을 읽어 지표를 계산하는 **분석 도구** 두 부분으로 되어 있다.

| 부분 | 하는 일 | 쓰는 곳 |
| --- | --- | --- |
| 수집기 (Unity, C#) | `Telemetry.Record(...)` 한 줄로 사건을 NDJSON 파일에 남긴다 | 게임 프로젝트 안 |
| 분석 도구 (Python, `ptel` 명령) | 파일을 SQLite에 적재하고, 정의 파일대로 지표를 계산하고, Metabase 카드까지 만든다 | 개발자 PC |

전체 흐름은 다음 순서다.

1. 게임이 사건을 기록한다. 참가자마다 로그 파일이 하나씩 생긴다.
2. 로그 파일을 한 폴더에 모은다.
3. `ptel run`이 적재, 협동 세션 병합, 조회용 뷰 생성, 지표 계산을 차례로 한다.
4. 결과는 `playtest.sqlite` 파일 하나다. SQLite 도구로 열거나 Metabase 대시보드로 본다.

이 도구의 중심 생각은 **무엇을 지표로 볼지는 수집한 뒤에 정한다**는 것이다. 게임은 원시 사건만 남기고, 지표는 정의 파일(`game.yaml`)에 적는다. 정의를 고치고 `ptel run`을 다시 돌리면 새 지표가 나온다. 게임을 다시 빌드하지 않는다.

분석 도구는 특정 게임을 모른다. 이벤트 이름과 파라미터 이름은 전부 사용자의 게임 폴더에 있다.

## 2. 용어 정리

이 가이드와 도구의 출력에서 쓰는 말이다.

| 용어 | 뜻 |
| --- | --- |
| 이벤트 | 게임에서 일어난 사건 하나. 이름(`level_completed`)과 파라미터를 갖는다. 로그 파일의 한 줄이다. |
| 파라미터 | 이벤트에 붙는 값. `("level_id", 3)`처럼 이름과 값의 쌍이다. 숫자, 글자, 참/거짓을 쓴다. |
| 세션 | 한 판의 플레이. `BeginSession`부터 `EndSession`까지다. 세션 식별자(`session_id`)는 판마다 달라야 한다. |
| 플레이어 | 세션의 참가자 한 명. `player_id`는 세션 안에서만 구분에 쓴다. |
| 호스트 | 협동 세션을 연 참가자. 중복된 기록 중 어느 것을 남길지 정할 때 기준이 된다. |
| 등급 | 버퍼가 가득 찼을 때의 처리 방식. Critical은 반드시 보존하고, Sampled는 넘치면 버린다. |
| 태그 | 이벤트를 묶어 보기 위한 선택 분류값(`"combat"` 등). |
| 로그 파일 | 참가자 한 명의 한 세션 기록. 한 줄에 이벤트 하나인 NDJSON 형식이다. |
| 게임 폴더 | 한 게임을 위한 정의를 모은 폴더. `game.yaml`(필수), `views.sql`, `cards.py`(선택)가 들어간다. |
| 정의 파일 | 게임 폴더의 `game.yaml`. 지표 정의와 협동 게임의 병합 규칙을 적는다. |
| 지표 | 이벤트에서 계산한 값. 예: 클리어 횟수, 평균 클리어 시간, 승률. |
| 범위(scope) | 지표를 묶는 단위. `session`은 세션×플레이어, `party`는 세션 전체, `global`은 모든 세션이다. |
| 축(by) | 지표를 쪼개는 파라미터. `by: level_id`면 레벨별로 값이 나온다. |
| 병합 | 협동 세션에서 여러 참가자 파일에 겹쳐 기록된 같은 사건을 하나로 세는 단계. |
| canonical | 병합 결과 표시. 겹친 기록 중 남긴 행은 1, 나머지는 0이다. 원본 행은 지우지 않는다. |
| 뷰 | 조회용 가상 테이블. 이벤트마다 `v_<이벤트이름>` 뷰가 자동으로 생긴다. |
| 카드 | Metabase의 차트 하나. 지표마다 자동으로 만들어진다. |

## 3. 설치

분석 도구와 수집기를 따로 설치한다. 분석 도구는 로그를 분석하는 PC에만 있으면 된다.

### 분석 도구

Python 3.9 이상이 필요하다. 외부 의존성은 PyYAML 하나이고 함께 설치된다.

```bash
pip install git+https://github.com/qssxc/game-telemetry
ptel --version
```

PyPI에는 올리지 않고 GitHub에서 바로 설치한다. 새 버전으로 올릴 때는 아래 명령을 쓴다.

```bash
pip install --upgrade git+https://github.com/qssxc/game-telemetry
```

### 수집기 (Unity)

Unity의 Package Manager에서 **Add package from git URL**을 고르고 아래 주소를 넣는다.

```
https://[github.com/qssxc/game-telemetry/issues](https://github.com/qssxc/game-telemetry/issues).git?path=unity
```

설치하면 `Playtest.Telemetry` 네임스페이스를 쓸 수 있다. Package Manager의 Samples에서 동작 확인용 스크립트(Smoke Test)를 가져올 수 있다.

### 지원 환경

| 대상 | 지원 | 비고 |
| --- | --- | --- |
| 분석 도구 | Windows, macOS, Linux | Python 3.9 이상 |
| 수집기: PC (Windows, macOS, Linux) | 지원 | 기본 방식. 워커 스레드가 파일에 쓴다 |
| 수집기: Android, iOS | 지원 | 앱이 백그라운드로 갈 때 기록을 마무리한다. 로그는 기기에서 직접 꺼내야 한다 |
| 수집기: WebGL | 지원 | 스레드 없이 동작한다. 로그는 브라우저 다운로드로 꺼낸다 |
| Metabase | 선택 | 대시보드가 필요할 때만 따로 설치한다. 이 도구에 포함되어 있지 않다 |

플랫폼별 차이는 5장의 "플랫폼별 차이"에서 다룬다.

## 4. 5분 시작하기

수집기를 붙이고 첫 지표를 보기까지 다섯 단계다.

**1단계. 게임 시작 시 초기화하고 세션을 연다.**

```csharp
using Playtest.Telemetry;

Telemetry.Init(new TelemetryConfig
{
    BuildVersion = Application.version,
    ConsentGiven = true,   // 수집 동의를 받은 뒤에 true 로 둔다
});

Telemetry.BeginSession(SessionMode.Solo);
```

**2단계. 보고 싶은 사건이 일어나는 곳에 기록 호출을 넣는다.**

```csharp
Telemetry.Record("level_completed",
    ("level_id", 3),
    ("is_success", true),
    ("elapsed_ms", 48200));
```

**3단계. 게임을 플레이하고 종료한다.** 로그 파일이 `Application.persistentDataPath/telemetry` 폴더에 생긴다. 파일 이름은 `session_<세션>_<플레이어>.ndjson`이다.

**4단계. 게임 폴더를 만들고 지표를 정의한다.**

```bash
ptel init mygame
```

`mygame/game.yaml`을 열어 자기 게임의 이벤트 이름으로 바꾼다.

```yaml
metrics:
  - name: level_count
    type: count
    event: level_completed

  - name: avg_level_time_ms
    type: avg
    event: level_completed
    param: elapsed_ms
```

**5단계. 로그 폴더를 넘겨 돌린다.**

```bash
ptel run <로그폴더> --game mygame
```

현재 폴더에 `playtest.sqlite`가 생긴다. 지표는 `metric_results` 테이블에, 이벤트는 `v_level_completed` 같은 뷰에 들어 있다. 조회 방법은 10장에 있다.

## 5. 수집기 사용법 (Unity)

모든 기능은 `Playtest.Telemetry` 네임스페이스의 정적 클래스 `Telemetry`로 쓴다. 기록 호출은 큐에 넣기만 하고 바로 돌아온다. 파일에 쓰는 일은 뒤에서 따로 일어나므로 게임 프레임을 막지 않는다.

기록 호출은 메인 스레드에서 한다. 기록이 실패해도 예외를 던지지 않고, 콘솔에 `[Telemetry]`로 시작하는 경고만 남긴다.

### 초기화와 설정

게임 시작 시 `Telemetry.Init`을 한 번 부른다.

| 설정 (`TelemetryConfig`) | 기본값 | 뜻 |
| --- | --- | --- |
| `ConsentGiven` | `false` | 수집 동의 여부. `false`면 아무것도 기록하지 않는다 |
| `Enabled` | `true` | `false`면 모든 기록 호출이 아무 일도 하지 않는다 |
| `OutputPath` | `persistentDataPath/telemetry` | 로그 파일을 쓸 폴더 |
| `PlayerId` | 기기별로 자동 생성 | 플레이어 식별자 |
| `BuildVersion` | `"0.0.0"` | 빌드 식별자. `Application.version`을 넣기를 권한다 |
| `FlushIntervalSec` | `2` | 파일에 쓰는 주기(초). 비정상 종료 시 이 시간만큼의 기록을 잃을 수 있다 |
| `QueueCapacity` | `4096` | 큐 용량. Sampled 등급은 이를 넘으면 버려진다 |
| `VerboseLog` | `false` | 내부 동작을 콘솔에 출력한다. 처음 붙일 때 켜 두면 좋다 |
| `UseWorkerThread` | `true` | `false`면 스레드 없이 게임 스레드에서 주기마다 쓴다. WebGL은 항상 이 방식이다 |
| `DownloadOnSessionEnd` | `false` | WebGL 전용. 세션이 끝날 때 로그를 브라우저 다운로드로 내보낸다 |

### 수집 동의

`ConsentGiven`의 기본값은 `false`다. 동의가 없으면 로그 파일도, 기기에 저장하는 플레이어 식별자도 만들지 않는다. 콘솔에 "수집 동의가 없어 기록하지 않습니다" 경고가 한 번 뜬다.

동의 여부는 **세션을 시작하기 전에** 정해져 있어야 한다. 세션 도중에 켜도 그 세션은 기록되지 않는다.

### 세션

| 함수 | 쓰는 경우 |
| --- | --- |
| `string BeginSession(SessionMode mode, string sessionId = null, string playerId = null)` | 혼자 하는 게임, 또는 협동 세션의 호스트. 세션 식별자를 돌려준다 |
| `void JoinSession(string sessionId, string playerId, long hostOffsetMs)` | 협동 세션의 참가자. 호스트에게 받은 식별자로 참가한다 |
| `void EndSession()` | 세션을 끝내고 파일을 마감한다 |
| `void Shutdown()` | 앱 종료 시. Unity에서는 자동으로 불린다 |

협동 게임은 참가자 전원이 **같은 세션 식별자**를 써야 한다. 두 가지 방법이 있다.

- 호스트가 `BeginSession`이 돌려준 값을 네트워크로 참가자에게 보낸다.
- 게임에 이미 공유된 값(방 번호 등)이 있으면 그것을 `sessionId`로 넘긴다.

```csharp
// 호스트
Telemetry.BeginSession(SessionMode.Coop, $"{roomCode}_{DateTime.UtcNow:yyyyMMddHHmmss}", "p1");

// 참가자
Telemetry.JoinSession(sharedSessionId, "p2", hostOffsetMs: 0);
```

**세션 식별자는 판마다 달라야 한다.** 방 번호만 쓰면 같은 방으로 다시 플레이했을 때 두 판이 한 세션으로 합쳐진다. 위 예처럼 날짜와 시각을 붙인다. 같은 식별자로 다시 시작하면(재접속 등) 기존 파일 뒤에 이어 쓰고 경고를 남긴다. 순번(`seq`)과 경과 시간(`t_ms`)도 앞 기록에서 이어진다.

호스트에 수집 동의가 없어도 `BeginSession`은 세션 식별자를 돌려준다. 동의한 참가자에게 전달할 값이 필요하기 때문이다.

### 사건 기록

```csharp
// 기본
Telemetry.Record("item_purchased", ("item_id", 19), ("price", 250));

// 분류 태그를 붙여서
Telemetry.Record("boss_phase_changed", "combat", ("boss_id", "flame_golem"), ("phase", 2));

// 등급을 지정해서 (자주 발생하는 값)
Telemetry.Record("player_position", EventTier.Sampled, ("x", pos.x), ("z", pos.z));
```

- **파라미터 타입**: `int`, `long`, `float`, `double`, `bool`, `string`을 쓴다. `uint`, `ulong`, `short`, `byte`, `decimal` 같은 다른 정수·실수 타입도 숫자로 기록된다. 그 외 타입(열거형 등)은 문자열로 바꿔 기록하고, 개발 빌드에서 경고한다.
- **이름 규칙**: 이벤트 이름과 파라미터 이름은 영문, 숫자, 밑줄(`_`)만 쓰고 숫자로 시작하지 않는다. 그래야 분석 쪽에서 이벤트별 뷰가 만들어진다.
- **태그와 파라미터의 혼동**: `Record("x", "combat")`의 두 번째 인자는 태그다. 파라미터는 반드시 `("이름", 값)` 모양으로 넘긴다.

### 리소스 변동

재화처럼 늘고 주는 값은 `Resource`로 기록한다. 보유량이 아니라 **변동량과 사유**를 남긴다. 사유가 있어야 "상점에서 쓴 골드"와 "전투에서 얻은 골드"를 나눠 볼 수 있다.

```csharp
Telemetry.Resource("gold", -250, "shop_purchase", balance: 1750);
```

이 호출은 `resource_changed` 이벤트를 남기고, 파라미터는 `resource_id`, `delta`, `reason`, `balance`(선택)다.

### 등급

| 등급 | 버퍼가 가득 차면 | 쓰는 곳 |
| --- | --- | --- |
| `EventTier.Critical` (기본) | 큐를 늘려서라도 보존한다 | 구매, 전투 결과처럼 한 건 한 건이 의미 있는 사건 |
| `EventTier.Sampled` | 버리고 버린 수를 센다 | 위치처럼 통계적으로만 의미 있는 값 |

버려진 수는 `Telemetry.DroppedCount`로 볼 수 있고, 세션이 끝날 때 `session_ended` 이벤트의 `dropped_count`로 기록된다.

### 파일에 쓰는 시점

| 함수 | 동작 |
| --- | --- |
| (자동) | `FlushIntervalSec`마다 파일에 쓴다 |
| `Telemetry.Flush()` | 지금 쓰도록 깨우고 바로 돌아온다 |
| `Telemetry.FlushNow(int timeoutMs = 500)` | 파일에 들어갈 때까지 기다린다. 다 들어갔으면 `true` |

앱이 백그라운드로 가면 수집기가 알아서 기록을 마무리한다. 모바일과 WebGL에서는 끝까지 쓰고 돌아가고, PC에서는 게임을 세우지 않고 깨우기만 한다.

### 상태 확인과 로그 꺼내기

| 항목 | 내용 |
| --- | --- |
| `Telemetry.IsRecording` | 지금 기록이 실제로 되는 상태인지 |
| `Telemetry.CurrentSessionId` | 현재 세션 식별자. 세션이 없으면 `null` |
| `Telemetry.OutputPath` | 로그 파일이 쌓이는 폴더 |
| `Telemetry.CurrentLogFile` | 기록 중인 파일의 경로 |
| `Telemetry.GetLogFiles()` | 출력 폴더의 로그 파일 경로. 최근 것이 앞에 온다 |
| `Telemetry.ExportLog(path)` | WebGL에서 파일을 브라우저 다운로드로 내보낸다. 다른 플랫폼에서는 `false`를 돌려준다 |

### 플랫폼별 차이

| 플랫폼 | 기록 방식 | 로그를 꺼내는 법 | 알아둘 점 |
| --- | --- | --- | --- |
| PC | 워커 스레드 | 출력 폴더에서 파일을 복사한다 | 강제 종료 시 마지막 주기(기본 2초)의 기록을 잃을 수 있다 |
| Android, iOS | 워커 스레드 | USB로 앱 저장소의 `telemetry` 폴더를 꺼내거나, 게임의 공유 기능에 `GetLogFiles()`의 경로를 넘긴다 | 앱이 백그라운드에서 종료되면 `session_ended`가 남지 않는다 |
| WebGL | 스레드 없음 | `ExportLog` 또는 `DownloadOnSessionEnd`로 다운로드한다 | 탭을 닫으면 브라우저가 게임에 알리지 않아 마지막 주기의 기록을 잃을 수 있다 |

`session_ended`가 없는 로그도 분석할 수 있다. 분석 도구는 이를 비정상 종료로 표시하고 나머지 이벤트를 그대로 적재한다.

## 6. 로그 형식

로그는 한 줄에 이벤트 하나가 들어가는 NDJSON 파일이다. 참가자 한 명의 한 세션이 파일 하나가 되고, 이름은 `session_<세션>_<플레이어>.ndjson`이다. 형식 버전은 1이다. 세션·플레이어 식별자에 영문, 숫자, `-`, `_`가 아닌 글자가 있거나 64자를 넘으면, 파일 이름에서는 그 글자를 `_`로 바꾸고 원래 값의 해시 8자리를 붙인다(예: `보스방_1120` → `session_____1120-17b55fd9_p1.ndjson`). 서로 다른 판이 한 파일에 섞이지 않게 하기 위해서다. 로그 안의 `session_id`는 원래 값 그대로다.

```json
{"session_id":"room7_20261115093012","player_id":"p1","seq":12,"t_ms":48211,"t_raw_ms":48211,"wall_clock":"2026-11-15T00:30:48.211Z","name":"level_completed","tier":"critical","build_version":"1.2.0","params":{"level_id":3,"is_success":true,"elapsed_ms":48200}}
```

| 필드 | 내용 |
| --- | --- |
| `session_id` | 세션 식별자. 협동 세션은 참가자 전원이 같은 값이다 |
| `player_id` | 플레이어 식별자 |
| `seq` | 이 파일 안에서의 순번. 1부터 늘어난다 |
| `t_ms` | 세션 시작부터 흐른 시간(밀리초). 시스템 시계가 아니라 단조 시계로 잰다 |
| `t_raw_ms` | 보정 전 경과 시간. 로그 파일에서는 `t_ms`와 같다. 분석 도구가 협동 세션의 `t_ms`를 맞춘 뒤에도 이 값은 그대로 남는다(9장 "시간 정렬") |
| `wall_clock` | UTC 절대 시각. 참고용이다 |
| `name` | 이벤트 이름 |
| `tag` | 분류 태그. 없으면 필드가 생략된다 |
| `tier` | `critical` 또는 `sampled` |
| `build_version` | 빌드 식별자 |
| `params` | 파라미터. 없으면 필드가 생략된다 |

분석 도구는 `session_id`, `player_id`, `seq` 중 하나라도 없는 줄과 JSON으로 읽히지 않는 줄을 건너뛰고 경고한다. 비정상 종료로 마지막 줄이 잘려도 나머지는 적재된다. 반대로 한 참가자의 로그가 두 파일에 나뉘어 있거나 한 파일에 여러 세션이 섞여 있으면 적재를 멈춘다. 그대로 넣으면 앞 파일의 기록이 오류 없이 지워지기 때문이다. 멈추면 기존 데이터는 그대로 남는다.

### 세션 이벤트

수집기가 세션의 시작과 끝에 자동으로 남기는 이벤트 두 개가 있다. 분석 도구는 여기서 세션 정보를 뽑는다.

| 이벤트 | 파라미터 |
| --- | --- |
| `session_started` | `mode`(`solo`/`coop`), `is_host`, `clock_offset_ms`, `started_at`, `platform` |
| `session_ended` | `duration_ms`, `dropped_count` |

수집기를 쓰지 않고 다른 방법으로 로그를 만들어도 된다. 위 필드와 두 세션 이벤트만 지키면 분석 도구가 그대로 읽는다.

## 7. 분석 도구 명령 (ptel)

명령은 네 개다. `ptel <명령> --help`로 각 명령의 옵션을 볼 수 있다.

| 명령 | 하는 일 |
| --- | --- |
| `ptel init <폴더>` | 게임 폴더의 틀을 만든다 |
| `ptel run <로그폴더> --game <폴더>` | 적재, 병합, 뷰 생성, 지표 계산을 차례로 돌린다 |
| `ptel check --game <폴더>` | 협동 세션이 병합 가능한 상태인지 본다 |
| `ptel push --game <폴더>` | Metabase 카드만 생성하거나 갱신한다 |

### ptel init

`game.yaml`, `views.sql`, `cards.py` 세 파일을 예시 내용으로 만든다. 필수는 `game.yaml` 하나이고, 나머지 둘은 필요 없으면 지운다. 폴더에 같은 이름의 파일이 이미 있으면 멈춘다. 덮어쓰려면 `--force`를 붙인다.

### ptel run

```bash
ptel run <로그폴더> --game mygame                  # 기본
ptel run <로그폴더> --game mygame --reset          # 기존 내용을 비우고 새로 적재
ptel run <로그폴더> --game mygame --reset --push   # Metabase 카드까지 반영
ptel run --game mygame --skip-load                # 적재는 건너뛰고 뒷단계만 다시
```

| 옵션 | 기본값 | 뜻 |
| --- | --- | --- |
| `--game <폴더>` | (필수) | 게임 폴더 |
| `--yaml <파일>` |  | 게임 폴더 없이 정의 파일 하나만으로 돌릴 때 쓴다 |
| `--db <파일>` | `playtest.sqlite` | 결과 DB 파일 |
| `--reset` |  | 기존 내용을 비우고 새로 적재한다. DB 파일은 지우지 않는다 |
| `--skip-load` |  | 적재를 건너뛴다. 지표 정의만 고쳤을 때 쓴다 |
| `--push` |  | 끝난 뒤 Metabase 카드를 반영한다 |
| `--collection <이름>` | `플레이테스트 지표` | 카드를 담을 Metabase 컬렉션 |
| `-v`, `--verbose` |  | 각 단계의 상세 출력을 모두 보여준다 |

`--reset` 없이 돌리면 같은 세션의 기록은 덮어쓰고 새 세션은 추가된다. 로그 폴더에서 파일을 뺐다면 `--reset`으로 돌려야 그 세션이 DB에서도 사라진다.

실행하면 단계별로 걸린 시간과 요약이 나온다. `[경고]`가 붙은 줄은 결과에 영향을 줄 수 있으니 읽어 본다.

```
[1/4] 적재 — logs → playtest.sqlite
      0.1초 — 파일 2개, 이벤트 1225건
[2/4] 협동 세션 병합
      0.1초 — 중복 310행 제외 (3종 이벤트)
[3/4] 이벤트 뷰 생성
      0.0초 — 뷰 9개
[4/4] 지표 계산 — game.yaml
      0.0초 — metric_results 5행
```

한 단계가 실패하면 거기서 멈춘다. 적재가 실패하면 기존 데이터는 그대로 남는다.

### ptel check

2인 이상 세션을 적재한 뒤 돌린다. 호스트가 한 명인지, 어떤 이벤트가 여러 파일에 겹쳐 있는지, 병합 규칙이 그것을 다 덮는지를 보여준다. 쓰는 법은 9장에 있다.

### ptel push

`ptel run --push`의 마지막 단계만 따로 돌린다. `--dry-run`을 붙이면 보내지 않고 무엇이 바뀔지만 보여준다. 설정은 11장에 있다.

### 단계를 하나씩 돌리기

문제를 좁힐 때는 단계를 따로 돌릴 수 있다. 순서를 지켜야 한다.

```bash
python -m playtest_telemetry.loader  <로그폴더> --db playtest.sqlite --reset --game-views mygame/views.sql
python -m playtest_telemetry.merge   mygame/game.yaml --db playtest.sqlite --show
python -m playtest_telemetry.viewgen --db playtest.sqlite --skip-existing mygame/views.sql --apply --no-file
python -m playtest_telemetry.metrics mygame/game.yaml --db playtest.sqlite --show
```

## 8. 지표 정의 (game.yaml)

지표는 `game.yaml`의 `metrics:` 아래에 목록으로 적는다. 고친 뒤 `ptel run --game mygame --skip-load`를 돌리면 다시 계산된다.

```yaml
metrics:
  - name: level_count          # 지표 이름
    type: count                # 종류
    event: level_completed     # 어떤 이벤트를 셀지
```

### 지표에 쓸 수 있는 키

| 키 | 필수 | 뜻 |
| --- | --- | --- |
| `name` | 예 | 지표 이름. 영문, 숫자, 밑줄을 쓴다(한글도 된다) |
| `type` | 예 | `count`, `sum`, `avg`, `min`, `max`, `formula` 중 하나 |
| `event` | `formula` 외 | 집계할 이벤트 이름 |
| `param` | `count`, `formula` 외 | 집계할 숫자 파라미터 |
| `where` |  | 파라미터 조건. 맞는 이벤트만 집계한다 |
| `by` |  | 이 파라미터의 값별로 쪼개서 계산한다 |
| `scope` |  | `session`(기본), `party`, `global` |
| `default` |  | 해당 이벤트가 없는 세션에 채울 값 |
| `formula` | `formula`일 때 | 다른 지표로 만드는 수식 |

여기 없는 키를 적으면 멈추고 알려준다. `wehre:` 같은 오타가 조건 없이 전부 집계되는 일을 막기 위해서다.

### 종류 (type)

| 종류 | 계산 | 예 |
| --- | --- | --- |
| `count` | 조건에 맞는 이벤트 수 | 클리어 횟수 |
| `sum` | `param`의 합 | 쓴 코인 |
| `avg` | `param`의 평균 | 평균 클리어 시간 |
| `min`, `max` | `param`의 최소, 최대 | 최단 클리어 시간 |
| `formula` | 앞서 선언한 지표로 계산 | 클리어율 |

### 조건 (where)

```yaml
  - name: coins_spent_in_shop
    type: sum
    event: coins_changed
    param: delta
    where:
      reason: shop            # 값을 그대로 쓰면 "같다"
      delta: { lt: 0 }        # 연산자를 쓰면 비교
```

연산자는 `eq`, `ne`, `lt`, `lte`, `gt`, `gte`다. 조건을 여러 개 적으면 모두 맞는 이벤트만 집계한다.

### 축 (by)

```yaml
  - name: deaths_by_level
    type: count
    event: player_died
    by: level_id
```

레벨별로 값이 하나씩 나온다. 축을 추가해도 DB 구조는 바뀌지 않는다.

### 범위 (scope)

| 범위 | 묶는 단위 | 쓰는 곳 |
| --- | --- | --- |
| `session` (기본) | 세션 × 플레이어 | "내가 쓴 골드" |
| `party` | 세션 전체 (참가자를 합침) | "파티가 쓴 골드" |
| `global` | 모든 세션 | 전체 평균 |

범위와 상관없이 모든 집계 지표는 전체 값(`global`)도 하나씩 계산된다. 전체 평균은 세션 평균의 평균이 아니라 원본 이벤트에서 다시 계산한다.

### 기본값 (default)

해당 이벤트가 한 번도 없는 세션은 보통 값이 없다. "0건"이 0을 뜻하는 지표에만 `default: 0`을 적는다. 쓴 코인의 합은 0이 맞지만, 평균 클리어 시간은 클리어를 안 했으면 0이 아니라 값이 없는 것이다.

### 수식 (formula)

```yaml
  - name: level_cleared
    type: count
    event: level_completed
    where: { is_success: true }

  - name: clear_rate
    type: formula
    formula: level_cleared / level_count
```

- 사칙연산(`+ - * /`), 괄호, 숫자, 지표 이름만 쓴다.
- 참조하는 지표는 **그 수식보다 위에** 선언되어 있어야 한다.
- 0으로 나누거나 참조한 지표에 값이 없으면 결과도 값이 없다. "안 했다"와 "0%"를 구분하기 위해서다.
- 세션 범위 수식에서 같은 세션의 파티 지표를 `party.지표이름`으로 참조할 수 있다. 예: `my_gold_spent / party.gold_spent`.
- 없는 지표 이름을 쓰면 멈추고 알려준다.

## 9. 협동 게임: 병합 규칙

혼자 하는 게임은 이 장을 건너뛴다. 여러 명이 한 세션에 들어가는 게임은 `game.yaml`에 `merge:` 섹션을 적어야 숫자가 맞는다.

### 왜 필요한가

참가자마다 로그 파일이 따로 생긴다. 파티가 공유하는 골드가 100 줄면 그 사건이 모든 참가자의 파일에 한 번씩 기록된다. 그대로 합치면 지출이 참가자 수만큼 부풀려진다. **오류는 나지 않고 숫자만 틀린다.**

병합은 겹친 기록 중 하나만 남기는 단계다. 원본은 지우지 않고 `canonical` 값만 0으로 내린다. 지표와 자동 뷰는 `canonical = 1`인 행만 본다.

2인 이상 세션이 있는데 `merge:` 섹션이 없으면 `ptel run`이 `[경고]`를 낸다.

### 규칙 쓰는 법

```yaml
merge:
  shared_state:                        # 파티가 공유하는 상태
    resource_changed:
      key: [resource_id, delta, balance]
      only: { resource_id: [gold] }    # 선택
  broadcast:                           # 전원에게 알려지는 사건
    boss_defeated: [boss_id, spawn_id]
```

| 항목 | 뜻 |
| --- | --- |
| `shared_state` | 한 명이 일으킨 변화를 전원이 관측하는 이벤트. 공유 재화, 공유 인벤토리 등 |
| `broadcast` | 호스트나 서버가 전원에게 알리는 사건. 보스 처치, 플레이어 쓰러짐 등 |
| `key` | 같은 사건인지 가르는 파라미터 목록. `key`만 있으면 `이벤트: [a, b]`로 줄여 쓴다 |
| `only` | 이 조건에 맞는 행만 판정한다. 한 이벤트가 공유 자원과 개인 자원을 함께 나를 때 쓴다 |

두 분류는 판정 방식이 같다. 읽는 사람이 구분하기 쉽도록 나눠 적을 뿐이다. 구매나 전투처럼 각자 따로 겪는 사건은 적지 않는다.

### key 고르는 법

- **모든 참가자의 파일에서 같은 값**인 파라미터만 넣는다. 서버나 호스트가 내려준 값이 여기에 해당한다.
- 클라이언트가 직접 잰 값(`elapsed_ms` 등)은 넣지 않는다. 파일마다 달라서 같은 사건이 서로 다른 것으로 판정된다.
- 공유 잔액(`balance`)은 좋은 키다. 같은 금액의 거래가 반복돼도 잔액은 매번 달라 사건을 구분해 준다.

같은 키가 한 세션에 여러 번 나오면 파일 안에서의 등장 순서대로 짝을 짓는다.

### only가 필요한 경우

`resource_changed`로 공유 골드와 개인 경험치를 모두 기록한다고 하자. `only`가 없으면 두 사람이 우연히 같은 경험치 변동(둘 다 +50, 잔액 50)을 겪었을 때 한쪽이 중복으로 빠진다. `only: { resource_id: [gold] }`로 공유 자원만 지정한다. 반대로 공유 자원을 `only`에서 빠뜨리면 그 자원이 부풀려진다.

### 어느 기록을 남기는가

겹친 기록 중 남길 행은 이 순서로 고른다.

1. 사유를 아는 행. `reason` 파라미터가 있고 `unknown`이 아닌 쪽이다.
2. 호스트의 행.
3. `player_id` 순서. 다시 돌려도 같은 결과가 나오게 하기 위한 규칙이다.

### ptel check로 규칙 만들기

처음에는 무엇을 적어야 할지 모른다. 2인 세션 로그를 적재한 뒤 `merge:` 없이 점검을 돌린다.

```bash
ptel run <로그폴더> --game mygame --reset
ptel check --game mygame
```

이벤트마다 판정이 나온다.

| 판정 | 뜻 | 할 일 |
| --- | --- | --- |
| `? 규칙 없음 — 같은 내용 N건` | 두 파일에 파라미터까지 같은 행이 있다 | 한 사건을 각자 기록한 것이면 규칙을 적는다 |
| `★ 공유 상태 — 규칙 있음` | 규칙이 적용되고 있다 | 없음 |
| `각자 기록 (규칙 없음)` | 겹치는 내용이 없다 | 없음 |
| `한쪽만` | 한 참가자의 파일에만 있다 | 없음 |

`session_started`처럼 원래 각자 남기는 이벤트도 후보로 나올 수 있다. 그런 것은 그대로 둔다. 규칙을 적은 뒤 다시 돌려 후보가 사라졌는지 확인한다.

### 선택 항목

| 항목 | 쓰는 경우 |
| --- | --- |
| `reason_correction` | 변동의 사유를 나중에 알려주는 보정 이벤트를 게임이 남길 때. `event`, `target`, `match`를 적는다 |
| `clock_pairs` | `ptel check`와 시간 정렬에 쓸 두 PC의 시계 차이를 잰다. 서로를 관측한 사건(쓰러짐과 그 알림 등)이 있을 때 `observed`, `who`, `cause`를 적는다 |
| `align_time` | 기본 `true`. 협동 세션의 `t_ms`를 호스트 시간축에 맞추지 않으려면 `false`를 적는다 |

세 항목의 예시는 `ptel init`이 만든 `game.yaml`의 주석에 있다.

### 시간 정렬

`t_ms`는 각 참가자가 자기 세션을 시작한 순간부터 잰 시간이라, 그대로는 참가자끼리 비교할 수 없다. 호스트가 0초에 시작하고 참가자가 30초 늦게 들어오면, 같은 순간이 호스트 쪽에서는 40000, 참가자 쪽에서는 10000이다.

그래서 `ptel run`의 병합 단계가 협동 세션의 `t_ms`를 **호스트의 시간축**으로 옮긴다. 설정이 필요 없고, 1인 세션은 건드리지 않는다.

- 두 PC의 시계 차이는 `clock_pairs`가 있으면 재서 뺀다. 없으면 PC 시계를 그대로 믿으므로, 두 PC의 시계가 어긋난 만큼 `t_ms`도 어긋난다.
- 원래 값은 `events.t_raw_ms`에 그대로 남는다. 다시 돌리면 원래 값에서 다시 계산한다.
- 무엇을 얼마나 옮겼는지는 `clock_alignment` 테이블에 남는다. `method`가 `왕복 쌍`이면 시계 차이를 잰 것이고, `PC 시계`면 재지 못한 것이다.
- 수집기의 `JoinSession`에 넘기는 `hostOffsetMs`는 정렬에 쓰지 않는다. `session_started`의 `clock_offset_ms`로 기록만 된다.

끄려면 `game.yaml`에 `merge: { align_time: false }`를 적는다.

## 10. 결과 조회

결과는 SQLite 파일 하나(`playtest.sqlite`)다. DB Browser for SQLite 같은 도구로 열거나 Metabase에 연결한다. 대부분의 조회는 지표 뷰와 이벤트별 뷰 두 가지로 끝난다.

### 지표 보기

| 뷰 | 한 행 | 칼럼 |
| --- | --- | --- |
| `v_metrics_session` | 세션 × 플레이어 × 지표 | `session_id`, `player_id`, `mode`, `is_host`, `started_at`, `duration_ms`, `metric_name`, `value` |
| `v_metrics_party` | 세션 × 지표 | `session_id`, `metric_name`, `value`, `party_size` |
| `v_metrics_dimension` | 축으로 쪼갠 값 (세션·파티 범위) | `session_id`, `player_id`, `metric_name`, `dimension_key`, `dimension_value`, `value` |
| `v_metrics_global` | 지표 | `metric_name`, `value`, `computed_at` |
| `v_metrics_global_dimension` | 축 값 × 지표 (전체, 원본에서 다시 계산) | `metric_name`, `dimension_key`, `dimension_value`, `value`, `computed_at` |

```sql
-- 세션별 클리어율
SELECT session_id, player_id, value
FROM v_metrics_session
WHERE metric_name = 'clear_rate';
```

### 이벤트 보기

`ptel run`은 이벤트마다 `v_<이벤트이름>` 뷰를 만든다. 파라미터가 칼럼으로 펼쳐져 있어 바로 조회할 수 있다.

```sql
SELECT level_id, AVG(elapsed_ms)
FROM v_level_completed
WHERE is_success = 1
GROUP BY level_id;
```

- 모든 뷰에 `session_id`, `player_id`, `seq`, `t_ms`, `wall_clock`, `canonical` 칼럼이 있다.
- 칼럼 타입은 실제 값에서 정한다. 소수가 하나라도 있으면 실수, 없으면 정수다.
- 파라미터 이름이 위 공통 칼럼과 겹치면 뒤에 `_param`이 붙는다.
- 새 이벤트를 게임에 추가하면 다음 `ptel run`에서 그 뷰가 생긴다.
- 2^53을 넘는 정수(64비트 식별자)는 정확히 보존하기 위해 글자로 저장된다.

협동 세션에서 중복이 걸러진 이벤트는 뷰가 둘이다. `v_<이벤트>`는 중복을 뺀 것이고, `v_<이벤트>_all`은 원본 전부다. 집계에는 앞의 것을 쓴다.

모든 이벤트를 한 번에 훑어볼 때는 `event_flat`을 쓴다. 파라미터가 JSON 한 칸에 들어 있다.

```sql
SELECT name, params FROM event_flat WHERE session_id = 'room7_20261115093012' ORDER BY seq;
```

### 테이블

뷰 아래에 있는 실제 테이블이다. 직접 볼 일은 많지 않다.

| 테이블 | 내용 |
| --- | --- |
| `sessions` | 참가자 한 명의 한 세션. 모드, 호스트 여부, 시작 시각, 길이, 이벤트 수, 원본 파일 이름 |
| `events` | 이벤트 한 건. 이름, 시각, 등급, `canonical` |
| `event_params` | 파라미터 한 개가 한 행. 숫자는 `num_value`, 글자는 `text_value`, 참/거짓은 `bool_value` |
| `metric_results` | 계산된 지표 |
| `merge_overrides` | 병합 단계가 보정한 값. 원본 위에 덧씌워 보여준다 |
| `clock_alignment` | 협동 세션의 시간 정렬 내역. 참가자마다 `t_ms`를 옮긴 양과 시계 차이를 잰 방법 |
| `load_log` | 적재 기록. 파일별 읽은 줄 수와 건너뛴 줄 수 |

`sessions.duration_ms`가 비어 있으면 `session_ended` 없이 끝난 세션이다.

### 손으로 쓰는 뷰 (views.sql)

자동 뷰로 안 되는 것만 게임 폴더의 `views.sql`에 쓴다. 누적 합계나 두 이벤트를 한 축으로 묶는 뷰가 여기에 해당한다. `ptel run`이 매번 다시 적용한다.

```sql
DROP VIEW IF EXISTS v_coin_ledger;
CREATE VIEW v_coin_ledger AS
SELECT e.session_id, e.player_id, e.seq, e.canonical,
       CAST(d.num_value AS INTEGER) AS delta,
       SUM(CAST(d.num_value AS INTEGER)) OVER (
           PARTITION BY e.session_id, e.player_id ORDER BY e.seq
       ) AS cumulative_delta
FROM events e
LEFT JOIN event_params_merged d
       ON d.session_id = e.session_id AND d.player_id = e.player_id
      AND d.seq = e.seq AND d.key = 'delta'
WHERE e.name = 'coins_changed';
```

지킬 것은 네 가지다.

- 뷰마다 `DROP VIEW IF EXISTS`를 앞에 둔다.
- 파라미터는 `event_params`가 아니라 `event_params_merged`에서 읽는다. 병합 단계의 보정이 반영된 뷰다.
- `events`의 별칭을 `e`로 두고 `WHERE e.name = '이벤트'`로 쓴다. 이 모양을 읽어 그 이벤트의 자동 뷰를 건너뛴다.
- `BEGIN`과 `COMMIT`을 쓰지 않는다.

## 11. Metabase 연동

Metabase는 선택 사항이다. 대시보드가 필요할 때 따로 설치해 쓰고, 이 도구는 카드(차트)를 대신 만들어 준다. Metabase 자체는 이 도구에 포함되어 있지 않으며 별도의 라이선스를 따른다.

### 준비

1. Metabase를 설치해 실행한다.
2. 관리자 설정의 데이터베이스에서 SQLite를 고르고 `playtest.sqlite`의 경로를 등록한다.
3. 관리자 설정의 인증에서 API 키를 만든다.
4. 터미널에 주소와 키를 환경변수로 설정한다.

```bash
# macOS, Linux
export METABASE_URL=http://localhost:3000
export METABASE_API_KEY=mb_xxxxxxxx
```

```powershell
# Windows PowerShell
$env:METABASE_URL     = "http://localhost:3000"
$env:METABASE_API_KEY = "mb_xxxxxxxx"
```

API 키 대신 `METABASE_USER`와 `METABASE_PASSWORD`로 로그인할 수도 있다. 키와 비밀번호는 명령줄 인자로 받지 않는다. 셸 기록에 남기 때문이다.

### 카드 올리기

```bash
ptel push --game mygame --dry-run    # 무엇이 바뀔지만 본다
ptel push --game mygame              # 반영한다
```

`ptel run ... --push`로 계산과 반영을 한 번에 할 수도 있다. 반영 과정은 다음과 같다.

- **지표마다 카드 한 장**이 만들어진다. `game.yaml`에 지표를 추가하고 다시 올리면 카드가 따라 생긴다.
- 카드는 `플레이테스트 지표` 컬렉션에 모인다. `--collection`으로 바꾼다.
- 다시 올리면 지우지 않고 갱신한다. 대시보드에 올려 둔 카드가 떨어지지 않는다.
- Metabase에서 손으로 바꾼 차트 모양은 덮어쓰지 않는다.
- 보내기 전에 모든 카드의 SQL을 로컬 DB에서 돌려 본다. 하나라도 실패하면 아무것도 올리지 않는다.

| 지표의 모양 | 카드 이름 | 내용 |
| --- | --- | --- |
| 기본 (`scope: session`) | `지표 · <이름>` | 세션·플레이어별 막대 |
| `by`가 있음 | `지표 · <이름> (<축>)` | 축 값별 막대. `avg`는 전체 값 |
| `scope: party` | `파티 · <이름>` | 세션별 막대와 참가 인원 |
| `scope: global` | `지표 · <이름>` | 전체 값 하나 |

대부분의 카드에는 `session_id` 필터가 달려 있고, 비워 두면 전체를 본다. 축별 `avg` 카드와 `scope: global` 카드는 필터 없이 전체 값만 보여준다. 세션별 평균을 더하거나 다시 평균내면 값이 틀리기 때문이다.

### 손으로 쓰는 카드 (cards.py)

지표 카드로 안 되는 차트는 게임 폴더의 `cards.py`에 SQL로 쓴다. 파일이 있으면 `ptel push`가 함께 올린다.

```python
CARDS = {}

CARDS['코인 잔액 추이'] = """
SELECT t_ms / 1000.0    AS "경과(초)",
       cumulative_delta AS "누적 변동"
FROM v_coin_ledger
WHERE canonical = 1
  [[AND session_id = {{session_id}}]]
ORDER BY session_id, seq
"""

DISPLAY = {'코인 잔액 추이': 'line'}   # 없으면 table
SAMPLE_PARAMS = {}                    # 필수 변수의 표본값 (로컬 검사용)
```

`[[ ... {{변수}} ]]`는 Metabase의 선택적 필터다. 변수가 비면 그 블록이 통째로 빠진다.

## 12. 사용 예시

솔로 게임과 협동 게임 두 가지 경우를 처음부터 끝까지 따라간다. 이벤트 이름은 예시다.

### 예시 1. 솔로 퍼즐 게임: 어느 레벨이 어려운가

**게임 쪽.** 레벨이 끝날 때와 플레이어가 죽을 때 기록한다.

```csharp
void Start()
{
    Telemetry.Init(new TelemetryConfig {
        BuildVersion = Application.version,
        ConsentGiven = PlayerPrefs.GetInt("telemetry_consent", 0) == 1,
    });
    Telemetry.BeginSession(SessionMode.Solo);
}

void OnLevelEnd(int levelId, bool success, long elapsedMs)
{
    Telemetry.Record("level_completed",
        ("level_id", levelId), ("is_success", success), ("elapsed_ms", elapsedMs));
}

void OnPlayerDied(int levelId, string cause)
{
    Telemetry.Record("player_died", ("level_id", levelId), ("cause", cause));
}
```

**정의 파일.** `ptel init puzzle`로 만든 `puzzle/game.yaml`을 이렇게 고친다.

```yaml
metrics:
  - name: level_count
    type: count
    event: level_completed

  - name: level_cleared
    type: count
    event: level_completed
    where: { is_success: true }

  - name: clear_rate
    type: formula
    formula: level_cleared / level_count

  - name: avg_clear_time_ms
    type: avg
    event: level_completed
    param: elapsed_ms
    where: { is_success: true }
    by: level_id

  - name: deaths_by_level
    type: count
    event: player_died
    by: level_id
```

**실행과 조회.** 테스터들의 로그 파일을 `logs` 폴더에 모으고 돌린다.

```bash
ptel run logs --game puzzle --reset
```

```sql
-- 레벨별 사망 수 (전 세션 합)
SELECT dimension_value AS level_id, SUM(value) AS deaths
FROM v_metrics_dimension
WHERE metric_name = 'deaths_by_level'
GROUP BY 1 ORDER BY 2 DESC;

-- 사망 원인을 직접 보고 싶으면 이벤트 뷰를 쓴다
SELECT level_id, cause, COUNT(*) FROM v_player_died GROUP BY 1, 2;
```

나중에 "죽기 직전까지 걸린 시간"이 궁금해지면 `game.yaml`에 지표를 더하고 `ptel run --game puzzle --skip-load`만 다시 돌린다. 게임은 건드리지 않는다.

### 예시 2. 2인 협동 게임: 파티 골드가 어디에 쓰이는가

**게임 쪽.** 호스트가 세션을 열고, 참가자는 같은 식별자로 참가한다. 골드는 파티가 공유하고 서버가 잔액을 내려준다.

```csharp
// 호스트
var sessionId = $"{roomCode}_{DateTime.UtcNow:yyyyMMddHHmmss}";
Telemetry.BeginSession(SessionMode.Coop, sessionId, "p1");
SendToClients(sessionId);

// 참가자
Telemetry.JoinSession(receivedSessionId, "p2", hostOffsetMs: 0);

// 양쪽 모두: 서버가 골드 변동을 알릴 때
void OnGoldChanged(int delta, int balance, string reason)
{
    Telemetry.Resource("gold", delta, reason, balance);
}
```

돈을 쓴 플레이어의 클라이언트만 사유를 안다면, 나머지 클라이언트는 `reason`에 `"unknown"`을 넣는다. 병합 단계가 사유를 아는 쪽의 기록을 남긴다.

**정의 파일.** 지표와 병합 규칙을 함께 적는다.

```yaml
metrics:
  - name: party_gold_spent
    type: sum
    scope: party
    event: resource_changed
    param: delta
    where: { resource_id: gold, delta: { lt: 0 } }
    default: 0

  - name: gold_spent_by_reason
    type: sum
    scope: party
    event: resource_changed
    param: delta
    where: { resource_id: gold, delta: { lt: 0 } }
    by: reason

merge:
  shared_state:
    resource_changed:
      key: [resource_id, delta, balance]
      only: { resource_id: [gold] }
```

**실행과 확인.** 두 참가자의 로그 파일을 한 폴더에 넣고 돌린다.

```bash
ptel run logs --game coop --reset
ptel check --game coop
```

`ptel run`의 2단계에 "중복 N행 제외"가 나오고, `ptel check`에서 `resource_changed`가 "공유 상태 — 규칙 있음"으로 나오면 된 것이다. `? 규칙 없음`으로 남은 이벤트가 있으면 9장을 따라 규칙을 더한다.

```sql
-- 사유별 파티 지출
SELECT dimension_value AS reason, SUM(value) AS spent
FROM v_metrics_dimension
WHERE metric_name = 'gold_spent_by_reason'
GROUP BY 1 ORDER BY 2;
```

## 13. FAQ와 문제 해결

### 수집기

**로그 파일이 생기지 않는다.** `ConsentGiven`이 `false`가 아닌지 먼저 본다. 기본값이 `false`이고, 이때 콘솔에 "수집 동의가 없어 기록하지 않습니다" 경고가 뜬다. 그다음 `BeginSession`을 불렀는지, `Telemetry.IsRecording`이 `true`인지 확인한다. `VerboseLog = true`로 두면 출력 경로와 파일 이름이 콘솔에 나온다.

**로그 파일이 어디에 있는가.** `Telemetry.OutputPath`가 폴더를, `Telemetry.GetLogFiles()`가 파일 목록을 돌려준다. 기본 위치는 `Application.persistentDataPath/telemetry`다.

**"같은 세션·플레이어의 로그 파일이 이미 있어 이어 씁니다" 경고가 뜬다.** 같은 세션 식별자로 다시 시작했다는 뜻이다. 재접속이면 정상이다. 새 판인데 뜬다면 세션 식별자가 판마다 달라지지 않는 것이니, 방 번호 뒤에 날짜와 시각을 붙인다.

**파일 끝에 `session_ended`가 없다.** 게임이 강제 종료됐거나, 모바일에서 백그라운드 상태로 종료됐거나, WebGL 탭이 닫힌 경우다. 분석은 그대로 되고 `sessions.duration_ms`만 비어 있다.

**기록 호출이 게임을 느리게 하지 않는가.** 기록 호출은 큐에 넣기만 하고 돌아온다. 파일에 쓰는 일은 워커 스레드가 한다. 매 프레임 기록하는 값은 `EventTier.Sampled`로 두면 큐가 차도 메모리가 늘지 않는다.

**`Telemetry`가 네임스페이스로 인식되어 컴파일 오류가 난다.** 자기 코드가 `Playtest`로 시작하는 네임스페이스 안에 있을 때 생긴다. `Playtest.Telemetry.Telemetry.Record(...)`처럼 전체 이름으로 부르거나, `using PT = Playtest.Telemetry.Telemetry;`로 별칭을 둔다.

**WebGL에서 로그를 어떻게 받는가.** `DownloadOnSessionEnd = true`로 두면 `EndSession` 때 브라우저가 파일을 내려받는다. 원하는 시점에 직접 받으려면 `Telemetry.ExportLog(Telemetry.CurrentLogFile)`을 부른다.

### 분석 도구

**`ptel` 명령을 찾을 수 없다.** `pip install`한 파이썬의 Scripts(또는 bin) 폴더가 PATH에 없는 경우다. `python -m playtest_telemetry`로 같은 명령을 쓸 수 있다.

**"어느 게임인지 지정하라"에서 멈춘다.** `--game <폴더>`가 빠졌다. 게임 폴더가 없으면 `ptel init <폴더>`로 만든다.

**지표에 "※ 해당 이벤트가 로그에 없다"가 나온다.** `game.yaml`의 `event` 이름이 로그의 이벤트 이름과 다르다. 대소문자와 밑줄까지 같아야 한다. 실제 이름은 `SELECT DISTINCT name FROM events`로 본다.

**"모르는 키" 오류로 멈춘다.** `game.yaml`에 오타가 있다. 메시지가 비슷한 키를 제안한다(`'wehre' (혹시 'where'?)`).

**`[건너뜀] 이벤트 '...' — 뷰 이름으로 쓸 수 없다`가 나온다.** 이벤트 이름에 점이나 하이픈 같은 문자가 있다. 이벤트는 정상 적재되고 지표에도 쓸 수 있지만 `v_<이름>` 뷰만 만들어지지 않는다. 이름을 영문, 숫자, 밑줄로 바꾸면 뷰가 생긴다.

**협동 세션의 합계가 두 배로 나온다.** 병합 규칙이 없거나 그 이벤트가 규칙에서 빠졌다. `ptel run`의 `[경고]`를 확인하고 9장을 따라 `merge:`를 적는다.

**한 플레이어의 값이 통째로 빠진다.** 개인 자원이 공유 자원으로 판정된 경우다. `shared_state` 규칙에 `only`를 더해 공유 자원만 지정한다.

**지표 정의만 고쳤는데 전부 다시 적재해야 하는가.** 아니다. `ptel run --game mygame --skip-load`가 병합, 뷰, 지표만 다시 돌린다.

**Metabase가 옛 데이터를 보여준다.** DB 파일을 지우고 새로 만들면 생기는 일이다. `ptel run --reset`은 파일을 유지하고 내용만 바꾸므로 이 방식을 쓴다. 새 테이블이나 뷰가 안 보이면 Metabase에서 데이터베이스 동기화를 돌린다.

**`ptel push`가 "Metabase에 등록돼 있지 않다"고 한다.** Metabase 관리자 설정에서 `playtest.sqlite`를 먼저 데이터베이스로 추가해야 한다. 파일 이름으로 찾으므로 `--db`에 준 파일 이름과 등록한 파일 이름이 같아야 한다.

**Windows PowerShell에서 환경변수를 어떻게 설정하는가.** `$env:METABASE_URL = "http://localhost:3000"`처럼 쓴다. 그 창에서만 유효하므로 창을 새로 열면 다시 설정한다.

## 14. 버전, 라이선스, 알려진 제약

이 가이드는 버전 0.1.0 기준이다. 설치된 버전은 `ptel --version`으로 본다.

### 라이선스

수집기와 분석 도구는 MIT 라이선스다. 무료로 쓰고, 고치고, 상용 게임에 넣을 수 있다. 조건은 저작권 표시를 유지하는 것뿐이다.

Metabase는 이 도구에 포함되어 있지 않다. 사용자가 따로 설치하며 Metabase의 라이선스(오픈소스 에디션은 AGPL)를 따른다. 자세한 내용은 저장소의 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)에 있다.

### 개인정보

수집기는 게임이 `Record`로 넘긴 값만 기록한다. 자동으로 붙는 정보는 세션 식별자, 플레이어 식별자, 시각, 빌드 버전, 플랫폼 이름이다. 로그는 기기의 파일로만 남고 어디로도 전송되지 않는다. 플레이어의 동의를 받는 화면과 로그를 모으는 방법은 게임이 정한다.

### 알려진 제약

| 제약 | 내용 |
| --- | --- |
| 로그 전송 없음 | 로그를 서버로 보내는 기능이 없다. 파일을 직접 모아야 한다 |
| 모바일 공유 화면 없음 | 파일 경로만 제공한다. 공유는 게임의 기능을 써야 한다 |
| 플레이어 식별자 | `player_id`는 세션 안에서만 의미가 있다. 여러 세션에 걸쳐 같은 사람을 추적하려면 게임이 고정된 값을 `playerId`로 넘겨야 한다 |
| 시계 차이 | 협동 세션의 `t_ms`는 호스트 기준으로 자동 정렬된다. 다만 `clock_pairs`가 없으면 두 PC의 시계 차이를 잴 수 없어, 그 차이만큼 어긋난 채로 남는다(9장 "시간 정렬") |
| 행 단위 계산 | 같은 이벤트의 두 파라미터를 나누는 지표(`hp / max_hp`)는 정의할 수 없다. 게임에서 계산해 파라미터로 넘기거나 `views.sql`에 쓴다 |
| 메인 스레드 | 기록 호출은 메인 스레드에서만 지원한다 |
| WebGL 탭 닫기 | 탭을 닫을 때 마지막 주기의 기록과 `session_ended`가 빠질 수 있다 |

### 도움 받기

문제와 제안은 GitHub 저장소의 Issues에 남긴다: [github.com/qssxc/game-telemetry](https://github.com/qssxc/game-telemetry). 변경 내역은 저장소의 [`CHANGELOG.md`](CHANGELOG.md)에 있다.

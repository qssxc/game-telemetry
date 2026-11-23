# Playtest Telemetry — 수집기 (Unity 패키지)

설계서 2장(수집 SDK 설계)의 기록 부분을 구현한 것이다. 기록한 로그는 같은 저장소의
분석 도구(`ptel`)로 적재하고 지표를 계산한다.

## 설치

Unity 2021.3 이상. Package Manager 에서 *Add package from git URL* 을 고르고 아래 주소를 넣는다.

```
https://github.com/qssxc/game-telemetry.git?path=unity
```

```
unity/
├── package.json
├── Runtime/        ← SDK 본체 (어셈블리 Playtest.Telemetry)
└── Samples~/       ← 동작 확인용. Package Manager 의 Samples 에서 가져온다
```

예전처럼 `Assets/` 아래에 SDK 를 복사해 쓰고 있었다면 그 폴더를 지운 뒤 설치한다.
같은 스크립트가 두 벌이면 컴파일 오류가 난다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `Telemetry.cs` | 공개 API. 설계서 2.1 ~ 2.2 의 함수 |
| `TelemetryTypes.cs` | 설정, 이벤트 구조체, 등급·모드 열거형 |
| `EventQueue.cs` | 게임 스레드와 워커 스레드가 공유하는 큐. 등급별 버퍼 정책 |
| `EventWriter.cs` | 워커 스레드. 주기적 배치 기록 |
| `NdjsonSerializer.cs` | 이벤트 → NDJSON 한 줄 |
| `SessionContext.cs` | 세션 상태, 모노토닉 클럭, 순번 발급 |
| `TelemetryLog.cs` | 내부 로그 |

## 사용

```csharp
// 게임 시작 시 1회
Telemetry.Init(new TelemetryConfig {
    BuildVersion = Application.version,
    ConsentGiven = true,          // 수집 동의를 받은 뒤 true
});

// 호스트
string sessionId = Telemetry.BeginSession(SessionMode.Coop);
// → sessionId 를 네트워크 계층으로 참가자에게 전달

// 클라이언트
Telemetry.JoinSession(sessionId, playerId, hostOffsetMs);

// 기록
Telemetry.Record("item_purchased", ("item_id", "golden_pickaxe"), ("price", 800));
Telemetry.Record("player_died", "combat", ("stage", 3), ("cause", "fire_zone"));
Telemetry.Record("player_position", EventTier.Sampled, ("x", 12.4f), ("y", 3.1f));
Telemetry.Resource("gold", +150, "enemy_kill");

// 종료
Telemetry.EndSession();
```

## 출력

`Application.persistentDataPath/telemetry/session_{세션ID}_{플레이어ID}.ndjson`

세션ID·플레이어ID 에 영문·숫자·`-`·`_` 가 아닌 글자가 있거나 64자를 넘으면, 그 글자를
`_` 로 바꾸고 원래 값의 해시 8자리를 붙인다(예: `보스방_1120` → `session_____1120-17b55fd9_p1.ndjson`).
해시가 없으면 `보스방_1120` 과 `던전방_1120` 이 같은 파일 이름이 되어 두 판이 한 파일에
섞인다. 영문·숫자만으로 된 ID(기본 GUID 포함)는 이름이 그대로다. 로그 안의 `session_id` 는
언제나 원래 값이다.

같은 세션·플레이어로 다시 참가하면(재접속) 같은 파일에 이어 쓴다. `seq` 와 함께 `t_ms` 도
앞 구간에서 이어 가며, 끊겨 있던 동안도 세션 시간에 들어간다. 그래서 마지막 `session_ended`
의 `duration_ms` 가 처음 시작부터의 전체 길이가 된다.

한 줄에 이벤트 하나가 들어간다.

```json
{"session_id":"...","player_id":"p01","seq":12,"t_ms":410100,"t_raw_ms":410100,
 "wall_clock":"2026-10-07T14:02:10.100Z","name":"item_purchased","tag":"economy",
 "tier":"critical","build_version":"0.4.2",
 "params":{"item_id":"golden_pickaxe","price":800}}
```

## 동작 확인

Package Manager 에서 이 패키지를 고르고 Samples 의 *Smoke Test* 를 Import 한다.
`Assets/Samples/Playtest Telemetry/<버전>/Smoke Test/TelemetrySmokeTest.cs` 가 생기면
빈 씬의 게임오브젝트에 붙이고 재생한다.
화면 좌측 상단 버튼으로 조작하며, 키보드 1~5 로도 같은 동작을 할 수 있다.

| 번호 | 동작 |
|---|---|
| 1 | 사건 기록 |
| 2 | 리소스 변동 기록 |
| 3 | 부하 테스트 (호출당 소요 시간 출력) |
| 4 | 세션 종료 |
| 5 | 로그 파일 목록 출력 |

비정상 종료 대응은 3번으로 이벤트를 쌓은 뒤 에디터를 강제 종료하고, 파일에 직전 기록이
남아 있는지 확인하면 된다.

### 입력 시스템

샘플은 구 Input Manager 와 Input System 패키지 양쪽에서 동작한다.
`ENABLE_INPUT_SYSTEM` / `ENABLE_LEGACY_INPUT_MANAGER` 정의로 분기하며,
둘 다 꺼져 있어도 화면 버튼으로 모든 동작을 확인할 수 있다.

샘플을 별도 어셈블리(asmdef)가 적용된 폴더로 옮기는 경우, Input System 을 쓰려면
그 asmdef 에 `Unity.InputSystem` 참조를 추가해야 한다. Import 한 기본 위치(`Assets/Samples/`)는
`Assembly-CSharp` 에 속하므로 추가 설정이 필요 없다.

SDK 본체(`Runtime/`)는 입력을 전혀 사용하지 않는다.

## 현재 범위

**구현됨**

- 생명주기 함수 전체
- `Record` 오버로드 4종, `Resource`
- 파라미터 값: 정수·실수 타입 전부(`uint` `ulong` `decimal` 포함)는 JSON 숫자로, `bool` 과 `string` 은 그대로, 그 외(열거형 등)는 문자열로
- 자동 메타데이터 부착 (`session_id`, `player_id`, `t_ms`, `t_raw_ms`, `wall_clock`, `seq`, `build_version`)
- 비동기 큐 + 워커 스레드 + NDJSON append
- 등급별 버퍼 정책, 폐기 횟수 집계
- `Enabled` / `ConsentGiven` 가 false 이면 전체 무동작

**미구현 (다음 단계)**

- `Sample` 과 `[Track]` 어트리뷰트 — 인스턴스 식별 문제로 보류
- 미전송 로그 재전송
- 시계 보정 적용 — 현재 `t_ms` 와 `t_raw_ms` 가 같은 값이며, 보정은 병합 단계에서 수행한다

## 주의

- 기록 호출은 게임 메인 스레드에서만 지원한다. 다른 스레드에서 호출하면 개발 빌드에서 경고가 출력된다.
- `Record(name, tag)` 와 `Record(name, 파라미터)` 는 두 번째 인자 타입으로 구분된다.
  파라미터 없이 문자열 하나만 넘기면 태그로 처리되며, 개발 빌드에서 경고가 나온다.
- 파일 기록 시 `fsync` 를 호출하지 않는다. 프로세스가 죽어도 기록은 남지만,
  전원이 끊기면 마지막 몇 초가 유실될 수 있다.

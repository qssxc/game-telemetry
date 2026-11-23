using System;
using System.Collections.Generic;

namespace Playtest.Telemetry
{
    /// <summary>이벤트 등급. 버퍼가 가득 찼을 때의 처리 방식을 결정한다.</summary>
    public enum EventTier
    {
        /// <summary>개별 발생이 의미를 갖는 사건. 버퍼를 확장해서라도 보존한다.</summary>
        Critical = 0,

        /// <summary>통계적으로만 의미를 갖는 값. 버퍼 초과 시 폐기한다.</summary>
        Sampled = 1,
    }

    /// <summary>세션의 플레이 형태.</summary>
    public enum SessionMode
    {
        Solo = 0,
        Coop = 1,
    }

    /// <summary>수집 SDK 설정.</summary>
    public sealed class TelemetryConfig
    {
        /// <summary>로그 파일을 쓸 디렉터리. 비워 두면 Application.persistentDataPath/telemetry 를 사용한다.</summary>
        public string OutputPath;

        /// <summary>클라이언트 식별자. 비워 두면 기기별로 생성해 보존한다.</summary>
        public string PlayerId;

        /// <summary>빌드 식별자.</summary>
        public string BuildVersion = "0.0.0";

        /// <summary>워커 스레드가 큐를 비우는 주기(초).</summary>
        public float FlushIntervalSec = 2f;

        /// <summary>큐 용량. Sampled 등급은 이 수를 넘으면 폐기된다.</summary>
        public int QueueCapacity = 4096;

        /// <summary>false 이면 모든 기록 호출이 아무 동작도 하지 않는다.</summary>
        public bool Enabled = true;

        /// <summary>수집 동의 여부. false 이면 기록하지 않는다.</summary>
        public bool ConsentGiven = false;

        /// <summary>true 이면 내부 동작을 Unity 콘솔에 출력한다.</summary>
        public bool VerboseLog = false;

        /// <summary>
        /// true(기본)면 워커 스레드가 파일에 쓴다. false 면 스레드 없이, 게임 스레드에서
        /// 주기마다 조금씩 쓴다(Telemetry.Tick). WebGL 은 C# 스레드를 쓸 수 없으므로
        /// 이 값과 무관하게 항상 스레드 없이 동작한다.
        /// </summary>
        public bool UseWorkerThread = true;

        /// <summary>
        /// WebGL 전용. true 면 세션이 끝날 때 로그 파일을 브라우저 다운로드로 내보낸다.
        /// WebGL 의 로그는 브라우저 내부 저장소에만 남아 달리 꺼낼 방법이 없다.
        /// false 로 두면 게임이 알맞은 때에 Telemetry.ExportLog 를 직접 불러야 한다.
        /// </summary>
        public bool DownloadOnSessionEnd = false;
    }

    /// <summary>기록된 이벤트 한 건.</summary>
    public sealed class TelemetryEvent
    {
        public string SessionId;
        public string PlayerId;
        public int Seq;

        /// <summary>세션 시작 기준 경과 시간(ms). 병합 단계에서 보정된다.</summary>
        public long TMs;

        /// <summary>보정 전 원본 경과 시간(ms).</summary>
        public long TRawMs;

        /// <summary>UTC 절대 시각. 참고용.</summary>
        public DateTime WallClockUtc;

        public string Name;
        public string Tag;
        public EventTier Tier;
        public string BuildVersion;

        /// <summary>이벤트별 파라미터. 직렬화 시 params 필드가 된다.</summary>
        public readonly List<KeyValuePair<string, object>> Params =
            new List<KeyValuePair<string, object>>(8);

        internal void Reset()
        {
            SessionId = null;
            PlayerId = null;
            Seq = 0;
            TMs = 0;
            TRawMs = 0;
            WallClockUtc = default;
            Name = null;
            Tag = null;
            Tier = EventTier.Critical;
            BuildVersion = null;
            Params.Clear();
        }
    }
}

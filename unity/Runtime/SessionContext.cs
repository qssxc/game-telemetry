using System;
using System.Diagnostics;
using System.Threading;

namespace Playtest.Telemetry
{
    /// <summary>
    /// 한 세션 동안 유지되는 상태. 설계서 2.3 의 자동 부착 메타데이터를 공급한다.
    /// </summary>
    internal sealed class SessionContext
    {
        private readonly Stopwatch _clock = new Stopwatch();
        private int _seq;

        // 재접속해 같은 파일에 이어 쓸 때, 앞 구간까지 흐른 시간(ms). 처음 시작이면 0.
        private long _baseMs;

        public string SessionId { get; private set; }
        public string PlayerId { get; private set; }
        public SessionMode Mode { get; private set; }
        public bool IsHost { get; private set; }

        /// <summary>호스트 기준 시계 차이(ms). 병합 단계에서 보정에 사용한다.</summary>
        public long HostOffsetMs { get; private set; }

        public DateTime StartedAtUtc { get; private set; }

        /// <summary>
        /// 호스트로 세션을 시작한다. sessionId 를 지정하지 않으면 새로 생성한다.
        /// 게임의 네트워크 계층이 이미 공유 식별자(방 번호 등)를 갖고 있는 경우,
        /// 그것을 그대로 전달하면 참가자에게 따로 알릴 필요가 없다.
        /// </summary>
        public static SessionContext BeginAsHost(string playerId, SessionMode mode,
                                                 string sessionId = null)
        {
            var ctx = new SessionContext
            {
                SessionId = string.IsNullOrEmpty(sessionId)
                            ? Guid.NewGuid().ToString("D")
                            : sessionId,
                PlayerId = playerId,
                Mode = mode,
                IsHost = true,
                HostOffsetMs = 0,
            };
            ctx.StartClock();
            return ctx;
        }

        public static SessionContext JoinAsClient(string sessionId, string playerId, long hostOffsetMs)
        {
            var ctx = new SessionContext
            {
                SessionId = sessionId,
                PlayerId = playerId,
                Mode = SessionMode.Coop,
                IsHost = false,
                HostOffsetMs = hostOffsetMs,
            };
            ctx.StartClock();
            return ctx;
        }

        private void StartClock()
        {
            StartedAtUtc = DateTime.UtcNow;
            _clock.Start();
        }

        /// <summary>
        /// 세션 시작 기준 경과 시간(ms).
        /// 시스템 시계가 아니라 모노토닉 클럭을 사용한다. 사용자 시간 변경이나
        /// 절전 복귀의 영향을 받지 않아야 이벤트 순서가 역전되지 않는다.
        /// </summary>
        public long ElapsedMs { get { return _baseMs + _clock.ElapsedMilliseconds; } }

        public DateTime NowUtc { get { return StartedAtUtc.AddMilliseconds(ElapsedMs); } }

        /// <summary>클라이언트별로 유일한 기록 순번을 발급한다.</summary>
        public int NextSeq() { return Interlocked.Increment(ref _seq); }

        /// <summary>
        /// 같은 세션·플레이어의 로그 파일에 이어 쓸 때, 앞서 기록된 마지막 순번을
        /// 알려준다. 다음 순번은 그 뒤부터 발급된다. 첫 이벤트를 기록하기 전에만 호출한다.
        /// </summary>
        public void ContinueSeqAfter(int lastSeq)
        {
            if (lastSeq > _seq) Interlocked.Exchange(ref _seq, lastSeq);
        }

        /// <summary>
        /// 같은 파일에 이어 쓸 때 시간도 앞 구간에서 이어 간다. 첫 이벤트를 기록하기
        /// 전에만 호출한다.
        ///
        /// 이어 가지 않으면 재접속 직후 t_ms 가 0 으로 되돌아가고, session_ended 의
        /// duration_ms 는 마지막 구간의 길이만 담는다. 분석 쪽은 마지막 session_ended
        /// 를 세션 길이로 읽으므로 세션이 실제보다 짧게 남는다.
        ///
        /// 처음 시작한 시각을 (마지막 줄의 wall_clock − t_ms) 로 되살리고, 지금까지
        /// 흐른 시간을 그 시각부터 잰다. 끊겨 있던 동안도 세션 시간에 들어간다.
        /// 시스템 시계가 뒤로 돌아가 있어도 t_ms 가 앞 구간보다 작아지지는 않는다.
        /// </summary>
        public void ContinueClockAfter(long lastTMs, DateTime lastWallClockUtc)
        {
            if (lastTMs < 0) return;
            var originalStart = lastWallClockUtc.AddMilliseconds(-lastTMs);
            var now = DateTime.UtcNow;
            long sinceStart = (long)(now - originalStart).TotalMilliseconds;
            _baseMs = Math.Max(lastTMs, sinceStart);
            StartedAtUtc = now.AddMilliseconds(-_baseMs);
            _clock.Restart();
        }

        public void StopClock() { _clock.Stop(); }
    }
}

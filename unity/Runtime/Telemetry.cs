using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;

namespace Playtest.Telemetry
{
    /// <summary>
    /// 수집 SDK 의 공개 진입점. 설계서 2장에 정의된 함수를 제공한다.
    ///
    /// 모든 기록 호출은 게임 메인 스레드에서 하는 것을 전제로 하며, 큐에 적재만 하고
    /// 즉시 반환한다. 직렬화와 파일 입출력은 워커 스레드가 담당한다.
    /// </summary>
    public static class Telemetry
    {
        private static TelemetryConfig _config;
        private static SessionContext _session;
        private static EventQueue _queue;
        private static EventWriter _writer;

        private static int _mainThreadId;
        private static bool _initialized;
        private static bool _threadWarned;
        private static bool _notRecordingWarned;
        private static bool _webglExportHintShown;
        private static readonly HashSet<string> _warnedParamKeys = new HashSet<string>();

        /// <summary>현재 세션 식별자. 세션이 없으면 null.</summary>
        public static string CurrentSessionId
        {
            get { return _session != null ? _session.SessionId : null; }
        }

        /// <summary>기록이 실제로 수행되는 상태인지.</summary>
        public static bool IsRecording
        {
            get
            {
                // _writer 가 없으면 큐를 비워 줄 워커도 없다. 그 상태에서 기록을 받으면
                // 이벤트가 메모리에만 쌓인다(동의 없이 시작한 세션, 파일을 못 연 세션).
                return _initialized && _session != null && _writer != null
                       && _config != null && _config.Enabled && _config.ConsentGiven;
            }
        }

        /// <summary>버퍼 초과로 폐기된 이벤트 수.</summary>
        public static int DroppedCount
        {
            get { return _queue != null ? _queue.DroppedCount : 0; }
        }

        /// <summary>로그 파일이 쌓이는 폴더. Init 전에는 null.</summary>
        public static string OutputPath
        {
            get { return _config != null ? _config.OutputPath : null; }
        }

        /// <summary>지금 기록 중인 세션의 로그 파일 경로. 기록 중이 아니면 null.</summary>
        public static string CurrentLogFile
        {
            get { return _writer != null ? _writer.FilePath : null; }
        }

        // ------------------------------------------------------------------ 생명주기

        /// <summary>게임 시작 시 1회 호출한다. 큐를 준비하고 설정을 적용한다.</summary>
        public static void Init(TelemetryConfig config)
        {
            if (_initialized)
            {
                TelemetryLog.Warn("이미 초기화되어 있습니다. Init 호출을 무시합니다.");
                return;
            }

            _config = config ?? new TelemetryConfig();
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;

            if (string.IsNullOrEmpty(_config.OutputPath))
            {
                _config.OutputPath = DefaultOutputPath();
            }

            // 플레이어 식별자와 큐는 여기서 만들지 않는다. 식별자를 기기에 저장하는
            // 것은 수집 동의가 있을 때만 해야 하고(세션 시작 시), 큐는 세션마다
            // 새로 만든다(StartWriter).
            _initialized = true;

            if (_config.VerboseLog)
            {
                TelemetryLog.Info($"초기화 완료. 출력 경로: {_config.OutputPath}");
            }
        }

        /// <summary>
        /// 호스트가 호출한다. 세션 식별자를 생성해 반환하며, 반환값을 게임의 네트워크
        /// 계층을 통해 참가자에게 전달해야 한다.
        ///
        /// 게임이 이미 참가자 전원이 아는 공유 식별자(방 번호 등)를 갖고 있다면
        /// sessionId 에 그 값을 넘겨 전달 절차를 생략할 수 있다. 그 값은 **판마다
        /// 달라야 한다** — 같은 값을 다시 쓰면 앞 판의 기록 뒤에 이어 붙어 한 세션으로
        /// 분석된다(방 번호만 쓰지 말고 날짜·시각을 붙인다).
        /// playerId 는 지정하지 않으면 설정값을 사용한다.
        ///
        /// 수집이 꺼져 있거나(Enabled) 동의가 없어도(ConsentGiven) 세션 식별자는
        /// 반환한다 — 이 기기는 기록하지 않더라도 동의한 참가자에게 전달할 값이
        /// 필요하기 때문이다. 그 경우 파일도 기기 식별자도 만들지 않는다.
        /// 동의 여부는 이 호출 전에 설정되어 있어야 한다.
        /// </summary>
        public static string BeginSession(SessionMode mode, string sessionId = null,
                                          string playerId = null)
        {
            if (!EnsureInitialized()) return null;
            if (_session != null)
            {
                TelemetryLog.Warn("이미 세션이 열려 있습니다. 기존 세션을 종료한 뒤 시작합니다.");
                EndSession();
            }

            _session = SessionContext.BeginAsHost(ResolvePlayerId(playerId), mode, sessionId);
            var id = _session.SessionId;
            if (StartWriter()) RecordSessionStarted();
            return id;
        }

        /// <summary>
        /// 클라이언트가 호출한다. 호스트에게 받은 세션 식별자로 기록을 시작한다.
        /// hostOffsetMs 에는 네트워크 계층이 제공하는 시계 차이를 전달한다.
        /// </summary>
        public static void JoinSession(string sessionId, string playerId, long hostOffsetMs)
        {
            if (!EnsureInitialized()) return;
            if (string.IsNullOrEmpty(sessionId))
            {
                TelemetryLog.Error("sessionId 가 비어 있어 세션에 참가할 수 없습니다.");
                return;
            }
            if (_session != null)
            {
                TelemetryLog.Warn("이미 세션이 열려 있습니다. 기존 세션을 종료한 뒤 참가합니다.");
                EndSession();
            }

            _session = SessionContext.JoinAsClient(sessionId, ResolvePlayerId(playerId), hostOffsetMs);
            if (StartWriter()) RecordSessionStarted();
        }

        /// <summary>세션 종료 시 호출한다. 큐에 남은 이벤트를 모두 기록하고 파일을 마감한다.</summary>
        public static void EndSession()
        {
            if (_session == null) return;

            Record("session_ended",
                ("duration_ms", _session.ElapsedMs),
                ("dropped_count", DroppedCount));

            _session.StopClock();

            if (_writer != null)
            {
                var finished = _writer.FilePath;
                _writer.Stop();
                if (_config != null && _config.VerboseLog)
                {
                    TelemetryLog.Info($"세션 종료. 기록 {_writer.WrittenCount}건, 폐기 {DroppedCount}건");
                }
                _writer = null;

                // WebGL 의 로그는 브라우저 안에만 있다. 설정돼 있으면 여기서 내보낸다.
                if (_config != null && _config.DownloadOnSessionEnd && IsWebGL)
                {
                    ExportLog(finished);
                }
            }

            // 큐는 세션마다 새로 만든다. 여기서 놓아 두면, 제한 시간 안에 끝나지 못한
            // 워커가 남아 있더라도 다음 세션의 이벤트에는 손대지 못한다.
            _queue = null;
            _session = null;
        }

        /// <summary>애플리케이션 종료 시 호출한다.</summary>
        public static void Shutdown()
        {
            EndSession();
            _initialized = false;
            _queue = null;
            _config = null;
        }

        // ------------------------------------------------------------------ 기록

        /// <summary>사건을 기록한다.</summary>
        public static void Record(string eventName, params (string, object)[] parameters)
        {
            RecordInternal(eventName, null, EventTier.Critical, parameters);
        }

        /// <summary>사건을 분류 태그와 함께 기록한다.</summary>
        public static void Record(string eventName, string tag, params (string, object)[] parameters)
        {
            WarnIfTagLooksLikeParam(eventName, tag, parameters);
            RecordInternal(eventName, tag, EventTier.Critical, parameters);
        }

        /// <summary>사건을 등급을 지정해 기록한다.</summary>
        public static void Record(string eventName, EventTier tier, params (string, object)[] parameters)
        {
            RecordInternal(eventName, null, tier, parameters);
        }

        /// <summary>사건을 태그와 등급을 모두 지정해 기록한다.</summary>
        public static void Record(string eventName, string tag, EventTier tier,
                                  params (string, object)[] parameters)
        {
            RecordInternal(eventName, tag, tier, parameters);
        }

        /// <summary>
        /// 리소스(재화 등)의 변동을 기록한다.
        /// 보유량이 아니라 변동량과 사유를 남긴다. 사유가 없으면 특정 원인의 기여분을
        /// 분리할 수 없으므로 reason 은 필수 인자이다.
        /// </summary>
        public static void Resource(string resourceId, int delta, string reason, int? balance = null)
        {
            if (!IsRecording) return;

            if (string.IsNullOrEmpty(reason))
            {
                TelemetryLog.Warn($"리소스 '{resourceId}' 변동에 reason 이 비어 있습니다.");
            }

            // 태그는 게임 정의 파일에서 선언하는 값이므로 SDK 가 임의로 붙이지 않는다.
            var e = NewEvent("resource_changed", null, EventTier.Critical);
            if (e == null) return;

            e.Params.Add(new KeyValuePair<string, object>("resource_id", resourceId));
            e.Params.Add(new KeyValuePair<string, object>("delta", delta));
            e.Params.Add(new KeyValuePair<string, object>("reason", reason));
            if (balance.HasValue)
            {
                e.Params.Add(new KeyValuePair<string, object>("balance", balance.Value));
            }

            _queue.Enqueue(e);
        }

        /// <summary>큐를 즉시 비우도록 워커 스레드를 깨운다. 호출자는 기다리지 않는다.</summary>
        public static void Flush()
        {
            if (_writer != null) _writer.RequestFlush();
        }

        /// <summary>
        /// 지금까지 기록한 이벤트가 파일에 들어갈 때까지 기다린다(최대 timeoutMs).
        /// 다 들어갔으면 true. 게임 스레드를 잠깐 세우므로 평소에는 Flush 를 쓴다.
        ///
        /// 앱이 백그라운드로 갈 때 SDK 가 자동으로 부른다. 모바일은 그 뒤에 OS 가
        /// 예고 없이 앱을 끝낼 수 있고, 그때는 종료 처리(session_ended)도 오지 않는다.
        /// </summary>
        public static bool FlushNow(int timeoutMs = 500)
        {
            return _writer == null || _writer.FlushNow(timeoutMs);
        }

        /// <summary>
        /// 스레드 없이 도는 방식(WebGL, 또는 UseWorkerThread = false)에서 주기마다
        /// 파일에 쓰는 일을 한다. Unity 에서는 SDK 가 프레임마다 자동으로 부르므로
        /// 게임이 부를 필요가 없다. 스레드 방식에서는 아무 일도 하지 않는다.
        /// </summary>
        public static void Tick()
        {
            if (_writer != null) _writer.Pump();
        }

        // ------------------------------------------------------------------ 로그 꺼내기

        /// <summary>
        /// 출력 폴더에 있는 로그 파일의 경로. 최근에 쓴 것이 앞에 온다.
        /// 게임이 "로그 보내기" 화면을 만들 때 쓴다(메일 첨부, 공유 등).
        /// </summary>
        public static string[] GetLogFiles()
        {
            var dir = OutputPath;
            try
            {
                if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) return new string[0];
                var files = Directory.GetFiles(dir, "*.ndjson");
                Array.Sort(files, (a, b) =>
                    File.GetLastWriteTimeUtc(b).CompareTo(File.GetLastWriteTimeUtc(a)));
                return files;
            }
            catch (Exception ex)
            {
                TelemetryLog.Warn("로그 파일 목록을 읽지 못했습니다: " + ex.Message);
                return new string[0];
            }
        }

        /// <summary>
        /// 로그 파일을 기기 밖으로 내보낸다. 내보냈으면 true.
        ///
        ///   WebGL     브라우저의 파일 다운로드로 내보낸다. 로그가 브라우저 내부
        ///             저장소에만 있어 이것 말고는 꺼낼 방법이 없다.
        ///   그 외     아무 일도 하지 않고 false 를 돌려준다. 파일이 이미 path 에
        ///             있으므로 그대로 복사하거나, 모바일이면 게임이 쓰는 공유 기능에
        ///             path 를 넘기면 된다.
        ///
        /// 기록 중인 세션의 파일도 내보낼 수 있다. 그때까지 기록된 것이 담긴다.
        /// </summary>
        public static bool ExportLog(string path)
        {
            if (string.IsNullOrEmpty(path)) return false;
            if (!IsWebGL) return false;

            try
            {
                if (_writer != null && path == _writer.FilePath) _writer.FlushNow(500);

                byte[] data;
                // 기록 중인 파일은 쓰기용으로 열려 있다. 공유 모드를 넓혀야 읽힌다.
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read,
                                               FileShare.ReadWrite))
                {
                    data = new byte[fs.Length];
                    int read = 0;
                    while (read < data.Length)
                    {
                        int n = fs.Read(data, read, data.Length - read);
                        if (n <= 0) break;
                        read += n;
                    }
                }
                BrowserDownload(Path.GetFileName(path), data);
                return true;
            }
            catch (Exception ex)
            {
                TelemetryLog.Warn("로그를 내보내지 못했습니다: " + ex.Message);
                return false;
            }
        }

        // ------------------------------------------------------------------ 내부

        private static void RecordInternal(string eventName, string tag, EventTier tier,
                                           (string, object)[] parameters)
        {
            if (!IsRecording) return;
            if (string.IsNullOrEmpty(eventName))
            {
                TelemetryLog.Warn("eventName 이 비어 있어 기록하지 않습니다.");
                return;
            }

            var e = NewEvent(eventName, tag, tier);
            if (e == null) return;

            if (parameters != null)
            {
                for (int i = 0; i < parameters.Length; i++)
                {
                    var key = parameters[i].Item1;
                    var value = parameters[i].Item2;

                    if (string.IsNullOrEmpty(key)) continue;
                    ValidateParamType(eventName, key, value);
                    e.Params.Add(new KeyValuePair<string, object>(key, value));
                }
            }

            _queue.Enqueue(e);
        }

        private static TelemetryEvent NewEvent(string name, string tag, EventTier tier)
        {
            WarnIfOffMainThread();

            var e = _queue.Rent();
            var raw = _session.ElapsedMs;

            // 설계서 2.3 — 아래 값은 SDK 가 자동으로 부착한다.
            e.SessionId = _session.SessionId;
            e.PlayerId = _session.PlayerId;
            e.Seq = _session.NextSeq();
            e.TRawMs = raw;
            e.TMs = raw;              // 보정은 병합 단계에서 수행한다.
            e.WallClockUtc = _session.NowUtc;
            e.Name = name;
            e.Tag = string.IsNullOrEmpty(tag) ? null : tag;
            e.Tier = tier;
            e.BuildVersion = _config.BuildVersion;
            return e;
        }

        private static void RecordSessionStarted()
        {
            Record("session_started",
                ("mode", _session.Mode == SessionMode.Coop ? "coop" : "solo"),
                ("is_host", _session.IsHost),
                ("clock_offset_ms", _session.HostOffsetMs),
                // InvariantCulture 를 빼면 기기의 달력 설정을 탄다(불기·히즈라력 등).
                ("started_at", _session.StartedAtUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffZ",
                                                              CultureInfo.InvariantCulture)),
                ("platform", PlatformName()));
        }

        /// <summary>
        /// 세션의 큐와 워커를 준비한다. 기록을 시작했으면 true.
        ///
        /// false 를 돌려주는 경우는 둘이고, 어느 쪽이든 예외를 내지 않는다.
        ///   - 수집이 꺼져 있거나 동의가 없다. 파일도 스레드도 만들지 않는다.
        ///   - 파일을 열지 못했다(경로, 권한, 디스크). 기록 실패가 게임을 멈추게
        ///     해서는 안 되므로 경고만 남긴다.
        /// 이때 _writer 가 null 로 남아 IsRecording 이 false 가 되고, 이후의 기록
        /// 호출은 아무 일도 하지 않는다.
        /// </summary>
        private static bool StartWriter()
        {
            if (!_config.Enabled || !_config.ConsentGiven)
            {
                if (!_notRecordingWarned)
                {
                    _notRecordingWarned = true;
                    TelemetryLog.Warn(_config.Enabled
                        ? "수집 동의(TelemetryConfig.ConsentGiven)가 없어 기록하지 않습니다."
                        : "수집이 꺼져 있어(TelemetryConfig.Enabled) 기록하지 않습니다.");
                }
                return false;
            }

            var fileName = $"session_{SafeFileNamePart(_session.SessionId)}_"
                           + $"{SafeFileNamePart(_session.PlayerId)}.ndjson";
            var path = Path.Combine(_config.OutputPath, fileName);

            // WebGL 은 C# 스레드를 쓸 수 없다. 설정과 무관하게 틱 방식으로 돈다.
            bool threaded = _config.UseWorkerThread && !IsWebGL;

            var queue = new EventQueue(_config.QueueCapacity);
            var writer = new EventWriter(queue, path, _config.FlushIntervalSec, _config.VerboseLog,
                                         threaded);
            if (IsWebGL)
            {
                // 파일에 쓴 것은 메모리 안의 가상 파일시스템에만 있다. 브라우저 저장소에
                // 반영해 두어야 페이지를 닫았다 열어도 남는다.
                writer.AfterWrite = BrowserSyncFiles;

                if (!_config.DownloadOnSessionEnd && !_webglExportHintShown)
                {
                    _webglExportHintShown = true;
                    TelemetryLog.Info("WebGL 의 로그는 브라우저 안에만 남습니다. 꺼내려면 "
                                      + "Telemetry.ExportLog 를 부르거나 "
                                      + "TelemetryConfig.DownloadOnSessionEnd 를 켜세요.");
                }
            }
            try
            {
                writer.Start();
            }
            catch (Exception ex)
            {
                TelemetryLog.Warn($"로그 파일을 열지 못해 이 세션은 기록하지 않습니다: {path} ({ex.Message})");
                return false;
            }

            _queue = queue;
            _writer = writer;

            if (writer.LastSeqInFile > 0)
            {
                // 재접속이면 정상이다. 아니라면 sessionId 가 판마다 다르지 않은 것이다.
                // 순번과 함께 시간도 잇는다 — t_ms 가 0 으로 되돌아가지 않게.
                _session.ContinueSeqAfter(writer.LastSeqInFile);
                _session.ContinueClockAfter(writer.LastInFile.TMs, writer.LastInFile.WallClockUtc);
                TelemetryLog.Warn($"같은 세션·플레이어의 로그 파일이 이미 있어 이어 씁니다 "
                                  + $"(seq {writer.LastSeqInFile} 다음부터): {path}\n"
                                  + "재접속이 아니라 새 판이라면 sessionId 가 판마다 달라야 합니다.");
            }

            if (_config.VerboseLog)
            {
                TelemetryLog.Info("로그 파일: " + path);
            }
            return true;
        }

        /// <summary>
        /// 식별자를 파일 이름에 넣을 수 있는 모양으로 바꾼다. 로그 안의 값은 그대로다.
        ///
        /// sessionId 는 게임이 넘겨주는 값이고 방 이름처럼 사용자가 입력한 것일 수
        /// 있다. 경로 구분자나 ".." 이 섞이면 파일을 못 만들거나 출력 폴더 밖에
        /// 파일이 생긴다. 영문·숫자·'-'·'_' 만 남기고 나머지는 '_' 로 바꾼다.
        ///
        /// 바꾸거나 잘라 낸 글자가 있으면 원래 값의 해시를 뒤에 붙인다. 붙이지 않으면
        /// 서로 다른 값이 같은 이름이 된다 — "보스방_1120" 과 "던전방_1120" 이 둘 다
        /// "____1120" 이 되어, 다른 판의 기록이 한 파일에 이어 붙는다. 바꿀 것이 없는
        /// 값(GUID, 영문 방 이름)은 지금과 똑같은 이름이 나온다.
        /// </summary>
        internal static string SafeFileNamePart(string value)
        {
            if (string.IsNullOrEmpty(value)) return "unknown";

            const int maxLength = 64;
            var sb = new StringBuilder(Math.Min(value.Length, maxLength) + 9);
            bool changed = value.Length > maxLength;
            for (int i = 0; i < value.Length && sb.Length < maxLength; i++)
            {
                char c = value[i];
                bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                          || (c >= '0' && c <= '9') || c == '-' || c == '_';
                if (!ok) changed = true;
                sb.Append(ok ? c : '_');
            }
            if (changed)
            {
                sb.Append('-');
                sb.Append(StableHash(value).ToString("x8", CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }

        /// <summary>
        /// 실행·기기·런타임이 달라도 같은 값을 내는 32비트 해시(FNV-1a, UTF-16 단위).
        /// string.GetHashCode 는 .NET 에서 실행마다 달라질 수 있어 파일 이름에 못 쓴다
        /// — 재접속했을 때 앞 파일을 찾지 못한다.
        /// </summary>
        private static uint StableHash(string value)
        {
            uint hash = 2166136261;
            for (int i = 0; i < value.Length; i++)
            {
                hash ^= value[i];
                hash *= 16777619;
            }
            return hash;
        }

        private static bool EnsureInitialized()
        {
            if (_initialized) return true;
            TelemetryLog.Error("Init 을 먼저 호출해야 합니다.");
            return false;
        }

        private static void WarnIfOffMainThread()
        {
#if UNITY_EDITOR || DEVELOPMENT_BUILD
            if (_threadWarned) return;
            if (Thread.CurrentThread.ManagedThreadId != _mainThreadId)
            {
                _threadWarned = true;
                TelemetryLog.Warn("메인 스레드가 아닌 곳에서 기록이 호출되었습니다. "
                                  + "현재 설계에서는 메인 스레드 호출만 지원합니다.");
            }
#endif
        }

        /// <summary>
        /// Record(name, tag) 와 Record(name, 파라미터 1개) 는 호출 형태가 비슷해 혼동될 수 있다.
        /// 파라미터 없이 두 번째 인자만 전달된 호출은 개발 빌드에서 경고한다.
        /// </summary>
        private static void WarnIfTagLooksLikeParam(string eventName, string tag,
                                                    (string, object)[] parameters)
        {
#if UNITY_EDITOR || DEVELOPMENT_BUILD
            if (parameters == null || parameters.Length == 0)
            {
                TelemetryLog.Warn($"'{eventName}' 기록에서 두 번째 인자 \"{tag}\" 가 분류 태그로 처리됩니다. "
                                  + "파라미터를 의도했다면 (\"이름\", 값) 형태로 전달하세요.");
            }
#endif
        }

        private static void ValidateParamType(string eventName, string key, object value)
        {
#if UNITY_EDITOR || DEVELOPMENT_BUILD
            if (value == null) return;
            if (value is int || value is long || value is float || value is double
                || value is bool || value is string || value is short || value is byte
                || value is uint || value is ulong || value is ushort || value is sbyte
                || value is decimal)
            {
                return;
            }

            var id = eventName + "." + key;
            if (_warnedParamKeys.Add(id))
            {
                TelemetryLog.Warn($"'{id}' 의 값 타입 {value.GetType().Name} 은 허용 타입이 아닙니다. "
                                  + "문자열로 변환해 기록합니다. (허용: 정수·실수 타입 전부, bool, string)");
            }
#endif
        }

        private static string DefaultOutputPath()
        {
#if UNITY_5_3_OR_NEWER
            return Path.Combine(UnityEngine.Application.persistentDataPath, "telemetry");
#else
            return Path.Combine(Path.GetTempPath(), "telemetry");
#endif
        }

        /// <summary>
        /// 이 세션에 쓸 플레이어 식별자. 호출 인자, 설정값, 기기에 저장된 값 순으로 찾는다.
        ///
        /// 기기에 새 식별자를 저장하는 것은 수집 동의가 있을 때만 한다. 동의가 없으면
        /// 이 세션 동안만 쓰는 임시 값을 돌려주고 아무것도 남기지 않는다.
        /// </summary>
        private static string ResolvePlayerId(string explicitId)
        {
            if (!string.IsNullOrEmpty(explicitId)) return explicitId;
            if (!string.IsNullOrEmpty(_config.PlayerId)) return _config.PlayerId;

            bool mayPersist = _config.Enabled && _config.ConsentGiven;
#if UNITY_5_3_OR_NEWER
            const string key = "telemetry.player_id";
            var id = UnityEngine.PlayerPrefs.GetString(key, null);
            if (string.IsNullOrEmpty(id))
            {
                id = Guid.NewGuid().ToString("N").Substring(0, 12);
                if (mayPersist)
                {
                    UnityEngine.PlayerPrefs.SetString(key, id);
                    UnityEngine.PlayerPrefs.Save();
                }
            }
#else
            var id = Guid.NewGuid().ToString("N").Substring(0, 12);
#endif
            if (mayPersist) _config.PlayerId = id;
            return id;
        }

        // ------------------------------------------------------------------ WebGL

#if UNITY_WEBGL && !UNITY_EDITOR
        private static readonly bool IsWebGL = true;

        [System.Runtime.InteropServices.DllImport("__Internal")]
        private static extern void PlaytestTelemetry_Download(string fileName, byte[] data, int length);

        [System.Runtime.InteropServices.DllImport("__Internal")]
        private static extern void PlaytestTelemetry_SyncFiles();

        private static void BrowserDownload(string fileName, byte[] data)
        {
            PlaytestTelemetry_Download(fileName, data, data.Length);
        }

        private static void BrowserSyncFiles()
        {
            PlaytestTelemetry_SyncFiles();
        }
#else
        private static readonly bool IsWebGL = false;

        private static void BrowserDownload(string fileName, byte[] data) { }

        private static void BrowserSyncFiles() { }
#endif

        private static string PlatformName()
        {
#if UNITY_5_3_OR_NEWER
            return UnityEngine.Application.platform.ToString();
#else
            return Environment.OSVersion.Platform.ToString();
#endif
        }

#if UNITY_5_3_OR_NEWER
        /// <summary>
        /// 도메인 리로드 시 정적 상태가 남아 있으면 이전 세션의 핸들을 물고 있게 된다.
        /// 플레이 모드 진입 시점에 초기 상태로 되돌린다.
        /// </summary>
        [UnityEngine.RuntimeInitializeOnLoadMethod(
            UnityEngine.RuntimeInitializeLoadType.SubsystemRegistration)]
        private static void ResetStatics()
        {
            _config = null;
            _session = null;
            _queue = null;
            _writer = null;
            _initialized = false;
            _threadWarned = false;
            _notRecordingWarned = false;
            _webglExportHintShown = false;
            _warnedParamKeys.Clear();
        }

        /// <summary>애플리케이션 종료 시 자동으로 파일을 마감한다.</summary>
        [UnityEngine.RuntimeInitializeOnLoadMethod(
            UnityEngine.RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void HookQuit()
        {
            UnityEngine.Application.quitting -= Shutdown;
            UnityEngine.Application.quitting += Shutdown;

            // 프레임마다 Tick 을 부르고, 앱이 백그라운드로 갈 때 기록을 마무리할 객체.
            TelemetryRunner.Ensure();
        }
#endif
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;

namespace Playtest.Telemetry
{
    /// <summary>
    /// 주기적으로 큐를 비워 NDJSON 으로 직렬화하고 파일에 추가 기록한다.
    ///
    /// 두 가지 방식으로 돈다.
    ///   스레드 방식 (기본)  워커 스레드가 주기마다 쓴다.
    ///   틱 방식            스레드 없이, 게임 스레드가 프레임마다 Pump 를 부르면 주기가
    ///                     찼을 때 쓴다. C# 스레드를 쓸 수 없는 WebGL 을 위한 것이다.
    ///
    /// 설계서 2.5 의 처리 구조를 구현한다.
    ///   - 파일 핸들은 세션 동안 유지한다.
    ///   - 강제 동기화(fsync)를 호출하지 않는다. OS 페이지 캐시에 위임한다.
    ///   - 게임 스레드는 기록을 기다리지 않는다. 예외는 세션을 끝내는 Stop 하나로,
    ///     남은 이벤트가 파일에 들어갈 때까지 제한 시간 안에서 기다린다.
    ///   - 스레드 방식에서 파일(StreamWriter)은 Start 이후 워커 스레드만 건드린다.
    ///     닫는 것도 워커다. 틱 방식에서는 전부 게임 스레드에서 일어난다.
    /// </summary>
    internal sealed class EventWriter
    {
        private readonly EventQueue _queue;
        private readonly NdjsonSerializer _serializer = new NdjsonSerializer();
        private readonly List<TelemetryEvent> _batch = new List<TelemetryEvent>(256);
        private readonly int _flushIntervalMs;
        private readonly string _filePath;
        private readonly bool _verbose;
        private readonly bool _threaded;

        // 스레드 방식에서만 만든다. 스레드가 없는 플랫폼에서는 대기 핸들도 쓰지 않는다.
        private readonly AutoResetEvent _wake;

        // 틱 방식에서 마지막으로 쓴 뒤 흐른 시간을 잰다.
        private readonly System.Diagnostics.Stopwatch _sinceDrain = new System.Diagnostics.Stopwatch();
        private bool _flushRequested;

        // 워커가 큐에서 꺼낸 것을 파일에 넣는 중인지. FlushNow 가 끝을 알기 위해 본다.
        private volatile bool _writing;

        /// <summary>파일에 쓴 직후 불린다. WebGL 이 브라우저 저장소에 반영하는 데 쓴다.</summary>
        public Action AfterWrite;

        private Thread _thread;
        private StreamWriter _writer;
        private volatile bool _running;
        private long _writtenCount;

        /// <summary>이어 쓰는 파일의 마지막 온전한 줄. 새 파일이면 Seq 가 0.</summary>
        internal struct LastLine
        {
            public int Seq;
            /// <summary>그 줄의 t_ms. 읽지 못했으면 -1.</summary>
            public long TMs;
            public DateTime WallClockUtc;
        }

        /// <summary>이어 쓰는 파일에 이미 기록돼 있던 마지막 seq. 새 파일이면 0.</summary>
        public int LastSeqInFile { get { return LastInFile.Seq; } }

        /// <summary>이어 쓰는 파일의 마지막 줄 정보(seq · t_ms · wall_clock).</summary>
        public LastLine LastInFile { get; private set; }

        public string FilePath { get { return _filePath; } }
        public long WrittenCount { get { return Interlocked.Read(ref _writtenCount); } }

        public EventWriter(EventQueue queue, string filePath, float flushIntervalSec, bool verbose,
                           bool threaded = true)
        {
            _queue = queue;
            _filePath = filePath;
            _flushIntervalMs = Math.Max(100, (int)(flushIntervalSec * 1000f));
            _verbose = verbose;
            _threaded = threaded;
            if (threaded) _wake = new AutoResetEvent(false);
        }

        public void Start()
        {
            var dir = Path.GetDirectoryName(_filePath);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
            {
                Directory.CreateDirectory(dir);
            }

            // 같은 세션·플레이어의 파일이 이미 있으면 그 뒤에 이어 쓴다(재접속 등).
            // 이때 seq 를 1 부터 다시 매기면 분석 쪽에서 (세션, 플레이어, seq) 가
            // 겹쳐 뒤 기록이 앞 기록을 덮어쓴다. 마지막 seq 를 읽어 그 뒤부터 잇는다.
            bool needsNewline;
            LastInFile = ReadLastLine(_filePath, out needsNewline);

            // append 전용으로 열고 세션이 끝날 때까지 핸들을 유지한다.
            var stream = new FileStream(_filePath, FileMode.Append, FileAccess.Write,
                                        FileShare.Read, 4096, FileOptions.None);
            _writer = new StreamWriter(stream, new UTF8Encoding(false));
            _writer.AutoFlush = false;

            // 앞 실행이 비정상 종료해 마지막 줄이 잘려 있으면, 줄을 바꾸고 시작한다.
            // 그러지 않으면 새 첫 줄이 잘린 줄 뒤에 붙어 함께 깨진다.
            if (needsNewline) _writer.WriteLine();

            _running = true;
            if (!_threaded)
            {
                _sinceDrain.Start();
                return;
            }

            _thread = new Thread(Loop)
            {
                Name = "Telemetry.EventWriter",
                IsBackground = true,
                Priority = ThreadPriority.BelowNormal,
            };
            _thread.Start();
        }

        /// <summary>즉시 한 번 비우도록 깨운다. 호출자는 완료를 기다리지 않는다.</summary>
        public void RequestFlush()
        {
            if (_threaded) _wake.Set();
            else _flushRequested = true;      // 다음 Pump 에서 쓴다
        }

        /// <summary>
        /// 틱 방식에서 게임 스레드가 프레임마다 부른다. 주기가 찼거나 Flush 요청이
        /// 있을 때만 쓰므로 대부분의 프레임에서는 비교 한 번으로 끝난다.
        /// 스레드 방식에서는 아무 일도 하지 않는다.
        /// </summary>
        public void Pump()
        {
            if (_threaded || !_running) return;
            if (!_flushRequested && _sinceDrain.ElapsedMilliseconds < _flushIntervalMs) return;

            _flushRequested = false;
            _sinceDrain.Restart();
            DrainOnce();
        }

        /// <summary>
        /// 지금까지 받은 이벤트가 파일에 들어갈 때까지 기다린다. 다 들어갔으면 true.
        ///
        /// 앱이 백그라운드로 갈 때 쓴다. 모바일에서는 그 뒤에 OS 가 예고 없이 앱을
        /// 끝낼 수 있어서, "깨워 두고 돌아가는" Flush 로는 마지막 몇 초가 사라진다.
        /// 게임 스레드를 잠깐 세우는 유일한 호출이므로 제한 시간을 짧게 둔다.
        /// </summary>
        public bool FlushNow(int timeoutMs)
        {
            if (!_running) return true;

            if (!_threaded)
            {
                _flushRequested = false;
                _sinceDrain.Restart();
                DrainOnce();
                return true;
            }

            var waited = System.Diagnostics.Stopwatch.StartNew();
            while (true)
            {
                _wake.Set();
                // 큐가 비었고 워커가 쓰는 중도 아니면, 이 호출 전에 들어온 것은 전부
                // 파일에 들어갔다. (워커는 큐를 비우기 전에 _writing 을 올리고, OS 에
                // 넘긴 뒤에 내린다.)
                if (_queue.Count == 0 && !_writing) return true;
                if (waited.ElapsedMilliseconds >= timeoutMs) return false;
                Thread.Sleep(1);
            }
        }

        /// <summary>남은 이벤트를 모두 기록하고 스레드를 정리한다. 세션 종료 시 호출한다.</summary>
        public void Stop(int timeoutMs = 3000)
        {
            if (!_running) return;

            _running = false;

            if (!_threaded)
            {
                DrainOnce();
                CloseWriter();
                return;
            }

            _wake.Set();

            // 마지막 비우기와 파일 닫기는 워커가 한다(Loop 의 끝). 여기서는 기다리기만
            // 한다. 디스크가 느려 제한 시간을 넘기면 기다리기를 그만둘 뿐이고, 워커는
            // 자기 큐에 남은 것을 마저 쓰고 스스로 파일을 닫는다. 큐가 세션마다 따로
            // 이므로 그 사이에 시작된 다음 세션의 이벤트를 가져가지 않는다.
            if (_thread != null && _thread.IsAlive)
            {
                if (!_thread.Join(timeoutMs))
                {
                    TelemetryLog.Warn("워커 스레드가 제한 시간 안에 종료되지 않았습니다. "
                                      + "남은 기록은 백그라운드에서 마무리됩니다.");
                }
            }
            _thread = null;
        }

        private void Loop()
        {
            try
            {
                while (_running)
                {
                    _wake.WaitOne(_flushIntervalMs);
                    DrainOnce();
                }

                // 종료 신호를 받은 뒤 마지막으로 한 번 더 비운다.
                DrainOnce();
            }
            finally
            {
                CloseWriter();
            }
        }

        /// <summary>
        /// 이미 있는 로그 파일의 마지막 온전한 줄에서 seq · t_ms · wall_clock 을 읽는다.
        /// 파일이 없거나 비어 있으면 seq 0. 읽지 못해도 예외를 내지 않는다 — 그때는
        /// seq 0 을 돌려주고, 호출자가 평소처럼 1 부터 시작한다.
        ///
        /// t_ms 와 wall_clock 은 재접속 때 시간을 이어 가는 데 쓴다. 옛 형식이라
        /// 없으면 LastTMs 가 -1 이고, 호출자는 순번만 잇는다.
        ///
        /// 끝에서 64KB 만 본다. 한 줄이 그보다 길 일은 없고, 파일 전체를 읽으면
        /// 긴 세션에 재접속할 때 게임 스레드가 멈춘다.
        /// </summary>
        private static LastLine ReadLastLine(string path, out bool needsNewline)
        {
            needsNewline = false;
            var none = new LastLine { Seq = 0, TMs = -1 };
            try
            {
                if (!File.Exists(path)) return none;

                string tail;
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read,
                                               FileShare.ReadWrite))
                {
                    if (fs.Length == 0) return none;

                    int size = (int)Math.Min(fs.Length, 64 * 1024);
                    var buf = new byte[size];
                    fs.Seek(-size, SeekOrigin.End);
                    int read = 0;
                    while (read < size)
                    {
                        int n = fs.Read(buf, read, size - read);
                        if (n <= 0) break;
                        read += n;
                    }
                    tail = Encoding.UTF8.GetString(buf, 0, read);
                }

                needsNewline = tail.Length > 0 && tail[tail.Length - 1] != '\n';

                // 뒤에서부터 온전한 줄(줄바꿈으로 끝난 줄)을 찾아 읽는다.
                // 직렬화 순서가 고정이라(session_id, player_id, seq, t_ms, t_raw_ms,
                // wall_clock ...) 구조상의 `,"seq":` · `,"t_ms":` · `,"wall_clock":"`
                // 는 줄의 앞쪽에 한 번씩 나온다. 값 안의 따옴표는 전부 \" 로
                // 이스케이프되어 있어 이 문자열과 겹치지 않는다. 그래서 줄 처음부터
                // 찾은 첫 위치가 봉투의 값이다.
                int end = tail.LastIndexOf('\n');
                while (end > 0)
                {
                    int start = tail.LastIndexOf('\n', end - 1) + 1;
                    long seq = ReadNumberAfter(tail, ",\"seq\":", start, end);
                    if (seq > 0 && seq <= int.MaxValue)
                    {
                        var line = new LastLine { Seq = (int)seq, TMs = -1 };
                        long tMs = ReadNumberAfter(tail, ",\"t_ms\":", start, end);
                        DateTime wall;
                        if (tMs >= 0 && ReadWallClock(tail, start, end, out wall))
                        {
                            line.TMs = tMs;
                            line.WallClockUtc = wall;
                        }
                        return line;
                    }
                    end = start - 1;
                }
                return none;
            }
            catch (Exception ex)
            {
                TelemetryLog.Warn("기존 로그 파일을 읽지 못했습니다: " + ex.Message);
                return none;
            }
        }

        /// <summary>[start, end) 안에서 marker 바로 뒤의 0 이상 정수. 없으면 -1.</summary>
        private static long ReadNumberAfter(string s, string marker, int start, int end)
        {
            int at = s.IndexOf(marker, start, end - start, StringComparison.Ordinal);
            if (at < 0) return -1;
            int i = at + marker.Length;
            long value = 0;
            bool any = false;
            while (i < end && s[i] >= '0' && s[i] <= '9' && value < long.MaxValue / 10)
            {
                value = value * 10 + (s[i] - '0');
                any = true;
                i++;
            }
            return any ? value : -1;
        }

        private static bool ReadWallClock(string s, int start, int end, out DateTime wall)
        {
            wall = default;
            const string marker = ",\"wall_clock\":\"";
            int at = s.IndexOf(marker, start, end - start, StringComparison.Ordinal);
            if (at < 0) return false;
            int from = at + marker.Length;
            int to = s.IndexOf('"', from, end - from);
            if (to < 0) return false;
            return DateTime.TryParseExact(s.Substring(from, to - from), "yyyy-MM-ddTHH:mm:ss.fffZ",
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out wall);
        }

        private void DrainOnce()
        {
            if (_writer == null) return;

            // 큐를 비우기 전에 올린다. 순서가 바뀌면 "큐는 비었는데 아직 파일에는
            // 없는" 순간에 FlushNow 가 다 됐다고 판단한다.
            _writing = true;
            _batch.Clear();
            int n = _queue.DrainTo(_batch);
            if (n == 0)
            {
                _writing = false;
                return;
            }

            try
            {
                for (int i = 0; i < _batch.Count; i++)
                {
                    _writer.WriteLine(_serializer.Serialize(_batch[i]));
                }

                // Flush 는 StreamWriter 버퍼를 OS 로 넘기는 것까지만 한다.
                // 디스크 동기화(fsync)는 호출하지 않는다.
                _writer.Flush();

                Interlocked.Add(ref _writtenCount, _batch.Count);

                if (_verbose)
                {
                    TelemetryLog.Info($"{_batch.Count}건 기록 (누적 {WrittenCount}건)");
                }
            }
            catch (Exception ex)
            {
                // 기록 실패가 게임을 멈추게 해서는 안 된다.
                TelemetryLog.Warn("이벤트 기록 실패: " + ex.Message);
            }
            finally
            {
                _queue.Return(_batch);
                _batch.Clear();
                _writing = false;
            }

            var after = AfterWrite;
            if (after != null)
            {
                try { after(); }
                catch (Exception ex) { TelemetryLog.Warn("기록 후 처리 실패: " + ex.Message); }
            }
        }

        private void CloseWriter()
        {
            if (_writer == null) return;
            try
            {
                _writer.Flush();
                _writer.Dispose();
            }
            catch (Exception ex)
            {
                TelemetryLog.Warn("파일 닫기 실패: " + ex.Message);
            }
            _writer = null;
        }
    }
}

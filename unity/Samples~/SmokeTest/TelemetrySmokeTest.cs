using System.Diagnostics;
using System.IO;
using Playtest.Telemetry;
using UnityEngine;

#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
#endif

/// <summary>
/// 수집 SDK 동작 확인용 스크립트. 빈 씬의 게임오브젝트에 붙여 재생한다.
///
/// 화면 좌측 상단 버튼으로 조작하며, 키보드 1~5 로도 같은 동작을 할 수 있다.
/// 입력은 구 Input Manager 와 Input System 패키지 양쪽을 지원한다.
///
///   1 : 사건 기록 한 건
///   2 : 리소스 변동 기록 한 건
///   3 : 부하 테스트 (stressCount 만큼 연속 기록)
///   4 : 세션 종료 (파일 마감)
///   5 : 로그 파일 목록을 콘솔에 출력
///
/// 비정상 종료 확인은 3 으로 이벤트를 쌓은 뒤 에디터를 강제 종료하고,
/// 다시 실행해 파일에 직전 기록이 남아 있는지 보면 된다.
/// </summary>
public sealed class TelemetrySmokeTest : MonoBehaviour
{
    [Header("설정")]
    public bool consentGiven = true;
    public bool verboseLog = true;
    public float flushIntervalSec = 2f;
    public int queueCapacity = 4096;

    [Header("부하 테스트")]
    public int stressCount = 5000;

    [Header("화면 조작 버튼")]
    public bool showOnScreenButtons = true;

    private string _outputPath;
    private string _sessionId;
    private int _bossPhase = 1;
    private string _lastResult = "준비됨";

    private void Start()
    {
        _outputPath = Path.Combine(Application.persistentDataPath, "telemetry");

        Telemetry.Init(new TelemetryConfig
        {
            OutputPath = _outputPath,
            BuildVersion = Application.version,
            FlushIntervalSec = flushIntervalSec,
            QueueCapacity = queueCapacity,
            Enabled = true,
            ConsentGiven = consentGiven,
            VerboseLog = verboseLog,
        });

        // 단독 플레이로 세션을 연다. 협동이면 호스트가 BeginSession 으로 받은 식별자를
        // 네트워크로 전달하고, 클라이언트는 JoinSession 으로 참가한다.
        _sessionId = Telemetry.BeginSession(SessionMode.Solo);

        UnityEngine.Debug.Log($"[SmokeTest] 세션 시작: {_sessionId}");
        UnityEngine.Debug.Log($"[SmokeTest] 출력 경로: {_outputPath}");
    }

    private void Update()
    {
        if (WasKeyPressed(1)) RecordEvent();
        if (WasKeyPressed(2)) RecordResource();
        if (WasKeyPressed(3)) RunStressTest();
        if (WasKeyPressed(4)) EndSession();
        if (WasKeyPressed(5)) ListFiles();
    }

    // ------------------------------------------------------------------ 동작

    private void RecordEvent()
    {
        Telemetry.Record("boss_phase_changed", "combat",
            ("boss_id", "flame_golem"),
            ("phase", _bossPhase++),
            ("elapsed_sec", Time.timeSinceLevelLoad));

        Report("사건 기록 1건");
    }

    private void RecordResource()
    {
        Telemetry.Resource("gold", 150, "enemy_kill");
        Report("리소스 변동 기록 1건");
    }

    /// <summary>
    /// 기록 호출이 게임 스레드를 얼마나 점유하는지 측정한다.
    /// 설계서 1.2 의 "게임 성능 비간섭" 항목을 확인하는 용도이다.
    /// </summary>
    private void RunStressTest()
    {
        var sw = Stopwatch.StartNew();

        for (int i = 0; i < stressCount; i++)
        {
            Telemetry.Record("player_position", EventTier.Sampled,
                ("x", i * 0.1f),
                ("y", 3.2f),
                ("z", i * -0.05f));
        }

        sw.Stop();

        double totalMs = sw.Elapsed.TotalMilliseconds;
        double perCallUs = totalMs * 1000.0 / stressCount;

        Report($"{stressCount}건 기록에 {totalMs:F2}ms " +
               $"(호출당 {perCallUs:F2}us, 폐기 {Telemetry.DroppedCount}건)");
    }

    private void EndSession()
    {
        Telemetry.EndSession();
        Report("세션 종료. 파일이 마감되었습니다.");
    }

    private void ListFiles()
    {
        if (!Directory.Exists(_outputPath))
        {
            Report("아직 생성된 로그 파일이 없습니다.");
            return;
        }

        var files = Directory.GetFiles(_outputPath, "*.ndjson");
        foreach (var f in files)
        {
            UnityEngine.Debug.Log($"[SmokeTest] {f} ({new FileInfo(f).Length} bytes)");
        }
        Report($"로그 파일 {files.Length}개 (상세는 콘솔)");
    }

    private void Report(string message)
    {
        _lastResult = message;
        UnityEngine.Debug.Log("[SmokeTest] " + message);
    }

    // ------------------------------------------------------------------ 입력

    /// <summary>
    /// 숫자 키 1~5 가 이번 프레임에 눌렸는지 확인한다.
    /// Input System 패키지가 활성화된 프로젝트와 구 Input Manager 양쪽을 지원한다.
    /// </summary>
    private static bool WasKeyPressed(int digit)
    {
#if ENABLE_INPUT_SYSTEM
        var kb = Keyboard.current;
        if (kb == null) return false;

        switch (digit)
        {
            case 1: return kb.digit1Key.wasPressedThisFrame;
            case 2: return kb.digit2Key.wasPressedThisFrame;
            case 3: return kb.digit3Key.wasPressedThisFrame;
            case 4: return kb.digit4Key.wasPressedThisFrame;
            case 5: return kb.digit5Key.wasPressedThisFrame;
            default: return false;
        }
#elif ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKeyDown(KeyCode.Alpha0 + digit);
#else
        return false;
#endif
    }

    // ------------------------------------------------------------------ 화면

    /// <summary>
    /// 입력 시스템 설정과 무관하게 동작하는 조작 버튼.
    /// 키보드가 막혀 있어도 이것으로 모든 동작을 확인할 수 있다.
    /// </summary>
    private void OnGUI()
    {
        if (!showOnScreenButtons) return;

        const int w = 260;
        const int h = 32;
        int x = 12, y = 12;

        GUI.Box(new Rect(x - 6, y - 6, w + 12, h * 7 + 40), "Telemetry 동작 확인");
        y += 24;

        if (GUI.Button(new Rect(x, y, w, h), "1  사건 기록")) RecordEvent();
        y += h + 4;

        if (GUI.Button(new Rect(x, y, w, h), "2  리소스 변동 기록")) RecordResource();
        y += h + 4;

        if (GUI.Button(new Rect(x, y, w, h), $"3  부하 테스트 ({stressCount}건)")) RunStressTest();
        y += h + 4;

        if (GUI.Button(new Rect(x, y, w, h), "4  세션 종료")) EndSession();
        y += h + 4;

        if (GUI.Button(new Rect(x, y, w, h), "5  로그 파일 목록")) ListFiles();
        y += h + 8;

        GUI.Label(new Rect(x, y, w, h),
            Telemetry.IsRecording ? "기록 중" : "기록 중지됨");
        y += 20;

        GUI.Label(new Rect(x, y, w, h * 2), _lastResult);
    }

    private void OnApplicationQuit()
    {
        Telemetry.Shutdown();
    }
}

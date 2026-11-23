namespace Playtest.Telemetry
{
    /// <summary>
    /// SDK 내부 로그. 게임의 로그와 구분되도록 접두사를 붙인다.
    /// Unity 가 아닌 환경(테스트 러너 등)에서도 컴파일되도록 분기해 둔다.
    /// </summary>
    internal static class TelemetryLog
    {
        private const string Prefix = "[Telemetry] ";

        public static void Info(string message)
        {
#if UNITY_5_3_OR_NEWER
            UnityEngine.Debug.Log(Prefix + message);
#else
            System.Console.WriteLine(Prefix + message);
#endif
        }

        public static void Warn(string message)
        {
#if UNITY_5_3_OR_NEWER
            UnityEngine.Debug.LogWarning(Prefix + message);
#else
            System.Console.WriteLine(Prefix + "WARN " + message);
#endif
        }

        public static void Error(string message)
        {
#if UNITY_5_3_OR_NEWER
            UnityEngine.Debug.LogError(Prefix + message);
#else
            System.Console.WriteLine(Prefix + "ERROR " + message);
#endif
        }
    }
}

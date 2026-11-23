#if UNITY_5_3_OR_NEWER
using UnityEngine;

namespace Playtest.Telemetry
{
    /// <summary>
    /// SDK 가 Unity 의 프레임과 앱 상태 변화를 받기 위해 스스로 만드는 숨은 객체.
    /// 게임이 씬에 놓거나 만들 필요가 없다.
    ///
    /// 하는 일은 둘이다.
    ///   - 프레임마다 Telemetry.Tick 을 부른다. 스레드 없이 도는 방식(WebGL)에서
    ///     파일에 쓰는 일이 여기서 일어난다. 스레드 방식에서는 아무 일도 하지 않는다.
    ///   - 앱이 백그라운드로 가면 그때까지의 기록을 파일에 넣는다. 모바일은 그 뒤에
    ///     OS 가 예고 없이 앱을 끝낼 수 있고, WebGL 은 탭을 닫을 때 아무 알림도
    ///     오지 않는다. 화면에서 내려가는 순간이 마지막 기회다.
    /// </summary>
    [AddComponentMenu("")]
    internal sealed class TelemetryRunner : MonoBehaviour
    {
        private static TelemetryRunner _instance;

        internal static void Ensure()
        {
            if (_instance != null) return;

            var go = new GameObject("[Playtest.Telemetry]");
            go.hideFlags = HideFlags.HideInHierarchy;
            DontDestroyOnLoad(go);
            _instance = go.AddComponent<TelemetryRunner>();
        }

        private void Update()
        {
            Telemetry.Tick();
        }

        private void OnApplicationPause(bool paused)
        {
            if (paused) OnBackground();
        }

        private void OnApplicationFocus(bool focused)
        {
            if (!focused) OnBackground();
        }

        private static void OnBackground()
        {
            // 모바일과 WebGL 은 끝까지 쓰고 돌아간다. 데스크톱은 창 전환이 잦고
            // 그 뒤에 앱이 사라질 일도 드물어서, 게임 스레드를 세우지 않고 깨우기만 한다.
            if (Application.isMobilePlatform || Application.platform == RuntimePlatform.WebGLPlayer)
            {
                Telemetry.FlushNow();
            }
            else
            {
                Telemetry.Flush();
            }
        }
    }
}
#endif

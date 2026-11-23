// Playtest.Telemetry 의 WebGL 브라우저 연동. WebGL 빌드에만 포함된다.
//
// C# 쪽 선언은 Telemetry.cs 의 "WebGL" 구역에 있다.
mergeInto(LibraryManager.library, {

  // 로그 파일을 브라우저의 파일 다운로드로 내보낸다.
  // 브라우저에 따라, 사용자의 클릭 없이 여러 파일을 연달아 받으면 허용 여부를 묻는다.
  PlaytestTelemetry_Download: function (fileNamePtr, dataPtr, length) {
    var fileName = (typeof UTF8ToString !== 'undefined')
      ? UTF8ToString(fileNamePtr)
      : Pointer_stringify(fileNamePtr);

    // 힙의 내용은 이 함수가 돌아간 뒤 바뀔 수 있으므로 복사해 둔다.
    var bytes = new Uint8Array(length);
    bytes.set(HEAPU8.subarray(dataPtr, dataPtr + length));

    var blob = new Blob([bytes], { type: 'application/x-ndjson' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  },

  // 메모리 안의 파일시스템을 브라우저 저장소(IndexedDB)에 반영한다.
  // 비동기라서 앞의 반영이 끝나기 전에 또 불릴 수 있다. 겹쳐 돌리지 않고,
  // 끝난 뒤 한 번 더 돌려 마지막 기록까지 반영되게 한다.
  PlaytestTelemetry_SyncFiles: function () {
    if (Module.playtestTelemetrySyncing) {
      Module.playtestTelemetrySyncPending = true;
      return;
    }
    var run = function () {
      Module.playtestTelemetrySyncing = true;
      FS.syncfs(false, function (err) {
        Module.playtestTelemetrySyncing = false;
        if (err) console.warn('[Telemetry] syncfs failed: ' + err);
        if (Module.playtestTelemetrySyncPending) {
          Module.playtestTelemetrySyncPending = false;
          run();
        }
      });
    };
    run();
  }
});

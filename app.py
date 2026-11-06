"""app.py — pywebview 진입점 (v3).

pywebview(WebView2) 기반 v3 진입점.
백엔드 3모듈(modrinth_api, preflight, mod_catalog)은 그대로 재사용하고,
UI 는 frontend/ 의 HTML 을 WebView2 로 렌더링한다.

Nether Glass HTML UI 를 WebView2 로 렌더링하고,
Api 클래스를 js_api 브릿지로 연결한다.
"""

import os
import sys

import webview

from api import Api
import app_meta


def _resource(rel: str) -> str:
    """PyInstaller onefile 에서도 동작하는 리소스 경로 해석."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# Microsoft 공식 WebView2 Evergreen 부트스트래퍼(설치 파일이 바로 내려받아진다)
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def _webview2_missing() -> bool:
    """WebView2 런타임이 없어 pywebview 가 IE(MSHTML)로 대체할 상황인가.

    pywebview 는 이때 경고 로그만 남기고 IE 로 창을 띄우는데, 우리 화면은 IE 에서 돌지 않아
    흰 창만 남는다. 판정은 pywebview 자신의 것(winforms.is_chromium)을 그대로 쓴다 —
    .NET 버전·채널·최소 버전까지 따지는 로직을 복제하면 언젠가 어긋난다.
    판정 자체가 실패하면 False: 오탐으로 런처를 막지 않는다.
    """
    if sys.platform != "win32":
        return False
    try:
        from webview.platforms import winforms
        return not winforms.is_chromium
    except Exception:
        return False


def _show_webview2_help():
    """설치 안내 메시지 상자. [예] → 공식 설치 파일 다운로드."""
    import ctypes
    import webbrowser
    msg = ("런처 화면을 표시하는 데 필요한 Microsoft Edge WebView2 런타임이 "
           "이 PC 에 설치되어 있지 않습니다.\n\n"
           "[예]를 누르면 Microsoft 공식 설치 파일이 내려받아집니다.\n"
           "설치한 뒤 런처를 다시 실행해주세요.")
    MB_YESNO, MB_ICONWARNING, IDYES = 0x4, 0x30, 6
    try:
        r = ctypes.windll.user32.MessageBoxW(None, msg, "윤슬 런처", MB_YESNO | MB_ICONWARNING)
    except Exception:
        r = IDYES
    if r == IDYES:
        webbrowser.open(WEBVIEW2_DOWNLOAD)


def _print_mc_token(uuid: str) -> int:
    """`--mc-token <uuid>` — 창 없이 유효한 MC 토큰 한 줄을 stdout 으로 내고 끝난다.

    게임 안 코어가 24시간 넘게 켜져 있을 때 프로필 공개키·서버 접속에 쓸 새 토큰을 받으려고
    부른다(런처 창이 닫혀 있어도 동작). 토큰은 로그·파일에 남기지 않는다.
    """
    import auth
    try:
        token = auth.get_token_for(uuid)
    except Exception as e:
        auth._log_auth(f"[인증] 게임 토큰 요청 실패: {type(e).__name__}")
        return 1
    auth._log_auth("[인증] 게임 토큰 요청 처리")
    data = (token + "\n").encode("ascii")
    if sys.platform != "win32":
        sys.stdout.write(data.decode("ascii"))
        return 0
    # 창 모드 exe(console=False)에선 sys.stdout 이 None 일 수 있다 → 상속받은 핸들에 직접 쓴다.
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    written = wintypes.DWORD()
    ok = k32.WriteFile(k32.GetStdHandle(-11), data, len(data), ctypes.byref(written), None)
    return 0 if ok else 1


def main():
    if "--mc-token" in sys.argv:
        i = sys.argv.index("--mc-token")
        sys.exit(_print_mc_token(sys.argv[i + 1] if i + 1 < len(sys.argv) else ""))

    # Windows 작업표시줄 아이콘 그룹화 (tkinter 판과 동일 로직)
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "yoonseul.launcher.1"
        )
    except Exception:
        pass

    if _webview2_missing():
        _show_webview2_help()
        return

    api = Api()

    html_path = _resource(os.path.join(
        "frontend", "nether-glass-launcher-standalone.html"
    ))

    kwargs = dict(
        title=f"윤슬 런처 v{app_meta.APP_VERSION}",
        url=html_path,
        js_api=api,
        width=1000,
        height=720,
        min_size=(880, 640),
        frameless=True,       # OS 타이틀바 제거 → HTML 커스텀 타이틀바 사용
        easy_drag=False,      # 전체 드래그 비활성 (제목표시줄 drag-region 만 이동)
    )
    # 시작 프로그램으로 부팅 때 뜬 경우(autostart.py 가 --autostart 를 붙인다)
    # 창을 최소화해 띄운다 — 매 부팅마다 창이 튀어나오지 않게.
    if "--autostart" in sys.argv:
        kwargs["minimized"] = True
    try:
        window = webview.create_window(**kwargs)
    except TypeError:
        # 이 pywebview 버전이 minimized 를 모르면 그냥 일반 창으로.
        kwargs.pop("minimized", None)
        window = webview.create_window(**kwargs)
    api._window = window  # Python→JS push(evaluate_js)용 참조

    # 앱 종료 시 플레이 시간 세션을 최종 정리(누적). 종료 경로가 여러 개라
    # (창 X, OS 종료, 코드 destroy) 가능한 모든 이벤트에 같은 정리를 건다.
    def _finalize_session():
        try:
            api._flush_playtime()
            api._config["session_start"] = None
            import preflight as _pf
            _pf.save_config(api._config)
        except Exception:
            pass

    api._finalize_session = _finalize_session   # win_close 등에서도 호출 가능
    try:
        window.events.closing += _finalize_session   # 창 닫히기 직전
    except Exception:
        pass
    try:
        window.events.closed += _finalize_session    # 완전히 닫힌 뒤(백업)
    except Exception:
        pass

    # 어떤 HTML 을 로드하는지 콘솔에 남긴다(파일 혼동 방지 진단).
    print(f"[app.py] loading HTML: {html_path}", flush=True)

    # ★ 파이썬 쪽에서 부트를 확실히 트리거한다 ★
    #   JS 폴링만으로도 되지만, DOM 로드 완료(loaded) 시점에 Python 이
    #   evaluate_js 로 bootFromBackend() 를 한 번 더 호출해 이중 안전망을
    #   둔다. bootFromBackend 는 idempotent(중복 무시)라 두 번 불려도 안전.
    def _trigger_boot():
        try:
            window.evaluate_js(
                "window.bootFromBackend ? (bootFromBackend(), 'boot triggered')"
                " : 'bootFromBackend missing'"
            )
            print("[app.py] boot triggered via evaluate_js", flush=True)
        except Exception as e:
            print(f"[app.py] evaluate_js failed: {e}", flush=True)

    # pywebview 버전에 따라 loaded 이벤트 접근 방식이 다르다 — 둘 다 대응.
    try:
        window.events.loaded += _trigger_boot            # pywebview 4.x+
        print("[app.py] hooked window.events.loaded", flush=True)
    except AttributeError:
        try:
            window.loaded += _trigger_boot               # 구버전
            print("[app.py] hooked window.loaded", flush=True)
        except Exception as e:
            print(f"[app.py] could not hook loaded event: {e}", flush=True)

    # 릴리스 빌드는 debug=False (exe 에서 devtools 안 뜨게).
    # 개발 중 브릿지/화면 디버깅이 필요하면 잠시 debug=True 로 바꿔 우클릭→검사 사용.
    webview.start(debug=False)


if __name__ == "__main__":
    main()

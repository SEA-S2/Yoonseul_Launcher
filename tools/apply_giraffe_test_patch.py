"""Actions 전용: 2.7.1 소스를 기린월드 필수 모드 테스트판으로 패치."""

from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"patch target not found: {path}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "app_meta.py",
    'APP_VERSION = "2.7.1"  # 사용자 지정 시간 기능 제거',
    'APP_VERSION = "2.7.2"  # 기린월드 공식 필수 모드 개인 테스트판',
)
replace("app_meta.py", 'APP_CHANNEL = "release"', 'APP_CHANNEL = "beta"')

replace(
    "Yoonseul_Launcher.spec",
    "    'instance', 'launcher', 'mod_catalog', 'modrinth_api', 'preflight',",
    "    'giraffe_required', 'instance', 'launcher', 'mod_catalog', 'modrinth_api', 'preflight',",
)

old_gate = '''        import time as _time
        gate = self.can_launch()
        if not gate["ok"]:
            return {"ok": False, "error": "required_missing",
                    "missing": gate["missing"], "message": gate["message"]}

        # ── 실행 전 무결성 검증 + 복구 ──────────────────────────────
'''
new_gate = '''        import time as _time

        # ── 실행 전 무결성 검증 + 복구 ──────────────────────────────
'''
replace("api.py", old_gate, new_gate)

marker = '''            except Exception:
                pass

        try:
            import game_installer
'''
required = '''            except Exception:
                pass

        # 기린월드 공식 필수 모드는 원본 배포처에서 직접 받아 설치한다.
        # 기존 이용자의 options.txt/키 설정은 건드리지 않는다.
        try:
            import giraffe_required
            if not self._registry:
                self.ensure_ready()
            giraffe_required.ensure(on_progress=_prep, registry=self._registry)
        except Exception as e:
            msg = f"기린월드 필수 모드를 준비하지 못했습니다:\\n{e}"
            _prep(-1, msg)
            return {"ok": False, "error": "required_download_failed",
                    "message": msg}

        gate = self.can_launch()
        if not gate["ok"]:
            return {"ok": False, "error": "required_missing",
                    "missing": gate["missing"], "message": gate["message"]}

        try:
            import game_installer
'''
replace("api.py", marker, required)

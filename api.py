"""api.py — pywebview JS↔Python 브릿지.

JS 에서 window.pywebview.api.메서드() 로 호출한다.
- 메서드명이 '_' 로 시작하면 JS 에 노출되지 않는다 (내부용).
- 반환값은 JSON 직렬화 가능한 dict/list/str/int/bool 만 가능.
  (백엔드 함수들은 이미 이 형태로 반환하도록 설계돼 있음)

v2.1 진행:
  [완료] ping / get_versions          — 최소 브릿지 검증
  [추가] ensure_ready / get_catalog   — 경로 준비 + 정적 카탈로그
  [추가] scan_mods                    — 설치/업데이트 상태 스캔 (네트워크)
  [추가] install_mod / remove_mod / update_mod / update_all
  다음:  셰이더 → 시스템(업데이트 확인) → 사전 점검 화면

설계 원칙(중요):
  - 모드 설치/삭제 로직 (_install_single / _do_install / _do_remove 등)
    _do_update / _scan_and_check 오케스트레이션을 그대로 옮긴다.
    UI 프레임워크(tkinter messagebox/after/thread)만 걷어내고,
    성공/실패는 반환 dict 로 JS 에 넘긴다.
  - 긴 작업(다운로드)은 pywebview 가 노출 메서드를 별도 스레드에서
    실행하므로 여기서 별도 스레드를 만들지 않아도 UI 가 안 멈춘다.
"""

import filecmp
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import app_meta
import mod_catalog
import modrinth_api
import preflight
import server_status

from mod_catalog import MODS, VISIBLE_MODS, MOD_BY_ID, CATEGORIES, CATEGORY_COLOR

GAME_VERSION = app_meta.GAME_VERSION
LOADER = app_meta.LOADER

#: 카탈로그에 없는 '의존 모드' 를 레지스트리에 기록할 때 쓰는 키 접두어.
#: 이래야 인스턴스 초기화에서 함께 지워지고, 갱신 시 옛 jar 가 남지 않는다.
DEP_PREFIX = "__dep__:"


def _dep_key(project_id: str) -> str:
    return DEP_PREFIX + str(project_id)


def _is_dep_key(mod_id: str) -> bool:
    return str(mod_id).startswith(DEP_PREFIX)


def _resource(rel: str) -> str:
    """PyInstaller onefile 에서도 동작하는 리소스 경로 해석.
    (app.py 의 _resource 와 동일 — 번들 에셋 복사용)"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class Api:
    def __init__(self):
        self._window = None          # app.py 에서 주입 (evaluate_js 용)
        self._maxed = False          # 최대화 상태 (win_toggle_max)
        self._config = preflight.load_config()
        # 이전 실행이 비정상 종료됐다면 남아있는 세션 기준점을 폐기(누적 인플레 방지)
        if self._config.get("session_start"):
            self._config["session_start"] = None
            preflight.save_config(self._config)
        self._minecraft_dir = None
        self._mods_dir = None
        self._shaderpacks_dir = None
        self._resourcepacks_dir = None
        self._registry = None        # ensure_ready 후 바인딩
        self._resourcepack_registry = None
        # mod_id → "installed" | "update" | "not_installed"
        self._installed_pids = set()
        self._mod_statuses = {}
        # mod_id → {"installed": "0.9.1", "latest": "0.9.2"}
        self._mod_versions = {}
        self._pid_cache = self._config.get("_pid_cache", {})
        # 셰이더: slug → {"filename":..., "project_id":...} (제거/업데이트용)
        self._shader_by_slug = {}
        # Modrinth 실시간 검색 결과: 동적 id("mr:<project_id>") → 메타
        self._search_mod_by_id = {}

    # ══════════════════════════════════════════════════════════════════════
    #  최소 브릿지 검증용
    # ══════════════════════════════════════════════════════════════════════

    def ping(self):
        """가장 단순한 왕복 테스트. JS 에서 결과를 받으면 브릿지 정상."""
        return {"ok": True, "message": "pywebview 브릿지 정상 작동"}

    # ── 창 컨트롤 (frameless 커스텀 타이틀바용) ────────────────────────────
    def win_minimize(self):
        """커스텀 제목표시줄의 최소화 버튼.

        pywebview 버전에 따라 Window.minimize()가 조용히 무시되는 경우가 있어
        Windows 네이티브 Form 상태 변경을 보조 경로로 사용한다.
        """
        first_error = None
        try:
            self._window.minimize()
        except Exception as e:
            first_error = e
        try:
            native = getattr(self._window, "native", None)
            if native is not None:
                from System.Windows.Forms import FormWindowState
                native.WindowState = FormWindowState.Minimized
                return {"ok": True}
        except Exception:
            pass
        if first_error is None:
            return {"ok": True}
        return {"ok": False, "message": str(first_error)}

    def win_toggle_max(self):
        """최대화 ↔ 복원 토글."""
        try:
            if getattr(self, "_maxed", False):
                self._window.restore()
                self._maxed = False
            else:
                self._window.maximize()
                self._maxed = True
        except Exception:
            pass
        return {"maxed": getattr(self, "_maxed", False)}

    def win_close(self):
        # 창을 닫기 전에 플레이 시간 세션을 확실히 정리(누적)
        fin = getattr(self, "_finalize_session", None)
        if fin:
            try:
                fin()
            except Exception:
                pass
        else:
            try:
                self._flush_playtime()
                self._config["session_start"] = None
                preflight.save_config(self._config)
            except Exception:
                pass
        try:
            self._window.destroy()
        except Exception:
            pass

    # ── 플레이 시간 (게임 실행 시점부터 누적) ─────────────────────────────
    def _flush_playtime(self):
        """활성 세션이 있으면 경과 시간을 누적하고 세션 기준점을 현재로 갱신.
        조회/종료 시 호출 → 크래시가 나도 마지막 flush 까지는 보존된다."""
        import time as _time
        start = self._config.get("session_start")
        if start:
            elapsed = max(0, int(_time.time()) - int(start))
            # 비정상적으로 큰 값(시계 변경 등)은 24시간으로 캡
            elapsed = min(elapsed, 24 * 3600)
            self._config["playtime_seconds"] = int(
                self._config.get("playtime_seconds", 0)) + elapsed
            self._config["session_start"] = int(_time.time())
            preflight.save_config(self._config)

    def _playtime_label(self, seconds):
        """분→시간→일 단위 자동 전환.
          60분 미만  → "M분"
          24시간 미만 → "H시간" 또는 "H시간 M분"
          24시간 이상 → "D일" 또는 "D일 H시간"
        """
        m_total = seconds // 60
        if m_total < 60:
            return f"{m_total}분"
        h_total = seconds // 3600
        if h_total < 24:
            m = (seconds % 3600) // 60
            return f"{h_total}시간 {m}분" if m else f"{h_total}시간"
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}일 {h}시간" if h else f"{d}일"

    def get_playtime(self):
        """누적 플레이 시간 반환: {"seconds":int, "label":"N시간 M분"}."""
        self._flush_playtime()
        secs = int(self._config.get("playtime_seconds", 0))
        return {"seconds": secs, "label": self._playtime_label(secs)}

    def _missing_required(self):
        """설치되지 않은 필수 모드 목록 [{id,name}] 반환."""
        if not self._registry:
            return []
        missing = []
        # 숨겨진 내부 코어도 필수 구성요소 검사에는 반드시 포함한다.
        for m in MODS:
            if m.get("required") and not self._registry.is_installed(m["id"]):
                missing.append({"id": m["id"], "name": m.get("name", m["id"])})
        return missing

    def _ensure_hidden_required(self):
        """UI에 숨긴 번들 필수 구성요소를 조용히 설치·복구한다."""
        if not self._registry:
            ready = self.ensure_ready()
            if not ready.get("ready"):
                return
        for mod in MODS:
            if not (mod.get("hidden") and mod.get("required")
                    and mod.get("bundled_asset")):
                continue
            asset = mod["bundled_asset"]
            target = os.path.join(self._mods_dir, asset)
            source = _resource(os.path.join("assets", asset))
            needs_copy = not os.path.isfile(target)
            if not needs_copy and os.path.isfile(source):
                try:
                    needs_copy = not filecmp.cmp(source, target, shallow=False)
                except OSError:
                    needs_copy = True
            if needs_copy:
                ok, reason = self._install_single(mod["id"])
                if not ok:
                    preflight.write_log(
                        f"[자동설치실패] {mod.get('name', mod['id'])}: {reason}")
            elif not self._registry.is_installed(mod["id"]):
                self._registry.record_install(
                    mod["id"], asset, mod.get("bundled_version", "bundled"), None)

    def can_launch(self):
        """게임 실행 가능 여부(필수 모드 전부 설치됐는지).
        반환: {"ok":bool, "missing":[{id,name}], "message":str}"""
        if not self._registry:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "missing": [], "message": r.get("reason", "준비 실패")}
        # 사용자가 따로 설치할 필요가 없는 내부 코어는 실행 전에 자동 복구한다.
        self._ensure_hidden_required()
        missing = self._missing_required()
        if missing:
            names = ", ".join(x["name"] for x in missing)
            return {"ok": False, "missing": missing,
                    "message": f"필수 모드가 설치되지 않았습니다: {names}\n먼저 설치해주세요."}
        return {"ok": True, "missing": [], "message": ""}

    # ── 설정 화면 동작들 ──────────────────────────────────────────────────
    def open_log_folder(self):
        """로그 파일이 있는 폴더를 탐색기로 연다."""
        try:
            log_path = preflight._log_path()
            folder = os.path.dirname(log_path)
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # Windows 전용
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": f"로그 폴더를 열 수 없습니다: {e}"}

    def open_mods_folder(self):
        """mods 폴더를 탐색기로 연다 (격리 인스턴스)."""
        import instance
        mods = instance.mods_dir()
        try:
            os.makedirs(mods, exist_ok=True)
            os.startfile(mods)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": f"폴더를 열 수 없습니다: {e}"}

    def open_shaderpacks_folder(self):
        """shaderpacks 폴더를 탐색기로 연다 (격리 인스턴스)."""
        import instance
        shaders = instance.shaderpacks_dir()
        try:
            os.makedirs(shaders, exist_ok=True)
            os.startfile(shaders)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": f"폴더를 열 수 없습니다: {e}"}

    # ── 리소스팩 (v3.1.x 리소스팩 탭) ─────────────────────────────────────
    def list_resourcepacks(self):
        """인스턴스 resourcepacks 폴더의 팩 목록.
        반환: {"ok":True,"packs":[{name,filename,size,kind}]}"""
        import instance
        d = instance.resourcepacks_dir()
        packs = []
        try:
            os.makedirs(d, exist_ok=True)
            for entry in sorted(os.listdir(d)):
                p = os.path.join(d, entry)
                if os.path.isfile(p) and entry.lower().endswith(".zip"):
                    packs.append({"name": os.path.splitext(entry)[0],
                                  "filename": entry,
                                  "size": os.path.getsize(p), "kind": "zip"})
                elif os.path.isdir(p) and os.path.isfile(os.path.join(p, "pack.mcmeta")):
                    packs.append({"name": entry, "filename": entry,
                                  "size": 0, "kind": "folder"})
            return {"ok": True, "packs": packs}
        except Exception as e:
            return {"ok": False, "message": str(e), "packs": []}

    def open_resourcepacks_folder(self):
        """resourcepacks 폴더를 탐색기로 연다 (격리 인스턴스)."""
        import instance
        d = instance.resourcepacks_dir()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": f"폴더를 열 수 없습니다: {e}"}

    def add_resourcepack_data(self, filename, data_b64):
        """모달에서 드래그드롭한 리소스팩(zip, base64)을 폴더에 저장.

        검증: 마인크래프트가 인식하려면 zip 최상위에 pack.mcmeta 가 있어야 한다.
        없으면 거부하고 원인을 안내한다(폰트 파일 묶음이면 '폰트 생성' 유도).
        반환: {"ok":True,"name","filename"} / 실패 {"ok":False,"message"}"""
        try:
            import base64
            import io
            import zipfile

            import instance
            base = os.path.basename(filename or "").strip()
            if not base.lower().endswith(".zip"):
                return {"ok": False, "message": "zip 파일만 추가할 수 있습니다."}
            raw = base64.b64decode(data_b64)
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    names = zf.namelist()
            except zipfile.BadZipFile:
                return {"ok": False, "message": "올바른 zip 파일이 아닙니다."}
            # 유효한 리소스팩 = 루트에 pack.mcmeta
            if "pack.mcmeta" not in names:
                if any(n.lower().endswith((".ttf", ".otf")) for n in names):
                    return {"ok": False,
                            "message": "이 zip 은 리소스팩이 아니라 폰트 파일 묶음입니다.\n"
                                       "폰트를 쓰려면 '폰트 생성' 으로 .ttf 파일 하나를 넣어 "
                                       "리소스팩을 만들어주세요."}
                if any(n.endswith("/pack.mcmeta") for n in names):
                    return {"ok": False,
                            "message": "리소스팩 구조가 잘못되었습니다.\n"
                                       "pack.mcmeta 가 zip 최상위가 아니라 하위 폴더에 있습니다."}
                return {"ok": False, "message": "리소스팩이 아닙니다 (pack.mcmeta 가 없습니다)."}
            d = instance.resourcepacks_dir()
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, base), "wb") as fh:
                fh.write(raw)
            return {"ok": True, "name": os.path.splitext(base)[0], "filename": base}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def delete_resourcepack(self, filename):
        """리소스팩(zip 파일 또는 폴더)을 resourcepacks 폴더에서 제거."""
        try:
            import shutil

            import instance
            base = os.path.basename(filename or "")
            if not base:
                return {"ok": False, "message": "잘못된 파일명입니다."}
            p = os.path.join(instance.resourcepacks_dir(), base)
            if os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
            else:
                return {"ok": True, "status": "not_found"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def generate_font_pack(self, filename, data_b64):
        """ttf(base64) 로 폰트 리소스팩을 만들어 resourcepacks 폴더에 저장.
        프론트 폰트 생성 모달이 드래그드롭한 ttf 를 base64 로 넘긴다.
        반환: {"ok":True,"name","filename"} / 실패 {"ok":False,"message"}"""
        try:
            import base64

            import fontpack
            import instance
            raw = base64.b64decode(data_b64)
            path = fontpack.build_font_pack(raw, filename, instance.resourcepacks_dir())
            return {"ok": True,
                    "name": os.path.splitext(os.path.basename(path))[0],
                    "filename": os.path.basename(path)}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def open_release_page(self):
        """앱 릴리스 페이지(GitHub)를 기본 브라우저로 연다.
        업데이트 모달/버전 행의 '업데이트'에서 호출 → 유저가 새 버전을 직접 받음."""
        import webbrowser
        import urllib.parse as _up
        _, url = preflight.get_update_urls()
        if not url:
            return {"ok": False, "message": "업데이트 저장소가 아직 설정되지 않았습니다."}
        p = _up.urlsplit(url)
        if p.scheme != "https" or p.hostname != "github.com":
            return {"ok": False, "message": "잘못된 URL"}
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def open_support(self):
        """지원 채널(디스코드)을 기본 브라우저로 연다."""
        import webbrowser
        import urllib.parse as _up
        url = "https://discord.com/users/270224308867956736"
        p = _up.urlsplit(url)
        if p.scheme != "https" or p.hostname not in ("discord.com", "www.discord.com"):
            return {"ok": False, "message": "잘못된 URL"}
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def open_project_page(self, kind, item_id):
        """모달의 '프로젝트 페이지' — 해당 모드/셰이더의 Modrinth 페이지를 연다."""
        import webbrowser
        if kind == "shader":
            slug = item_id
        else:
            mod = MOD_BY_ID.get(item_id) or self._search_mod_by_id.get(item_id, {})
            slug = mod.get("slug")
        if not slug:
            return {"ok": False, "message": "이 항목은 Modrinth 페이지가 없습니다."}
        ptype = "shader" if kind == "shader" else "mod"
        url = f"https://modrinth.com/{ptype}/{slug}"
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def check_updates(self):
        """앱/Fabric 최신 버전 확인 (다운로드는 안 함, 정보만).

        반환: {"ok":True,"app":{status,current,latest,page_url},
               "fabric":{status,installed,latest}}
        """
        result = {"ok": True}
        try:
            result["app"] = preflight.check_app_update(app_meta.APP_VERSION)
        except Exception:
            result["app"] = {"status": "check_failed"}
        # Java: 요구 충족 + 최신 LTS 여부 종합 판단
        try:
            result["java"] = preflight.check_java_status(check_latest=True)
        except Exception:
            result["java"] = {"status": "check_failed"}
        try:
            # v3: 격리 인스턴스의 state.json 기준으로 판단 (mc_dir 캐시 영향 없음)
            import game_installer
            result["fabric"] = game_installer.check_loader_update()
        except Exception:
            result["fabric"] = {"status": "check_failed"}
        return result

    def reset_instance(self):
        """설치된 모드·셰이더를 제거하고 레지스트리를 비운다.
        (월드/설정은 보존. 프론트에서 2단계 확인 후 호출.)

        우리 레지스트리가 추적하는 모드 jar + 추적 중인 셰이더 파일만 제거해
        사용자가 별도로 넣은 파일은 건드리지 않는다.

        반환: {"ok":True,"removed_mods":n,"removed_shaders":m}
        """
        if not self._registry or not self._mods_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "message": r.get("reason", "준비 실패")}

        removed_mods = 0
        # 카탈로그 모드 + 자동 설치된 의존 모드(__dep__:*) 를 모두 제거한다.
        for mid in list(self._registry.installed_ids()):
            fn = self._registry.get_installed_filename(mid)
            if fn:
                path = os.path.join(self._mods_dir, fn)
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        removed_mods += 1
                    except OSError:
                        pass
            self._registry.record_remove(mid)
        self._installed_pids = set()
        self._mod_versions = {}

        removed_shaders = 0
        if self._shaderpacks_dir:
            for slug, info in list(self._shader_by_slug.items()):
                fn = (info or {}).get("filename")
                if fn:
                    path = os.path.join(self._shaderpacks_dir, fn)
                    if os.path.isfile(path):
                        try:
                            os.remove(path)
                            removed_shaders += 1
                        except OSError:
                            pass
            self._shader_by_slug = {}

        self._mod_statuses = {}
        preflight.write_log(
            f"[초기화] 모드 {removed_mods}개, 셰이더 {removed_shaders}개 제거")
        return {"ok": True, "removed_mods": removed_mods,
                "removed_shaders": removed_shaders}

    # ── 진행률 push (Python→JS) ───────────────────────────────────────────
    def _push_progress(self, item_id, pct):
        """다운로드 진행률을 JS 의 window.onProgress(id, pct) 로 밀어넣는다.
        app.py 에서 self._window 를 주입해두었고, pywebview 는 노출 메서드를
        별도 스레드에서 실행하므로 여기서 evaluate_js 를 호출해도 UI 가 멈추지
        않는다. 정수 % 가 바뀔 때만 호출되도록 호출부에서 중복 제거한다."""
        w = self._window
        if not w:
            return
        try:
            import json as _json
            w.evaluate_js(
                "window.onProgress && window.onProgress(%s, %d)"
                % (_json.dumps(str(item_id)), int(pct))
            )
        except Exception:
            pass  # 진행률 push 실패가 설치 자체를 막지 않도록 무시

    def _progress_cb_for(self, item_id):
        """item_id 에 묶인 progress_cb 를 만든다. 정수 % 중복은 걸러낸다."""
        last = {"pct": -1}
        def cb(pct):
            p = int(pct)
            if p != last["pct"]:
                last["pct"] = p
                self._push_progress(item_id, p)
        return cb

    # ── 약관 동의 (최초 실행 게이트) ──────────────────────────────────
    def get_terms_status(self):
        """약관 동의 여부. 약관이 개정되면(TERMS_VERSION 상승) 다시 물어본다."""
        cfg = self._config or preflight.load_config()
        accepted = cfg.get("terms_accepted_version")
        return {
            "ok": True,
            "accepted": accepted == app_meta.TERMS_VERSION,
            "version": app_meta.TERMS_VERSION,
            "terms_url": app_meta.TERMS_URL,
            "notices_url": app_meta.NOTICES_URL,
        }

    def accept_terms(self):
        """사용자가 약관에 동의함을 기록한다."""
        import time as _time
        try:
            cfg = self._config or preflight.load_config()
            cfg["terms_accepted_version"] = app_meta.TERMS_VERSION
            cfg["terms_accepted_at"] = int(_time.time())
            preflight.save_config(cfg)
            self._config = cfg
            preflight.write_log(f"[약관동의] v{app_meta.TERMS_VERSION}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def open_terms_page(self):
        return {"ok": preflight.open_url_in_browser(app_meta.TERMS_URL)}

    def open_licenses_page(self):
        return {"ok": preflight.open_url_in_browser(app_meta.NOTICES_URL)}

    def get_versions(self):
        """앱 / Fabric / Java 버전 정보를 반환.

        System 화면의 'Versions & updates' 섹션에 대응.
        네트워크가 필요한 최신 버전 확인은 별도 메서드로 분리하고,
        여기서는 로컬에서 즉시 알 수 있는 현재 버전 위주로 반환한다.
        """
        # PATH 의 java 가 구버전(예: Java 8)이어도, 실제로 사용할 21+ Java 를
        # 찾아 표시한다. 없으면 PATH 버전(부적합)이라도 보여주고, 그것도
        # 없으면 '미설치'.
        valid = preflight.find_valid_java()
        if valid:
            java_ver = valid["version"]
        else:
            java_ver = preflight.get_java_version_string() or "미설치"
        app_ver = app_meta.APP_VERSION

        fabric_ver = "확인 필요"
        try:
            # 설치가 끝났다면 state.json 에 기록된 로더 버전이 가장 정확하다.
            # (get_versions 가 캐시된 mc_dir 을 보다가 '미설치'로 오표시되는 것 방지)
            import instance as _inst
            st = _inst.load_state()
            if _inst.is_version_ready() and st.get("fabric_loader_version"):
                fabric_ver = st["fabric_loader_version"]
            else:
                mc_dir = self._minecraft_dir or preflight.find_minecraft_dir(self._config)
                if mc_dir:
                    info = preflight.check_fabric_loader_update(mc_dir, GAME_VERSION)
                    fabric_ver = info.get("installed") or "미설치"
        except Exception:
            fabric_ver = "확인 불가"

        return {
            "app": app_ver,
            "app_channel": getattr(app_meta, "APP_CHANNEL", "release"),
            "java": java_ver,
            "fabric": fabric_ver,
            "game_version": GAME_VERSION,
        }

    # ══════════════════════════════════════════════════════════════════════
    #  준비 / 카탈로그
    # ══════════════════════════════════════════════════════════════════════

    def ensure_ready(self):
        """모드 작업에 필요한 경로(minecraft_dir/mods_dir)와 레지스트리 준비.

        전체 사전 점검(run_preflight: Java/Fabric 자동설치)과 달리,
        여기서는 '경로 확인'만 한다. Java/Fabric 설치 마법사는 사전 점검
        화면(다음 단계)에서 별도로 다룬다. 모드 목록/설치는 mods 폴더만
        있으면 되므로 여기서 가볍게 준비한다.

        반환:
          {"ready": True,  "minecraft_dir":..., "mods_dir":...}
          {"ready": False, "reason": "마인크래프트 경로를 찾을 수 없음"}
        """
        # v3: 격리 인스턴스 경로 사용 (기본 .minecraft 런처가 아니라 우리 인스턴스).
        # mods/shaderpacks 가 전부 instances/dongleland/ 아래로 들어간다.
        import instance
        instance.ensure_dirs()
        mc_dir = instance.instance_dir()

        mods_dir = instance.mods_dir()
        os.makedirs(mods_dir, exist_ok=True)

        self._minecraft_dir = mc_dir
        self._mods_dir = mods_dir
        self._shaderpacks_dir = instance.shaderpacks_dir()
        os.makedirs(self._shaderpacks_dir, exist_ok=True)
        self._resourcepacks_dir = instance.resourcepacks_dir()
        os.makedirs(self._resourcepacks_dir, exist_ok=True)
        self._registry = modrinth_api.ModRegistry(mods_dir)
        self._resourcepack_registry = modrinth_api.ModRegistry(self._resourcepacks_dir)

        # 예전 낙인/윤슬 장부 JAR이 남아 있으면 같은 Java 클래스가 먼저 로드되어
        # 새 윤띵의 표시명과 /윤띵 명령이 사라질 수 있다. 기존 사용자는 새 번들로
        # 자동 마이그레이션하고, 충돌하는 구형 파일은 제거한다.
        self._migrate_legacy_ledger()

        # 경로를 config 에 저장 (다음 실행에서 재사용)
        if self._config.get("minecraft_dir") != mc_dir:
            self._config["minecraft_dir"] = mc_dir
            preflight.save_config(self._config)

        return {"ready": True, "minecraft_dir": mc_dir, "mods_dir": mods_dir}

    def _migrate_legacy_ledger(self):
        """구형 낙인/윤슬 장부를 현재 윤띵 번들로 안전하게 교체한다."""
        if not self._mods_dir or not self._registry:
            return
        current = "yoonseul-ledger-fabric-0.2.2.jar"
        found_legacy = False
        try:
            for filename in list(os.listdir(self._mods_dir)):
                low = filename.lower()
                legacy = (
                    (low.startswith("nakin-") and low.endswith(".jar"))
                    or (low.startswith("yoonseul-ledger-fabric-")
                        and low.endswith(".jar") and filename != current)
                    or (low.startswith("yoonseulledger_") and low.endswith(".jar"))
                )
                if not legacy:
                    continue
                os.remove(os.path.join(self._mods_dir, filename))
                found_legacy = True
                preflight.write_log(f"[마이그레이션] 구형 장부 제거: {filename}")
        except OSError as e:
            preflight.write_log(f"[마이그레이션실패] 구형 장부 정리: {e}")

        if found_legacy:
            self._registry.record_remove("nakin")
            ok, reason = self._install_single("yoonseul-ledger")
            if ok:
                preflight.write_log("[마이그레이션] 구형 낙인/장부를 윤띵 0.2.2로 교체")
            else:
                preflight.write_log(f"[마이그레이션실패] 윤띵 설치: {reason}")

    def get_catalog(self):
        """모드 카탈로그 반환. 프로세스 첫 호출에서만 원격 허용 정책을 1회 받아 덮어쓴다(5초 타임아웃).

        UI 최초 렌더용. 설치 상태(status)는 아직 모르므로 넣지 않는다.
        상태는 scan_mods() 로 뒤이어 채운다.
        각 항목: id, name, category, required, tags, description,
                 exclusive_group, dependencies, has_slug
        """
        if not getattr(self, "_policy_applied", False):
            # 허용 모드 원격 목록 → 카탈로그에 덮어쓰기 (실패 시 마지막 성공값/카탈로그 그대로)
            self._policy_applied = True
            pol = preflight.fetch_mod_policy(self._config)
            mod_catalog.apply_policy(pol, modrinth_api.get_projects_media_batch)
        if not self._registry:
            self.ensure_ready()

        def _listed(m):
            # 정책에서 빠진 모드는 목록에서 뺀다. 이미 설치돼 있으면 남겨서 제거할 수 있게.
            if not m.get("off_policy"):
                return True
            return bool(self._registry and self._registry.is_installed(m["id"]))

        items = []
        for m in VISIBLE_MODS:
            if not _listed(m):
                continue
            tags = list(m.get("tags", []))
            if "추천" not in tags:
                tags.append("추천")
            items.append({
                "id": m["id"],
                "name": m["name"],
                "category": m["category"],
                "required": m.get("required", False),
                "tags": tags,
                "description": m.get("description", ""),
                "exclusive_group": m.get("exclusive_group"),
                "dependencies": m.get("dependencies", []),
                "has_slug": bool(m.get("slug")),
                "kind": m.get("kind", "mod"),
                "author": m.get("author"),
                "size_label": m.get("size_label"),
                "version": m.get("version"),
                "banned": bool(m.get("banned")),
            })
        return {
            "mods": items,
            "categories": CATEGORIES,
            "category_color": CATEGORY_COLOR,
            "game_version": GAME_VERSION,
            "loader": LOADER,
        }

    def search_mods(self, query=""):
        """추천 목록과 별개로 Modrinth 전체 모드를 실시간 검색한다."""
        q = str(query or "").strip()
        if len(q) < 2:
            return {"ok": True, "mods": [], "total": 0}
        if not self._registry or not self._mods_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "reason": r.get("reason", "준비 실패")}
        res = modrinth_api.search_mods(GAME_VERSION, LOADER, limit=40, query=q)
        if res.get("error"):
            return {"ok": False, "reason": f"Modrinth 검색 실패: {res['error']}"}
        out = []
        self._search_mod_by_id = {}
        for m in res.get("mods", []):
            pid, slug = m.get("project_id"), m.get("slug")
            if not pid or not slug:
                continue
            mid = f"mr:{pid}"
            filename = self._registry.get_installed_filename(mid)
            installed = bool(filename and os.path.isfile(os.path.join(self._mods_dir, filename)))
            meta = dict(m)
            meta.update({"id": mid, "status": "installed" if installed else "not_installed",
                         "filename": filename if installed else None})
            self._search_mod_by_id[mid] = meta
            out.append(meta)
        return {"ok": True, "mods": out, "total": res.get("total", len(out))}

    # ══════════════════════════════════════════════════════════════════════
    #  스캔 (설치/업데이트 상태) — tkinter _scan_and_check 이식
    # ══════════════════════════════════════════════════════════════════════

    def scan_mods(self):
        """mods 폴더 스캔 → 설치 감지 → 업데이트 확인.

        tkinter _scan_and_check 와 동일 원리:
          1) 번들 모드(slug 없음) 즉시 처리
          2) slug→project_id 맵 (batch API, 캐시)
          3) jar 별 sha1 → get_version_from_hash → project_id 특정
          4) 설치 파일 sha1 vs 최신 파일 sha1 비교 → installed/update

        반환: {"ok": True, "statuses": {mod_id: "installed|update|not_installed"}}
              준비 안 됐으면 {"ok": False, "reason": ...}
        """
        if not self._registry or not self._mods_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "reason": r.get("reason", "준비 실패")}

        mods_dir = self._mods_dir
        os.makedirs(mods_dir, exist_ok=True)

        # Step 1: 번들 모드
        for mod in MODS:
            if mod.get("slug"):
                continue
            mid = mod["id"]
            ba = mod.get("bundled_asset")
            if ba and os.path.isfile(os.path.join(mods_dir, ba)):
                # 바이트까지 정본이어야 한다 — 서버 모드검사(modcheckclient)는 클라가 자기 jar
                # 바이트 전체를 HMAC 으로 서명해 보내게 해서, 이름·버전이 같아도 바이트가 다르면
                # "잘못된 핸드셰이크 응답"으로 추방된다. 다르면 번들본으로 덮어 복구한다.
                src = _resource(os.path.join("assets", ba))
                dst = os.path.join(mods_dir, ba)
                if os.path.isfile(src) and not filecmp.cmp(src, dst, shallow=False):
                    try:
                        shutil.copyfile(src, dst)
                        preflight.write_log(f"[복구] 번들 모드 {ba} 가 정본과 달라 다시 배치")
                    except OSError as e:
                        preflight.write_log(f"[복구실패] {ba}: {e}")
                if not self._registry.is_installed(mid):
                    self._registry.record_install(mid, ba, "1.2.0", None)
            self._mod_statuses[mid] = (
                "installed" if self._registry.is_installed(mid) else "not_installed"
            )

        # Step 2: slug → project_id 맵
        all_slugs = [m["slug"] for m in MODS if m.get("slug")]
        missing = [s for s in all_slugs if s not in self._pid_cache]
        if missing:
            try:
                fresh = modrinth_api.get_projects_batch(missing)
            except Exception:
                fresh = {}
            if fresh:
                self._pid_cache.update(fresh)
                self._config["_pid_cache"] = self._pid_cache
                preflight.save_config(self._config)

        # Step 3: jar 스캔 → sha1 → project_id  (배치 1회)
        # 이전에는 jar 하나당 get_version_from_hash 를 순차 호출했다(N회).
        # POST /version_files 로 전체 해시를 한 번에 조회한다.
        installed_by_pid = {}
        sha1_to_jar = {}
        required_pids = set()
        jars = []
        # 조회 실패(네트워크·429)를 '모드 없음'으로 읽으면 설치된 모드가 0개로 보이고
        # 설치 기록까지 지워진다. 실패면 레지스트리(마지막으로 확인된 상태)를 믿는다.
        lookup_ok = True
        if os.path.isdir(mods_dir):
            for jar in os.listdir(mods_dir):
                if not jar.lower().endswith(".jar") or jar.startswith("."):
                    continue
                jars.append(jar)
                path = os.path.join(mods_dir, jar)
                try:
                    local_sha1 = modrinth_api.sha1_of_file_cached(path)
                except Exception:
                    continue
                sha1_to_jar[local_sha1] = jar

        if sha1_to_jar:
            found = modrinth_api.get_versions_from_hashes(list(sha1_to_jar.keys()))
            if found is None:
                lookup_ok = False
            for h, info in (found or {}).items():
                if not info or not info.get("project_id"):
                    continue
                installed_by_pid[info["project_id"]] = (
                    sha1_to_jar.get(h, ""), h, info.get("version_number", "?"))
            # 설치된 jar 들이 요구하는 필수 의존 project_id 집합
            # (레지스트리에 없던 기존 의존 모드도 여기서 잡아내 초기화 대상으로 만든다)
            for info in (found or {}).values():
                for pid in modrinth_api.required_dependency_project_ids(info or {}):
                    required_pids.add(pid)

        # Step 4: 설치된 것들의 최신 버전을 배치로 확인 (1회)
        # 이전에는 모드마다 get_latest_compatible_version 을 순차 호출했다(N회).
        installed_hashes = [h for (_j, h, _v) in installed_by_pid.values()]
        latest_by_hash = modrinth_api.get_latest_versions_from_hashes(
            installed_hashes, GAME_VERSION, LOADER) if installed_hashes else {}

        def _latest_sha1_for(local_sha1):
            latest = latest_by_hash.get(local_sha1)
            if not latest:
                return None
            primary = modrinth_api.pick_primary_file(latest)
            return primary.get("hashes", {}).get("sha1") if primary else None

        # 설치된 버전 / 최신 버전 번호 (UI 표시용)
        self._mod_versions = {}
        # 카탈로그에 없는 의존 모드도 포함해, 실제 설치된 project_id 집합
        self._installed_pids = set(installed_by_pid.keys())

        # 카탈로그가 관리하는 project_id
        catalog_pids = {self._pid_cache.get(m.get("slug"))
                        for m in MODS
                        if m.get("slug") and m.get("kind", "mod") == "mod"}
        catalog_pids.discard(None)

        # 카탈로그에 없지만 '설치된 모드가 필수로 요구하는' jar → 의존 모드로 등록.
        # (사용자가 직접 넣은 무관한 모드는 required_pids 에 없으므로 건드리지 않는다)
        for pid, (jar, _h, ver) in installed_by_pid.items():
            if pid in catalog_pids or pid not in required_pids:
                continue
            self._registry.record_install(_dep_key(pid), jar, ver, pid)

        for mod in VISIBLE_MODS:
            mid = mod["id"]
            slug = mod.get("slug")
            if mod.get("kind", "mod") == "resourcepack":
                continue
            if not slug:
                continue  # Step 1 처리됨

            pid = self._pid_cache.get(slug)
            if pid and pid in installed_by_pid:
                jar_file, local_sha1, ver = installed_by_pid[pid]
                self._registry.record_install(mid, jar_file, ver, pid)
                latest_sha1 = _latest_sha1_for(local_sha1)
                latest_ver = (latest_by_hash.get(local_sha1) or {}).get("version_number")
                self._mod_versions[mid] = {"installed": ver, "latest": latest_ver or ver}
                if latest_sha1 and local_sha1 != latest_sha1:
                    self._mod_statuses[mid] = "update"
                else:
                    self._mod_statuses[mid] = "installed"
            elif not lookup_ok or not pid:
                # 확인을 못 한 것이지 지워진 게 아니다 → 기록 + 파일 존재가 진실
                if self._registry.is_installed(mid):
                    ver = self._registry.get_installed_version(mid)
                    self._mod_statuses[mid] = "installed"
                    self._mod_versions[mid] = {"installed": ver, "latest": ver}
                else:
                    self._mod_statuses[mid] = "not_installed"
            else:
                if self._registry.is_installed(mid):
                    self._registry.record_remove(mid)
                self._mod_statuses[mid] = "not_installed"

        # Step 5: 추천 리소스팩은 resourcepacks 폴더에서 별도로 추적한다.
        # 모드 레지스트리와 섞으면 .zip을 mods 폴더에서 찾게 되어 매번 미설치로
        # 보이므로 전용 레지스트리를 사용한다.
        resourcepacks = [m for m in VISIBLE_MODS
                         if m.get("kind", "mod") == "resourcepack"]
        rp_hash_to_file = {}
        if self._resourcepacks_dir:
            for filename in os.listdir(self._resourcepacks_dir):
                if not filename.lower().endswith(".zip") or filename.startswith("."):
                    continue
                try:
                    h = modrinth_api.sha1_of_file_cached(
                        os.path.join(self._resourcepacks_dir, filename))
                    rp_hash_to_file[h] = filename
                except Exception:
                    continue
        rp_found = (modrinth_api.get_versions_from_hashes(list(rp_hash_to_file))
                    if rp_hash_to_file else {})
        rp_lookup_ok = rp_found is not None
        rp_by_pid = {}
        for h, info in (rp_found or {}).items():
            if info and info.get("project_id"):
                rp_by_pid[info["project_id"]] = (
                    rp_hash_to_file.get(h, ""), h, info.get("version_number", "?"))
        rp_latest = modrinth_api.get_latest_versions_from_hashes(
            [h for (_f, h, _v) in rp_by_pid.values()], GAME_VERSION, None
        ) if rp_by_pid else {}
        for mod in resourcepacks:
            mid, slug = mod["id"], mod.get("slug")
            pid = self._pid_cache.get(slug)
            if pid and pid in rp_by_pid:
                filename, local_sha1, ver = rp_by_pid[pid]
                self._resourcepack_registry.record_install(mid, filename, ver, pid)
                latest = rp_latest.get(local_sha1) or {}
                primary = modrinth_api.pick_primary_file(latest) if latest else None
                latest_sha1 = primary.get("hashes", {}).get("sha1") if primary else None
                latest_ver = latest.get("version_number") or ver
                self._mod_versions[mid] = {"installed": ver, "latest": latest_ver}
                self._mod_statuses[mid] = (
                    "update" if latest_sha1 and latest_sha1 != local_sha1 else "installed")
            elif not rp_lookup_ok or not pid:
                if self._resourcepack_registry.is_installed(mid):
                    ver = self._resourcepack_registry.get_installed_version(mid)
                    self._mod_statuses[mid] = "installed"
                    self._mod_versions[mid] = {"installed": ver, "latest": ver}
                else:
                    self._mod_statuses[mid] = "not_installed"
            else:
                self._resourcepack_registry.record_remove(mid)
                self._mod_statuses[mid] = "not_installed"

        # 카탈로그·의존 모드·번들 어디에도 기록되지 않은 jar = 사용자가 직접 넣은 모드
        claimed = self._registry.filenames()
        extra = []
        for jar in sorted(jars, key=str.lower):
            if jar in claimed:
                continue
            try:
                size = os.path.getsize(os.path.join(mods_dir, jar))
            except OSError:
                size = 0
            extra.append({"filename": jar, "size": size})

        return {"ok": True, "statuses": dict(self._mod_statuses),
                "versions": dict(getattr(self, "_mod_versions", {})),
                "extra": extra, "lookup_ok": lookup_ok}

    # ══════════════════════════════════════════════════════════════════════
    #  설치 / 제거 / 업데이트 — tkinter _install_single/_do_* 이식
    # ══════════════════════════════════════════════════════════════════════

    def _install_single(self, mod_id):
        """모드 1개 설치. (성공여부, 실패사유) 튜플 반환. (내부용)

        tkinter _install_single 과 동일. UI(after/messagebox) 만 제거.
        """
        mod = MOD_BY_ID.get(mod_id) or self._search_mod_by_id.get(mod_id)
        if not mod or not self._mods_dir:
            return (False, "내부 오류: 모드 정보 또는 경로 준비 안 됨")
        is_resourcepack = mod.get("kind", "mod") == "resourcepack"
        target_dir = self._resourcepacks_dir if is_resourcepack else self._mods_dir
        registry = self._resourcepack_registry if is_resourcepack else self._registry
        os.makedirs(target_dir, exist_ok=True)
        name = mod.get("name") or mod.get("title") or mod_id

        # 번들 모드 (modcheckclient 등)
        if mod.get("bundled_asset"):
            src = _resource(os.path.join("assets", mod["bundled_asset"]))
            dst = os.path.join(target_dir, mod["bundled_asset"])
            if not os.path.abspath(dst).startswith(os.path.abspath(target_dir) + os.sep):
                reason = "비정상적인 대상 경로"
                preflight.write_log(f"[설치실패] {name}: {reason}")
                return (False, reason)
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                reason = f"번들 파일 복사 실패: {e}"
                preflight.write_log(f"[설치실패] {name}: {reason}")
                return (False, reason)
            registry.record_install(
                mod_id, mod["bundled_asset"], mod.get("bundled_version", "bundled"), None)
            preflight.write_log(f"[설치성공] {name} (번들)")
            return (True, None)

        slug = mod.get("slug")
        if not slug:
            reason = "slug 정보 없음 (카탈로그 오류)"
            preflight.write_log(f"[설치실패] {name}: {reason}")
            return (False, reason)

        try:
            if is_resourcepack:
                result = modrinth_api.install_resourcepack_by_slug(
                    slug, target_dir, GAME_VERSION,
                    progress_cb=self._progress_cb_for(mod_id),
                )
            else:
                result = modrinth_api.install_mod_by_slug(
                    slug, target_dir, GAME_VERSION, LOADER,
                    progress_cb=self._progress_cb_for(mod_id),
                )
        except Exception as e:
            reason = f"예기치 못한 오류: {e}"
            preflight.write_log(f"[설치실패] {name} (slug={slug}): {reason}")
            return (False, reason)

        if result["status"] in ("installed", "up_to_date"):
            registry.record_install(
                mod_id, result["filename"], result["version"],
                result.get("project_id"),
            )
            self._last_install = {"version": result["version"],
                                  "requires": result.get("requires", [])}
            preflight.write_log(
                f"[설치성공] {name} v{result['version']} ({result['status']})"
            )
            return (True, None)

        reason = result.get("message", f"알 수 없는 오류 (status={result['status']})")
        preflight.write_log(f"[설치실패] {name} (slug={slug}): {reason}")
        return (False, reason)

    def _korean_conflict(self, mod_id):
        """설치하려는 한글 모드가 배타 그룹 충돌이면 충돌 상대 id 반환, 없으면 None.
        (내부용) tkinter _do_install 의 한글 배타 그룹 검사 이식."""
        mod = MOD_BY_ID.get(mod_id, {})
        if mod.get("exclusive_group") != "korean":
            return None
        group_ids = [m["id"] for m in MODS if m.get("exclusive_group") == "korean"]
        for gid in group_ids:
            if gid == mod_id:
                continue
            if (self._registry and self._registry.is_installed(gid)) or \
               self._mod_statuses.get(gid) in ("installed", "update"):
                return gid
        return None

    def list_versions(self, item_id, kind="mod"):
        """모드/셰이더의 설치 가능한 버전 목록 (최신순).

        kind: "mod" | "shader"
        반환: {"ok":True, "versions":[{id, name, version_number, version_type,
               date, is_current(bool)}...]}
        """
        try:
            if kind == "shader":
                slug = item_id
                loader = None
                info = (self._shader_by_slug or {}).get(slug) or {}
                cur_file = info.get("version")
            else:
                mod = MOD_BY_ID.get(item_id) or self._search_mod_by_id.get(item_id)
                if not mod:
                    return {"ok": False, "message": f"알 수 없는 모드: {item_id}"}
                is_resourcepack = mod.get("kind", "mod") == "resourcepack"
                slug = mod.get("slug")
                if not slug:
                    # 번들 모드(자체 배포) — Modrinth 버전 목록이 없다
                    return {"ok": False, "message": "이 모드는 버전 선택을 지원하지 않습니다."}
                loader = None if is_resourcepack else modrinth_api.DEFAULT_LOADER
                registry = self._resourcepack_registry if is_resourcepack else self._registry
                cur_file = (registry.get_installed_version(item_id)
                            if registry else None)

            vs = modrinth_api.get_project_versions(slug, GAME_VERSION, loader)
            vs.sort(key=modrinth_api._parse_date, reverse=True)
            out = []
            for v in vs[:30]:   # 너무 길어지지 않게 최근 30개
                vnum = v.get("version_number", "?")
                out.append({
                    "id": v.get("id"),
                    "name": v.get("name") or vnum,
                    "version_number": vnum,
                    "version_type": v.get("version_type", "release"),
                    "date": (v.get("date_published") or "")[:10],
                    "is_current": bool(cur_file and cur_file == vnum),
                })
            return {"ok": True, "versions": out}
        except Exception as e:
            return {"ok": False, "message": f"버전 목록을 가져오지 못했습니다: {e}"}

    def install_version(self, item_id, version_id, kind="mod"):
        """특정 버전으로 설치/되돌리기. 기존 파일은 먼저 제거해 중복을 막는다.

        반환: {"ok":True, "version":...} 또는 {"ok":False, "message":...}
        """
        if not self._registry or not self._mods_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "message": r.get("reason", "준비 실패")}
        try:
            if kind == "shader":
                # 기존 셰이더 파일 제거 후 지정 버전 설치 (중복 방지)
                target = self._shaderpacks_dir
                info = (self._shader_by_slug or {}).get(item_id) or {}
                old = info.get("filename")
                if old and target:
                    try:
                        os.remove(os.path.join(target, old))
                    except Exception:
                        pass
                res = modrinth_api.install_shader_by_slug(
                    item_id, target, GAME_VERSION, version_id=version_id)
            else:
                mod = MOD_BY_ID.get(item_id) or self._search_mod_by_id.get(item_id)
                if not mod:
                    return {"ok": False, "message": f"알 수 없는 모드: {item_id}"}
                is_resourcepack = mod.get("kind", "mod") == "resourcepack"
                target = self._resourcepacks_dir if is_resourcepack else self._mods_dir
                registry = self._resourcepack_registry if is_resourcepack else self._registry
                slug = mod.get("slug")
                if not slug:
                    return {"ok": False, "message": "이 모드는 버전 선택을 지원하지 않습니다."}
                # 기존 jar 제거 (버전이 다르면 파일명도 달라 중복 설치됨)
                old = registry.get_installed_filename(item_id)
                if old:
                    try:
                        os.remove(os.path.join(target, old))
                    except Exception:
                        pass
                    registry.record_remove(item_id)
                if is_resourcepack:
                    res = modrinth_api.install_resourcepack_by_slug(
                        slug, target, GAME_VERSION, version_id=version_id)
                else:
                    res = modrinth_api.install_mod_by_slug(
                        slug, target, GAME_VERSION,
                        modrinth_api.DEFAULT_LOADER, version_id=version_id)

            if res.get("status") in ("installed", "up_to_date"):
                if kind != "shader":
                    registry = (self._resourcepack_registry
                                if (MOD_BY_ID.get(item_id) or {}).get("kind") == "resourcepack"
                                else self._registry)
                    registry.record_install(
                        item_id, res.get("filename"), res.get("version"))
                # 설치한 버전이 최신인지 판정해 상태를 함께 돌려준다.
                # (구버전으로 되돌리면 '업데이트' 로 보여야 한다)
                status, latest_ver = self._status_after_install(item_id, kind, res)
                deps = []
                if kind == "shader":
                    # ⚠️ 셰이더 파일명을 기록하지 않으면 remove_shader 가
                    #    "설치된 셰이더 파일을 찾을 수 없습니다" 로 실패한다.
                    self._shader_by_slug.setdefault(item_id, {})["filename"] = res.get("filename")
                    if res.get("project_id"):
                        self._shader_by_slug[item_id]["project_id"] = res.get("project_id")
                    self._shader_by_slug[item_id]["version"] = res.get("version")
                else:
                    self._mod_statuses[item_id] = status
                    self._mod_versions[item_id] = {
                        "installed": res.get("version"), "latest": latest_ver}
                    # 되돌린 버전이 요구하는 필수 의존 모드도 맞춰준다
                    deps, _f = ([], []) if registry is self._resourcepack_registry else \
                        self._install_modrinth_deps(res.get("requires", []))
                return {"ok": True, "version": res.get("version"),
                        "status": status, "latest": latest_ver, "deps": deps,
                        "filename": res.get("filename")}
            return {"ok": False, "message": res.get("message", "설치에 실패했습니다.")}
        except Exception as e:
            return {"ok": False, "message": f"설치 중 오류: {e}"}

    def _status_after_install(self, item_id, kind, res):
        """방금 설치한 파일이 최신인지 확인 → ("installed"|"update", 최신버전번호).

        설치된 파일의 sha1 을 최신 호환 버전의 sha1 과 비교한다.
        확인에 실패하면 보수적으로 ("installed", 설치버전).
        """
        installed_ver = res.get("version")
        try:
            filename = res.get("filename")
            catalog_item = MOD_BY_ID.get(item_id) or {}
            is_resourcepack = catalog_item.get("kind", "mod") == "resourcepack"
            target = (self._shaderpacks_dir if kind == "shader" else
                      self._resourcepacks_dir if is_resourcepack else self._mods_dir)
            if not filename or not target:
                return ("installed", installed_ver)
            path = os.path.join(target, filename)
            if not os.path.isfile(path):
                return ("installed", installed_ver)

            local_sha1 = modrinth_api.sha1_of_file_cached(path)
            loader = None if kind == "shader" or is_resourcepack else LOADER
            latest_map = modrinth_api.get_latest_versions_from_hashes(
                [local_sha1], GAME_VERSION, loader)
            latest = latest_map.get(local_sha1.lower())
            if not latest:
                return ("installed", installed_ver)
            latest_ver = latest.get("version_number") or installed_ver
            primary = modrinth_api.pick_primary_file(latest)
            latest_sha1 = primary.get("hashes", {}).get("sha1") if primary else None
            if latest_sha1 and latest_sha1.lower() != local_sha1.lower():
                return ("update", latest_ver)
            return ("installed", latest_ver)
        except Exception:
            return ("installed", installed_ver)

    def _install_modrinth_deps(self, requires, depth=0, seen=None):
        """Modrinth 가 알려준 필수 의존 모드를 설치.

        카탈로그(mod_catalog.py)의 dependencies 는 손으로 채워야 해서 비어 있다.
        → Modrinth 버전 JSON 의 dependencies(required)를 그대로 따라간다.
        카탈로그에 없는 모드(YACL, fzzy_config, malilib 등)도 설치한다.

        반환: (설치된 이름 목록, 실패 메시지 목록)
        """
        if not requires or depth > 3 or not self._mods_dir:
            return ([], [])
        seen = seen if seen is not None else set()

        pending = [p for p in requires if p not in seen]
        if not pending:
            return ([], [])
        seen.update(pending)

        meta = modrinth_api.get_projects_meta(pending)   # 배치 1회
        installed, failed = [], []
        for pid in pending:
            info = meta.get(pid)
            if not info:
                continue
            slug = info["slug"]
            # 이미 mods 폴더에 있으면 건너뛴다 (파일명 기준이 아니라 project_id 기준)
            if self._pid_installed(pid):
                continue
            try:
                res = modrinth_api.install_mod_by_slug(
                    slug, self._mods_dir, GAME_VERSION, LOADER)
            except Exception as e:
                failed.append(f"{info['title']}: {e}")
                continue
            if res.get("status") in ("installed", "up_to_date"):
                # 의존 모드도 레지스트리에 기록해야 초기화 때 지워지고,
                # 새 버전으로 갱신될 때 이전 jar 가 남지 않는다.
                dep_key = _dep_key(pid)
                old_file = self._registry.get_installed_filename(dep_key)
                new_file = res.get("filename")
                if old_file and new_file and old_file != new_file:
                    old_path = os.path.join(self._mods_dir, old_file)
                    if os.path.isfile(old_path):
                        try:
                            os.remove(old_path)
                        except OSError:
                            pass
                self._registry.record_install(
                    dep_key, new_file, res.get("version", "?"), pid)
                try:
                    self._installed_pids.add(pid)
                except Exception:
                    pass

                installed.append(info["title"])
                # 의존의 의존까지 (깊이 제한)
                sub_i, sub_f = self._install_modrinth_deps(
                    res.get("requires", []), depth + 1, seen)
                installed += sub_i
                failed += sub_f
            else:
                failed.append(f"{info['title']}: {res.get('message', '설치 실패')}")
        return (installed, failed)

    def _pid_installed(self, project_id) -> bool:
        """project_id 의 모드가 이미 mods 폴더에 있는지 (sha1 배치 조회 결과 캐시)."""
        try:
            cache = getattr(self, "_installed_pids", None)
            if cache is None:
                return False
            return project_id in cache
        except Exception:
            return False

    def install_mod(self, mod_id):
        """모드 설치 (종속 모드 자동 설치 + 한글 배타 그룹 검사).

        tkinter _do_install 이식. 반환 dict 로 결과 전달:
          성공: {"ok": True, "status": "installed", "installed": [id, ...]}
          한글충돌: {"ok": False, "error": "korean_conflict",
                    "conflict_id":..., "conflict_name":..., "message":...}
          종속실패: {"ok": False, "error": "dependency_failed",
                    "dep_id":..., "dep_name":..., "message":...}
          실패: {"ok": False, "error": "install_failed", "message":...}
        """
        if not self._registry:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "error": "not_ready",
                        "message": r.get("reason", "준비 실패")}

        mod = MOD_BY_ID.get(mod_id) or self._search_mod_by_id.get(mod_id)
        if not mod:
            return {"ok": False, "error": "unknown_mod",
                    "message": f"알 수 없는 모드: {mod_id}"}

        # 한글 배타 그룹
        conflict = self._korean_conflict(mod_id)
        if conflict:
            other_name = MOD_BY_ID[conflict]["name"]
            return {
                "ok": False,
                "error": "korean_conflict",
                "conflict_id": conflict,
                "conflict_name": other_name,
                "message": f"한글 모드는 1개만 설치할 수 있습니다. "
                           f"현재 '{other_name}'이(가) 설치되어 있습니다. "
                           f"먼저 제거한 후 설치해주세요.",
            }

        installed_now = []

        # 종속 모드 먼저
        for dep_id in mod.get("dependencies", []):
            if not self._registry.is_installed(dep_id):
                dep_ok, dep_reason = self._install_single(dep_id)
                if dep_ok:
                    self._mod_statuses[dep_id] = "installed"
                    installed_now.append(dep_id)
                else:
                    dep_name = MOD_BY_ID.get(dep_id, {}).get("name", dep_id)
                    return {
                        "ok": False,
                        "error": "dependency_failed",
                        "dep_id": dep_id,
                        "dep_name": dep_name,
                        "message": f"{mod.get('name') or mod.get('title') or mod_id}의 필수 종속 모드 "
                                   f"'{dep_name}' 설치에 실패했습니다. 원인: {dep_reason}",
                    }

        ok, reason = self._install_single(mod_id)
        if ok:
            self._mod_statuses[mod_id] = "installed"
            installed_now.append(mod_id)

            last = getattr(self, "_last_install", {}) or {}
            version = last.get("version")
            # Modrinth 가 선언한 필수 의존 모드 설치 (카탈로그에 없어도)
            dep_names, dep_fail = self._install_modrinth_deps(last.get("requires", []))

            self._mod_versions[mod_id] = {"installed": version, "latest": version}
            return {"ok": True, "status": "installed", "installed": installed_now,
                    "version": version, "deps": dep_names, "dep_failed": dep_fail}

        return {
            "ok": False,
            "error": "install_failed",
            "message": f"{mod.get('name') or mod.get('title') or mod_id} 설치에 실패했습니다. 원인: {reason}",
            "installed": installed_now,
        }

    def remove_mod(self, mod_id):
        """모드 제거 (필수 모드는 거부).

        tkinter _do_remove 이식.
          성공: {"ok": True, "status": "not_installed"}
          필수: {"ok": False, "error": "required", "message":...}
          실패: {"ok": False, "error": "remove_failed", "message":...}
        """
        if not self._registry:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "error": "not_ready",
                        "message": r.get("reason", "준비 실패")}

        mod = MOD_BY_ID.get(mod_id) or self._search_mod_by_id.get(mod_id, {})
        if mod.get("required"):
            return {"ok": False, "error": "required",
                    "message": f"{mod.get('name', mod_id)}은(는) 필수 모드입니다."}

        is_resourcepack = mod.get("kind", "mod") == "resourcepack"
        registry = self._resourcepack_registry if is_resourcepack else self._registry
        target_dir = self._resourcepacks_dir if is_resourcepack else self._mods_dir
        filename = registry.get_installed_filename(mod_id)
        if filename:
            path = os.path.join(target_dir, filename)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError as e:
                    return {"ok": False, "error": "remove_failed", "message": str(e)}

        registry.record_remove(mod_id)
        self._mod_statuses[mod_id] = "not_installed"
        preflight.write_log(f"[제거] {mod.get('name') or mod.get('title') or mod_id}")
        return {"ok": True, "status": "not_installed"}

    def remove_extra_mod(self, filename):
        """사용자가 직접 넣은 jar(카탈로그 밖) 제거.

        카탈로그가 관리하는 파일은 거부한다 — 그건 remove_mod 의 필수 모드 검사를 거쳐야 한다.
        """
        if not self._registry:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "message": r.get("reason", "준비 실패")}
        base = os.path.basename(filename or "")
        if not base or base != filename or not base.lower().endswith(".jar"):
            return {"ok": False, "message": "잘못된 파일명입니다."}
        if base in self._registry.filenames():
            return {"ok": False, "message": "런처가 관리하는 모드입니다. 모드 목록에서 제거해주세요."}
        path = os.path.join(self._mods_dir, base)
        if not os.path.isfile(path):
            return {"ok": True, "status": "not_found"}
        try:
            os.remove(path)
        except OSError as e:
            return {"ok": False, "message": str(e)}
        preflight.write_log(f"[제거] 직접 추가한 모드 {base}")
        return {"ok": True}

    def update_mod(self, mod_id):
        """모드 업데이트 (재설치 후 이전 파일 정리).

        tkinter _do_update 이식.
          성공: {"ok": True, "status": "installed"}
          실패: {"ok": False, "error": "update_failed", "message":...}
        """
        if not self._registry:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "error": "not_ready",
                        "message": r.get("reason", "준비 실패")}

        mod = MOD_BY_ID.get(mod_id) or self._search_mod_by_id.get(mod_id, {})
        is_resourcepack = mod.get("kind", "mod") == "resourcepack"
        registry = self._resourcepack_registry if is_resourcepack else self._registry
        target_dir = self._resourcepacks_dir if is_resourcepack else self._mods_dir
        old_file = registry.get_installed_filename(mod_id)
        ok, reason = self._install_single(mod_id)
        if ok:
            new_file = registry.get_installed_filename(mod_id)
            if old_file and old_file != new_file:
                old_path = os.path.join(target_dir, old_file)
                if os.path.isfile(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
            self._mod_statuses[mod_id] = "installed"
            return {"ok": True, "status": "installed"}

        return {
            "ok": False,
            "error": "update_failed",
            "message": f"{(MOD_BY_ID.get(mod_id) or self._search_mod_by_id.get(mod_id, {})).get('name') or (MOD_BY_ID.get(mod_id) or self._search_mod_by_id.get(mod_id, {})).get('title') or mod_id} "
                       f"업데이트에 실패했습니다. 원인: {reason}",
        }

    # ══════════════════════════════════════════════════════════════════════
    #  셰이더 (Iris) — 백엔드 6함수 래핑. 모드와 달리 하드코딩 카탈로그가 없고
    #  Modrinth 실시간 검색으로 목록을 만든다. 식별 키는 slug.
    # ══════════════════════════════════════════════════════════════════════

    def get_shaders(self, query=""):
        """Iris 셰이더팩 목록 + 설치/업데이트 상태.

        search_shaders(categories:iris facet 포함) 로 목록을 받고,
        scan_installed_shaders 로 로컬 설치본을 대조해 상태를 붙인다.

        반환:
          {"ok": True, "shaders": [ {slug, project_id, title, description,
              icon_url, downloads, categories, status, filename}, ... ],
              "categories": [고유 카테고리...] }
          {"ok": False, "reason": ...}
        """
        if not self._shaderpacks_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "reason": r.get("reason", "준비 실패")}

        try:
            res = modrinth_api.search_shaders(GAME_VERSION, limit=40, query=query or "")
        except Exception as e:
            return {"ok": False, "reason": f"셰이더 검색 실패: {e}"}
        if res.get("error"):
            return {"ok": False, "reason": f"셰이더 검색 실패: {res['error']}"}

        # 로컬 설치본 스캔 (project_id → {filename, sha1, version})
        try:
            installed = modrinth_api.scan_installed_shaders(self._shaderpacks_dir)
        except Exception:
            installed = {}

        self._shader_by_slug = {}
        out = []
        cats = set()
        for sh in res.get("shaders", []):
            pid = sh.get("project_id")
            slug = sh.get("slug")
            status = "not_installed"
            filename = None
            if pid and pid in installed:
                filename = installed[pid]["filename"]
                local_sha1 = installed[pid]["sha1"]
                # 업데이트 여부 (설치된 것만 추가 조회)
                try:
                    upd = modrinth_api.check_shader_update(slug, local_sha1, GAME_VERSION)
                    status = "update" if upd.get("update_available") else "installed"
                except Exception:
                    status = "installed"
            if slug:
                self._shader_by_slug[slug] = {"filename": filename, "project_id": pid}
            for c in (sh.get("categories") or []):
                cats.add(c)
            gal = sh.get("gallery") or []
            image = gal[0] if gal and isinstance(gal[0], str) else (
                (gal[0].get("url") if gal and isinstance(gal[0], dict) else None))
            out.append({
                "slug": slug,
                "project_id": pid,
                "title": sh.get("title"),
                "description": sh.get("description", ""),
                "icon_url": sh.get("icon_url"),
                "image": image,
                "downloads": sh.get("downloads", 0),
                "categories": sh.get("categories", []),
                "status": status,
                "filename": filename,
            })

        return {"ok": True, "shaders": out, "categories": sorted(cats)}

    def shader_ready(self):
        """셰이더 설치 가능 여부. 격리 인스턴스에선 shaderpacks 폴더를 우리가
        만들므로 Iris 모드 설치 여부만 확인한다.

        반환: {"ready":bool, "iris":bool, "folder":bool, "reason":str}
        """
        import instance
        mods = instance.mods_dir()
        shaders = instance.shaderpacks_dir()
        os.makedirs(shaders, exist_ok=True)  # 폴더는 항상 준비
        iris = preflight.is_iris_installed(mods) if os.path.isdir(mods) else False
        ready = iris
        reason = "" if ready else "셰이더팩을 쓰려면 먼저 Iris Shaders 모드를 설치하세요."
        return {"ready": ready, "iris": iris, "folder": True, "reason": reason}

    def install_shader(self, slug):
        """셰이더팩 설치. 반환: {"ok":True,"status":"installed","filename":...}
                              또는 {"ok":False,"error":...,"message":...}"""
        # Iris + shaderpacks 폴더 준비 확인 (없으면 설치 차단)
        sr = self.shader_ready()
        if not sr["ready"]:
            return {"ok": False, "error": "shader_not_ready", "message": sr["reason"]}
        if not self._shaderpacks_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "error": "not_ready",
                        "message": r.get("reason", "준비 실패")}
        try:
            res = modrinth_api.install_shader_by_slug(
                slug, self._shaderpacks_dir, GAME_VERSION,
                progress_cb=self._progress_cb_for(slug),
            )
        except Exception as e:
            preflight.write_log(f"[셰이더설치실패] {slug}: {e}")
            return {"ok": False, "error": "install_failed",
                    "message": f"셰이더 설치 중 오류: {e}"}

        if res.get("status") in ("installed", "up_to_date"):
            self._shader_by_slug.setdefault(slug, {})["filename"] = res.get("filename")
            self._shader_by_slug[slug]["project_id"] = res.get("project_id")
            preflight.write_log(
                f"[셰이더설치성공] {slug} v{res.get('version','?')} ({res['status']})"
            )
            return {"ok": True, "status": "installed", "filename": res.get("filename")}

        msg = res.get("message", f"알 수 없는 오류 (status={res.get('status')})")
        preflight.write_log(f"[셰이더설치실패] {slug}: {msg}")
        return {"ok": False, "error": "install_failed", "message": msg}

    def remove_shader(self, slug):
        """셰이더팩 제거 (파일명은 내부 맵에서 조회).
        반환: {"ok":True,"status":"not_installed"} 또는 {"ok":False,...}"""
        if not self._shaderpacks_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "error": "not_ready",
                        "message": r.get("reason", "준비 실패")}

        info = self._shader_by_slug.get(slug)
        filename = info.get("filename") if info else None
        if not filename:
            # 맵에 없으면 재스캔으로 파일명 복구 시도.
            # (런처 재시작 후 셰이더 탭을 열지 않으면 _shader_by_slug 가 비어 있다)
            try:
                installed = modrinth_api.scan_installed_shaders(self._shaderpacks_dir)
                pid = (info or {}).get("project_id")
                if not pid:
                    # slug → project_id 를 Modrinth 에서 직접 확인
                    try:
                        meta = modrinth_api.get_projects_batch([slug]) or {}
                        pid = meta.get(slug)
                    except Exception:
                        pid = None
                if pid and pid in installed:
                    filename = installed[pid]["filename"]
                    self._shader_by_slug.setdefault(slug, {})["filename"] = filename
                    self._shader_by_slug[slug]["project_id"] = pid
            except Exception:
                pass
        if not filename:
            # 파일이 실제로 없는 상태 → '제거됨' 으로 처리해야 UI 가 정상화된다.
            # (사용자가 폴더에서 직접 지운 경우 등)
            self._shader_by_slug.setdefault(slug, {})["filename"] = None
            return {"ok": True, "status": "not_installed",
                    "message": "이미 제거되어 있습니다."}

        res = modrinth_api.remove_shader_file(self._shaderpacks_dir, filename)
        if res.get("status") == "removed":
            if slug in self._shader_by_slug:
                self._shader_by_slug[slug]["filename"] = None
            preflight.write_log(f"[셰이더제거] {slug} ({filename})")
            return {"ok": True, "status": "not_installed"}
        if res.get("status") == "not_found":
            return {"ok": True, "status": "not_installed"}  # 이미 없음 → 성공 취급
        return {"ok": False, "error": "remove_failed",
                "message": res.get("message", "제거 실패")}

    def shader_statuses(self):
        """shaderpacks 폴더만 다시 스캔해 설치 상태를 갱신 (네트워크 없음).

        셰이더 목록(get_shaders)은 Modrinth 검색까지 해서 느리므로,
        탭에 다시 들어올 때마다 부르기엔 무겁다.
        여기서는 로컬 파일만 확인한다.
        → 사용자가 탐색기에서 직접 지운 셰이더가 계속 '설치됨' 으로 남던 문제 해결.

        반환: {"ok":True, "statuses": {slug: "installed"|"not_installed"}}
        """
        if not self._shaderpacks_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "reason": r.get("reason", "준비 실패")}
        try:
            installed = modrinth_api.scan_installed_shaders(self._shaderpacks_dir)
        except Exception as e:
            return {"ok": False, "reason": str(e)}

        statuses = {}
        for slug, info in list(self._shader_by_slug.items()):
            pid = info.get("project_id")
            if pid and pid in installed:
                info["filename"] = installed[pid]["filename"]
                statuses[slug] = "installed"
            else:
                info["filename"] = None
                statuses[slug] = "not_installed"
        return {"ok": True, "statuses": statuses}

    def update_shader(self, slug):
        """셰이더팩 업데이트 = 재설치(최신). 이전 파일과 다르면 정리."""
        if not self._shaderpacks_dir:
            r = self.ensure_ready()
            if not r.get("ready"):
                return {"ok": False, "error": "not_ready",
                        "message": r.get("reason", "준비 실패")}

        old = self._shader_by_slug.get(slug, {})
        old_file = old.get("filename")
        res = self.install_shader(slug)
        if res.get("ok"):
            new_file = res.get("filename")
            if old_file and new_file and old_file != new_file:
                try:
                    modrinth_api.remove_shader_file(self._shaderpacks_dir, old_file)
                except Exception:
                    pass
            return {"ok": True, "status": "installed"}
        return {"ok": False, "error": "update_failed",
                "message": res.get("message", "업데이트 실패")}

    # ══════════════════════════════════════════════════════════════════════
    #  Modrinth 미디어/상세 — 카드 사진·아이콘(#8), 모달 제작자·버전(#9)
    # ══════════════════════════════════════════════════════════════════════

    def get_mod_media(self):
        """모든 모드의 아이콘/대표사진/다운로드수 + 제작자를 미리 로드.

        - 아이콘/사진/다운로드수: 1회 배치 호출(get_projects_media_batch)
        - 제작자: 프로젝트별 /members 필요 → 스레드풀로 병렬 조회(지연 최소화)

        반환: {"ok":True,"media":{ mod_id: {icon_url,image,downloads,author} }}
        """
        slug_to_id = {}
        for m in VISIBLE_MODS:
            slug = m.get("slug")
            if slug:
                slug_to_id[slug] = m["id"]
        if not slug_to_id:
            return {"ok": True, "media": {}}

        # 1) 아이콘/사진/다운로드수 (배치 1회)
        try:
            data = modrinth_api.get_projects_media_batch(list(slug_to_id.keys()))
        except Exception:
            data = {}

        # 2) 제작자 병렬 조회 (slug → author)
        authors = {}
        slugs = list(slug_to_id.keys())
        def _one(slug):
            try:
                return slug, modrinth_api.get_project_author(slug)
            except Exception:
                return slug, None
        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for slug, author in ex.map(_one, slugs):
                    if author and author != "알 수 없음":
                        authors[slug] = author
        except Exception:
            pass

        media = {}
        for slug, mid in slug_to_id.items():
            info = data.get(slug, {})
            media[mid] = {
                "icon_url": info.get("icon_url"),
                "image": info.get("image"),
                "downloads": info.get("downloads", 0),
                "author": authors.get(slug),
            }
        return {"ok": True, "media": media}

    def get_project_detail(self, kind, item_id):
        """모달용 상세: 제작자 + 최신 버전 (+ icon/image 보강).

        kind: "mod" | "shader"
        item_id: 모드면 mod_id, 셰이더면 slug.
        반환: {"ok":True,"author","version","icon_url","image","downloads"}
              실패해도 가능한 필드만 채워 ok=True (모달이 안 깨지게).
        """
        # slug 결정
        if kind == "shader":
            slug = item_id
        else:
            mod = MOD_BY_ID.get(item_id) or self._search_mod_by_id.get(item_id, {})
            slug = mod.get("slug")
            if not slug:
                # 번들 모드 등 slug 없음 → 작성자/버전 없음
                return {"ok": True, "author": None, "version": None,
                        "icon_url": None, "image": None, "downloads": None}

        author = None
        version = None
        icon_url = None
        image = None
        downloads = None
        try:
            author = modrinth_api.get_project_author(slug)
        except Exception:
            author = None
        try:
            latest = modrinth_api.get_latest_compatible_version(slug, GAME_VERSION, LOADER)
            if latest:
                version = latest.get("version_number")
        except Exception:
            version = None
        try:
            info = modrinth_api.get_project_info(slug)
            if info:
                icon_url = info.get("icon_url")
                downloads = info.get("downloads")
                gallery = info.get("gallery") or []
                if gallery:
                    feat = [g for g in gallery if g.get("featured")]
                    pick = feat[0] if feat else gallery[0]
                    image = pick.get("url") if isinstance(pick, dict) else None
        except Exception:
            pass

        return {
            "ok": True,
            "author": author,
            "version": version,
            "icon_url": icon_url,
            "image": image,
            "downloads": downloads,
        }

    # ══════════════════════════════════════════════════════════════════════
    #  사전 점검 (Preflight) — Java(21+)/Fabric 감지·설치
    #  스마트 UX: quick_check 로 조용히 확인 → 문제 있을 때만 화면,
    #  run_preflight_async 로 실제 설치 진행(백그라운드 + 진행상황 push).
    # ══════════════════════════════════════════════════════════════════════

    def quick_check(self):
        """설치/다운로드 없이 현재 상태만 조용히 확인 (빠름).

        반환:
          {"ok": True,  "mc":..., "java":..., "fabric":...}      # 전부 정상
          {"ok": False, "problems": ["java"|"fabric"|"mc"], ...} # 문제 목록
        각 항목: {"present": bool, "version": str|None}
        """
        mc_dir = preflight.find_minecraft_dir(self._config)
        problems = []

        mc_present = bool(mc_dir)
        if not mc_present:
            problems.append("mc")

        valid_java = None
        try:
            valid_java = preflight.find_valid_java()
        except Exception:
            valid_java = None
        java_present = valid_java is not None
        if not java_present:
            problems.append("java")

        fabric_present = False
        fabric_ver = None
        if mc_present:
            try:
                fabric_present = preflight.is_fabric_installed(mc_dir)
                if fabric_present:
                    fabric_ver = preflight.get_fabric_version(mc_dir)
            except Exception:
                fabric_present = False
        # v3: Fabric 은 '게임 설치'(game_installer.install → mll)가 격리 인스턴스에
        # 함께 넣는다. v2.1 의 fabric-installer.exe 는 .minecraft 에 설치하므로
        # 절대 사용하지 않는다. 따라서 Fabric 미설치는 '사전 점검 문제'가 아니다.
        # (게임 미설치 상태일 뿐이며, 플레이 탭의 '게임 설치'로 해결된다)

        return {
            "ok": len(problems) == 0,
            "problems": problems,
            "mc": {"present": mc_present, "path": mc_dir},
            "java": {
                "present": java_present,
                "version": (valid_java or {}).get("version"),
                "required_major": preflight.REQUIRED_JAVA_MAJOR,
            },
            "fabric": {"present": fabric_present, "version": fabric_ver},
        }

    def install_java_async(self, to_latest=False):
        """Mojang 공식 Java 런타임을 인스턴스 안에 설치한다(백그라운드 스레드).

        v3: Adoptium MSI + 설치 마법사(UAC) 경로를 폐기하고,
        mll 의 install_jvm_runtime 을 쓴다.
          - 버전 JSON 의 javaVersion 이 지정한 정확한 JVM (Mojang 이 테스트한 것)
          - sha1 검증 + 병렬 다운로드
          - 인스턴스 안에 격리 설치 → 관리자 권한 불필요, 시스템 Java 영향 없음
        to_latest 인자는 하위호환용으로 받지만 무시한다.
        ('최신 LTS 로 교체'는 Mixin/ASM 을 깨뜨릴 수 있어 더 이상 지원하지 않음)

        진행 상황은 window.onJavaInstall(phase, payload) 로 push:
          phase="progress" payload={pct, msg}
          phase="done"     payload={status}
          phase="error"    payload={message}
        """
        if getattr(self, "_java_installing", False):
            return {"started": False, "message": "이미 설치가 진행 중입니다."}

        def _push(phase, payload=None):
            w = self._window
            if not w:
                return
            try:
                import json as _json
                w.evaluate_js(
                    "window.onJavaInstall && window.onJavaInstall(%s, %s)"
                    % (_json.dumps(str(phase)), _json.dumps(payload or {}))
                )
            except Exception:
                pass

        def _worker():
            try:
                import game_installer
                ri = game_installer.runtime_info()
                if not ri.get("ok"):
                    # 게임 미설치 등 → 게임 설치 시 함께 내려받힌다
                    _push("error", {"message": "먼저 게임을 설치해주세요."})
                    return
                game_installer.ensure_runtime(
                    on_progress=lambda pct, msg, detail=None: _push(
                        "progress", {"pct": pct, "msg": str(msg), "detail": detail or {}}))
                preflight.invalidate_java_cache()
                _push("done", preflight.check_java_status())
            except game_installer.InstallCancelled:
                _push("cancelled", {"message": "Java 설치를 취소했습니다."})
            except Exception as e:
                msg = getattr(e, "message", None) or str(e)
                _push("error", {"message": msg})
            finally:
                self._java_installing = False

        self._java_installing = True
        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    def run_preflight_async(self):
        """사전 점검을 백그라운드 스레드에서 실행하고, 단계별 상태를
        window.onPreflight(step, message) 로 JS 에 push 한다.

        run_preflight 는 블로킹(설치 마법사 대기 등)이라 노출 메서드가
        pywebview 워커 스레드에서 실행되더라도, 여기서 별도 스레드로 돌려
        브릿지 응답이 곧바로 반환되게 한다(UI 프리즈 방지).

        JS 쪽 계약:
          window.onPreflight(step, message)  # 각 단계마다
          최종적으로 step 이 "done" 또는 "error"/"mc_not_found" 로 종료.
        """
        def _push(step, message):
            w = self._window
            if not w:
                return
            try:
                import json as _json
                w.evaluate_js(
                    "window.onPreflight && window.onPreflight(%s, %s)"
                    % (_json.dumps(str(step)), _json.dumps(str(message)))
                )
            except Exception:
                pass

        def _worker():
            try:
                result = preflight.run_preflight(_push, self._config)
                if result:
                    # 성공: 경로/레지스트리 즉시 준비해 이후 모드작업이 바로 되게
                    self._minecraft_dir = result.get("minecraft_dir")
                    self._mods_dir = result.get("mods_dir")
                    if self._mods_dir:
                        self._shaderpacks_dir = preflight.get_shaderpacks_dir(
                            self._minecraft_dir)
                        self._registry = modrinth_api.ModRegistry(self._mods_dir)
            except Exception as e:
                _push("error", f"사전 점검 중 오류가 발생했습니다:\n{e}")

        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    # ── 동글랜드 서버 상태 ────────────────────────────────────────────────
    def get_server_status(self):
        """동글랜드 서버 상태를 조회(30초 캐시).

        반환: {"online":bool, "players_online":int, "players_max":int,
               "motd":str, "version":str} 또는 {"online":False}
        """
        import time as _time
        if not app_meta.SERVER_HOST:
            return {"online": False, "configured": False}
        now = _time.time()
        cache = getattr(self, "_server_cache", None)
        if cache and (now - cache["t"] < 30):
            return cache["v"]
        try:
            v = server_status.ping(app_meta.SERVER_HOST, app_meta.SERVER_PORT)
        except Exception:
            v = {"online": False}
        self._server_cache = {"t": now, "v": v}
        return v

    # ═══════════════════════════════════════════════════════════════════
    # v3 클라이언트 브릿지 — Microsoft 로그인 / 게임 설치 / 직접 실행
    # (HANDOFF_v3_CLIENT.md Phase 1~3. 신규 모듈: auth/instance/
    #  game_installer/launcher — 임포트 실패해도 v2.1 기능은 영향 없도록
    #  각 메서드 안에서 지연 임포트한다.)
    # ═══════════════════════════════════════════════════════════════════

    # ── 로그인 (Phase 1) ─────────────────────────────────────────────────
    def auth_status(self):
        """저장된 로그인 상태. {"logged_in":bool, username?, uuid?, avatar_url?}"""
        try:
            import auth
            return auth.get_status()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}

    def auth_begin(self):
        """로그인 시작 — 기본: 앱 내부 로그인 창(임베디드 웹뷰).
        창을 못 띄우면 외부 브라우저로 폴백한다.
        반환: {"ok":True, "flow":"authcode", "auth_url":..., "embedded":bool}
        UI 는 이후 auth_poll 로 완료를 확인한다.
        """
        import auth
        try:
            r = auth.begin_auth_code_login()
            auth_url = r["auth_url"]
            embedded = auth.open_login_window(auth_url)   # 앱 안에 로그인 창
            if not embedded:
                try:
                    import webbrowser
                    webbrowser.open(auth_url)
                except Exception:
                    pass
            return {"ok": True, "flow": "authcode",
                    "auth_url": auth_url, "embedded": embedded}
        except Exception as e:
            msg = getattr(e, "message", None) or f"로그인 시작에 실패했습니다:\n{e}"
            return {"ok": False, "code": getattr(e, "code", "auth_error"),
                    "message": msg}

    def auth_begin_devicecode(self):
        """대체 로그인 — 인증 코드 방식(device code).
        임베디드 창이 안 될 때나 다른 기기로 로그인하고 싶을 때 사용.
        반환: {"ok":True, "flow":"devicecode", user_code, verification_uri,
               device_code, interval, expires_in}
        """
        try:
            import auth
            r = auth.begin_device_login()
            r["flow"] = "devicecode"
            return r
        except Exception as e:
            msg = getattr(e, "message", None) or f"코드 발급에 실패했습니다:\n{e}"
            return {"ok": False, "code": getattr(e, "code", "auth_error"),
                    "message": msg}

    def auth_poll(self, device_code=None):
        """폴링 1회. device_code 가 주어지면 device code flow, 아니면 authcode.
          {"status":"pending"}                 → 잠시 후 재호출
          {"status":"slow_down","interval_add"}→ 간격 늘려 재호출 (devicecode)
          {"status":"ok","account":{...}}      → 로그인 완료
          {"status":"error","code","message"}  → 중단"""
        try:
            import auth
            if device_code:
                return auth.poll_device_login(device_code)
            return auth.poll_auth_code()
        except Exception as e:
            msg = getattr(e, "message", None) or f"로그인에 실패했습니다:\n{e}"
            return {"status": "error",
                    "code": getattr(e, "code", "auth_error"), "message": msg}

    def auth_cancel(self):
        """로그인 취소 — 열려 있는 앱 내부 로그인 창을 닫는다."""
        try:
            import auth
            auth.close_login_window()
        except Exception:
            pass
        return {"ok": True}

    def auth_open_verification(self, url):
        """로그인 페이지를 다시 연다. 앱 내부 창 우선, 실패 시 외부 브라우저.
        (MS 로그인 도메인만 허용)"""
        allowed = ("https://microsoft.com/link",
                   "https://www.microsoft.com/link",
                   "https://login.microsoftonline.com/",
                   "https://login.live.com/")
        if not isinstance(url, str) or not url.startswith(allowed):
            return {"ok": False, "message": "허용되지 않은 주소입니다."}
        try:
            import auth
            if auth.open_login_window(url):
                return {"ok": True, "embedded": True}
        except Exception:
            pass
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True, "embedded": False}
        except Exception as e:
            return {"ok": False, "message": f"브라우저를 열 수 없습니다: {e}"}

    def auth_logout(self):
        """활성 계정만 로그아웃(다른 계정 유지). {"ok","active"}"""
        try:
            import auth
            return auth.logout()
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def auth_accounts(self):
        """저장된 모든 계정 목록. [{username,uuid,avatar_url,active}]"""
        try:
            import auth
            return {"ok": True, "accounts": auth.list_accounts()}
        except Exception as e:
            return {"ok": False, "accounts": [], "message": str(e)}

    def auth_switch(self, uuid):
        """저장된 계정으로 활성 전환 (재로그인 없이)."""
        try:
            import auth
            return auth.switch_account(uuid)
        except Exception as e:
            return {"ok": False, "code": getattr(e, "code", "auth_error"),
                    "message": getattr(e, "message", None) or str(e)}

    def auth_remove(self, uuid):
        """계정 목록에서 제거. {"ok","active"}"""
        try:
            import auth
            return auth.remove_account(uuid)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # ── 게임 설치 (Phase 2) ──────────────────────────────────────────────
    def client_status(self):
        """설치 상태. {"installed":bool, version_id?, mc_version?,
                       loader_version?, installing:bool}"""
        try:
            import game_installer
            import instance
            st = instance.load_state()
            return {
                "installed": game_installer.is_installed(),
                "version_id": st.get("installed_version_id"),
                "mc_version": st.get("mc_version"),
                "loader_version": st.get("fabric_loader_version"),
                "installing": bool(getattr(self, "_client_installing", False)),
            }
        except Exception as e:
            return {"installed": False, "installing": False, "error": str(e)}

    def client_install_async(self):
        """게임(바닐라+Fabric) 설치를 백그라운드로 시작.
        진행률은 window.onClientInstall(pct, msg) 로 push 하고,
        완료/실패는 pct=100/-1 로 신호한다.
        반환: {"started":True} 또는 {"started":False,"message"}"""
        if getattr(self, "_client_installing", False):
            return {"started": False, "message": "이미 설치가 진행 중입니다."}

        def _push(pct, msg, detail=None):
            w = self._window
            if not w:
                return
            try:
                import json as _json
                w.evaluate_js(
                    "window.onClientInstall && window.onClientInstall(%d, %s, %s)"
                    % (int(pct), _json.dumps(str(msg)), _json.dumps(detail or {})))
            except Exception:
                pass

        def _worker():
            try:
                import game_installer
                # install() 이 검증 통과 후에만 100% 를 내보낸다 (중복 push 금지)
                game_installer.install(on_progress=_push)
                # 숨겨진 필수 Fabric 구성요소는 게임 설치에 포함한다.
                # 기린월드 코어는 launcher.py에서 Java Agent로 별도 연결한다.
                self._ensure_hidden_required()
            except Exception as e:
                import game_installer as _gi
                if isinstance(e, _gi.InstallCancelled):
                    _push(-2, "설치를 취소했습니다")
                else:
                    msg = getattr(e, "message", None) or str(e)
                    _push(-1, msg)
            finally:
                self._client_installing = False

        self._client_installing = True
        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    # ── 직접 실행 + 서버 자동 접속 (Phase 3) ─────────────────────────────
    def update_fabric_async(self):
        """Fabric 로더를 최신 stable 로 갱신 (백그라운드).

        진행률은 window.onFabricUpdate(pct, msg) 로 push.
        완료 100 / 실패 -1. 반환: {"started":bool, "message"?}
        """
        if getattr(self, "_fabric_updating", False):
            return {"started": False, "message": "이미 업데이트가 진행 중입니다."}
        if getattr(self, "_client_installing", False):
            return {"started": False, "message": "게임 설치가 진행 중입니다."}

        def _push(pct, msg, detail=None):
            w = self._window
            if not w:
                return
            try:
                import json as _json
                w.evaluate_js(
                    "window.onFabricUpdate && window.onFabricUpdate(%d, %s, %s)"
                    % (int(pct), _json.dumps(str(msg)), _json.dumps(detail or {})))
            except Exception:
                pass

        def _worker():
            try:
                import game_installer
                r = game_installer.update_loader(on_progress=_push)
                if r.get("unchanged"):
                    _push(100, "이미 최신 로더입니다", {"stage":"완료","stage_no":4,"stage_total":4,"cur":0,"max":0,"raw":"done"})
            except game_installer.InstallCancelled:
                # -2 = 취소 (게임 설치 onClientInstall 과 같은 약속). 상태 전환 전에 멈추므로 옛 로더 그대로.
                _push(-2, "Fabric 업데이트를 취소했습니다")
            except Exception as e:
                msg = getattr(e, "message", None) or str(e)
                _push(-1, msg)
            finally:
                self._fabric_updating = False

        self._fabric_updating = True
        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    def game_running(self):
        """게임 프로세스가 아직 실행 중인지. 프론트가 폴링해 버튼 상태 갱신."""
        try:
            import launcher
            return {"running": launcher.is_game_running()}
        except Exception:
            return {"running": False}

    # ── 런처 동작 설정 (Windows 시작 프로그램) ────────────────────────────
    def get_app_settings(self):
        """{"ok":True, run_at_startup, startup_supported, startup_blocked}"""
        try:
            import autostart
            st = autostart.status()
            return {"ok": True,
                    "run_at_startup": st["enabled"],
                    "startup_supported": st["supported"],
                    "startup_blocked": st["blocked"]}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def get_theme(self):
        """파일에 저장된 테마를 반환한다. onefile 임시 경로의 localStorage에 의존하지 않는다."""
        theme = (self._config or {}).get("theme", "system")
        if theme not in ("system", "dark", "light"):
            theme = "system"
        return {"ok": True, "theme": theme}

    def set_theme(self, theme):
        """사용자가 고른 테마를 런처 설정 파일에 영구 저장한다."""
        if theme not in ("system", "dark", "light"):
            return {"ok": False, "message": "올바르지 않은 테마입니다."}
        try:
            self._config["theme"] = theme
            preflight.save_config(self._config)
            return {"ok": True, "theme": theme}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def set_run_at_startup(self, on):
        """레지스트리(HKCU Run)가 진실이라 config 에 따로 저장하지 않는다."""
        try:
            import autostart
            return autostart.set_enabled(bool(on))
        except Exception as e:
            return {"ok": False, "enabled": False, "message": str(e)}

    def memory_info(self):
        """Java 할당 메모리 설정 + 시스템 총 RAM.

        반환: {"ok":True, "alloc_mb":int, "total_mb":int,
               "min_mb":int, "max_mb":int, "recommended_mb":int}
        """
        try:
            total_mb = self._system_total_mb()
            cfg = self._config or preflight.load_config()
            alloc = int(cfg.get("max_mem_mb") or 0)

            # 슬라이더 범위: 2GB ~ (총 RAM의 3/4, 최대 16GB). 총 RAM 모르면 8GB 상한.
            min_mb = 2048
            if total_mb:
                max_mb = min(int(total_mb * 0.75), 16384)
                max_mb = max(max_mb, min_mb)
            else:
                max_mb = 8192
            # 권장값: 총 RAM 기준 (8GB 미만 → 2GB, 16GB 미만 → 4GB, 그 이상 → 6GB)
            if not total_mb:
                rec = 4096
            elif total_mb < 8192:
                rec = 2048
            elif total_mb < 16384:
                rec = 4096
            else:
                rec = 6144
            rec = min(max(rec, min_mb), max_mb)

            if not alloc:
                alloc = rec
            alloc = min(max(alloc, min_mb), max_mb)
            return {"ok": True, "alloc_mb": alloc, "total_mb": total_mb or 0,
                    "min_mb": min_mb, "max_mb": max_mb, "recommended_mb": rec}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def _system_total_mb(self) -> int:
        """시스템 총 물리 메모리(MB). 알 수 없으면 0."""
        try:
            if sys.platform == "win32":
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong),
                                ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong),
                                ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong),
                                ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong),
                                ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                return int(stat.ullTotalPhys // (1024 * 1024))
            # 리눅스/맥 (개발 환경)
            pages = os.sysconf("SC_PHYS_PAGES")
            size = os.sysconf("SC_PAGE_SIZE")
            return int(pages * size // (1024 * 1024))
        except Exception:
            return 0

    def set_memory(self, alloc_mb):
        """Java 할당 메모리 저장. 실행 시 -Xmx 로 적용된다."""
        try:
            info = self.memory_info()
            if not info.get("ok"):
                return {"ok": False, "message": "메모리 정보를 읽을 수 없습니다."}
            v = int(alloc_mb)
            v = min(max(v, info["min_mb"]), info["max_mb"])
            self._config = self._config or preflight.load_config()
            self._config["max_mem_mb"] = v
            preflight.save_config(self._config)
            return {"ok": True, "alloc_mb": v}
        except Exception as e:
            return {"ok": False, "message": f"저장에 실패했습니다: {e}"}

    _WINDOW_MODES = ("keep", "windowed", "fullscreen")

    def window_info(self):
        """게임 창 설정. mode: keep(게임 설정 그대로) / windowed / fullscreen.

        반환: {"ok":True, "mode":str, "width":int, "height":int}
        """
        try:
            cfg = self._config or preflight.load_config()
            w = cfg.get("window") or {}
            mode = w.get("mode") if w.get("mode") in self._WINDOW_MODES else "keep"
            return {"ok": True, "mode": mode,
                    "width": int(w.get("width") or 1280), "height": int(w.get("height") or 720)}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def set_window(self, mode, width, height):
        """게임 창 설정 저장. 다음 실행부터 적용된다."""
        try:
            if mode not in self._WINDOW_MODES:
                return {"ok": False, "message": "알 수 없는 창 모드입니다."}
            w = min(max(int(width), 640), 7680)
            h = min(max(int(height), 480), 4320)
            self._config = self._config or preflight.load_config()
            self._config["window"] = {"mode": mode, "width": w, "height": h}
            preflight.save_config(self._config)
            return {"ok": True, "mode": mode, "width": w, "height": h}
        except (TypeError, ValueError):
            return {"ok": False, "message": "창 크기는 숫자로 입력해주세요."}
        except Exception as e:
            return {"ok": False, "message": f"저장에 실패했습니다: {e}"}

    def cancel_install(self):
        """진행 중인 게임 설치 / 검증 / 로더 업데이트를 취소.

        mll 은 취소 API 가 없으므로, 진행률 콜백 안에서 예외를 던져 중단시킨다.
        이미 받은 파일은 남지만, 다음 설치/검증 때 그대로 재사용되고
        빠진 파일만 다시 받으므로 손해가 없다.
        """
        try:
            import game_installer
            busy = (getattr(self, "_client_installing", False)
                    or getattr(self, "_fabric_updating", False)
                    or getattr(self, "_java_installing", False))
            if not busy:
                return {"ok": False, "message": "진행 중인 작업이 없습니다."}
            game_installer.request_cancel()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def client_launch(self, quick_connect=True):
        """v3 게임 실행: 파일 검증·복구 → 로그인 계정(자동 갱신) → JVM 직접 실행 →
        동글랜드 서버 자동 입장. 필수 모드 게이팅은 v2.1 것 그대로 적용.

        ⚠️ mll 권고대로 '실행 전 매번' install_minecraft_version 으로 검증·복구한다.
        빠졌거나 손상된 파일만 다시 받으므로, 정상 설치 상태면 수 초 안에 끝난다.

        반환: {"ok":True, pid, username} 또는
              {"ok":False, "error":code, "message"}
          error 코드: required_missing / relogin / not_installed /
                      no_java / verify_failed / launch_failed ...
        """
        import time as _time
        gate = self.can_launch()
        if not gate["ok"]:
            return {"ok": False, "error": "required_missing",
                    "missing": gate["missing"], "message": gate["message"]}

        # ── 실행 전 무결성 검증 + 복구 ──────────────────────────────
        def _prep(pct, msg, detail=None):
            w = self._window
            if not w:
                return
            try:
                import json as _json
                w.evaluate_js(
                    "window.onLaunchPrep && window.onLaunchPrep(%d, %s, %s)"
                    % (int(pct), _json.dumps(str(msg)), _json.dumps(detail or {})))
            except Exception:
                pass

        try:
            import game_installer
            _prep(0, "게임 파일 검증 중")
            vr = game_installer.verify_and_repair(on_progress=_prep)
            if vr.get("repaired"):
                _prep(100, f"손상된 파일 {vr.get('files', 0)}개를 복구했습니다", {"stage":"완료","stage_no":4,"stage_total":4,"cur":0,"max":0,"raw":"done"})
            # Mojang 공식 런타임 확보 (보통 위 검증에서 이미 설치된다)
            ri = game_installer.runtime_info()
            if ri.get("ok") and not ri.get("installed"):
                _prep(0, "Java 런타임 준비 중")
                game_installer.ensure_runtime(on_progress=_prep)
        except Exception as e:
            msg = getattr(e, "message", None) or str(e)
            _prep(-1, msg)
            return {"ok": False, "error": "verify_failed", "message": msg}

        try:
            import auth
            import launcher
            self._sync_mods_to_instance()
            # 설정된 할당 메모리를 -Xmx 로 적용 (미설정이면 권장값)
            mem = self.memory_info()
            max_mem = mem.get("alloc_mb") if mem.get("ok") else None
            win = self.window_info()
            win = win if win.get("ok") else None
            if max_mem:
                result = launcher.launch(quick_connect=bool(quick_connect),
                                         max_mem_mb=int(max_mem), window=win)
            else:
                result = launcher.launch(quick_connect=bool(quick_connect), window=win)
        except Exception as e:
            msg = getattr(e, "message", None) or f"게임 실행에 실패했습니다:\n{e}"
            return {"ok": False, "error": getattr(e, "code", "launch_failed"),
                    "message": msg}
        # 플레이 시간 세션 시작 (v2.1 launch_game 과 동일한 규약)
        self._flush_playtime()
        self._config["session_start"] = int(_time.time())
        preflight.save_config(self._config)
        return result

    def _sync_mods_to_instance(self):
        """[Phase 4 전환 전 임시] v2.1 mods 폴더의 jar 를 격리 인스턴스
        mods 로 미러링(추가/갱신/삭제). Phase 4 에서 modrinth_api 설치
        경로 자체가 인스턴스로 바뀌면 이 함수는 제거한다."""
        try:
            import instance
            src = self._mods_dir
            if not src or not os.path.isdir(src):
                return
            dst = instance.mods_dir()
            os.makedirs(dst, exist_ok=True)
            src_jars = {f for f in os.listdir(src) if f.endswith(".jar")}
            dst_jars = {f for f in os.listdir(dst) if f.endswith(".jar")}
            for name in src_jars:
                s, d = os.path.join(src, name), os.path.join(dst, name)
                if (name not in dst_jars
                        or os.path.getsize(s) != os.path.getsize(d)):
                    shutil.copy2(s, d)
            for name in dst_jars - src_jars:  # 소스에서 지운 모드는 제거
                try:
                    os.remove(os.path.join(dst, name))
                except OSError:
                    pass
        except Exception:
            pass  # 동기화 실패가 실행 자체를 막지 않도록

    def open_instance_folder(self):
        """v3 전용 인스턴스 폴더를 탐색기로 연다."""
        try:
            import instance
            p = instance.ensure_dirs()
            os.startfile(p)  # Windows 전용
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": f"폴더를 열 수 없습니다: {e}"}

    def client_reinstall_async(self):
        """게임 재설치: 설치 완료 마커만 지우고 설치를 다시 돌린다.
        mods/월드/옵션은 건드리지 않음 — 버전·라이브러리·에셋은
        무결성 검증 기반이라 손상 파일만 실제로 다시 받는다."""
        if getattr(self, "_client_installing", False):
            return {"started": False, "message": "이미 설치가 진행 중입니다."}
        try:
            import instance
            st = instance.load_state()
            st.pop("installed_version_id", None)
            instance.save_state(st)
        except Exception as e:
            return {"started": False, "message": f"재설치 준비 실패: {e}"}
        return self.client_install_async()

    # ── 스킨 관리 (v3 스킨 탭) ───────────────────────────────────────────
    def skin_get(self):
        """현재 스킨. {"ok":True,"variant","url","default"} / 실패 {"ok":False,...}"""
        try:
            import skin
            return skin.get_skin()
        except Exception as e:
            return {"ok": False, "code": getattr(e, "code", "skin_error"),
                    "message": getattr(e, "message", None) or str(e)}

    def skin_pick_file(self):
        """PNG 파일 선택(네이티브 대화상자) + 규격 검증 + 미리보기 data_url.
        모달의 '스킨 파일 선택' 버튼이 호출. 실제 저장은 skin_lib_add/update."""
        try:
            import base64
            import webview

            import skin
            paths = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("PNG 스킨 (*.png)",))
            if not paths:
                return {"ok": False, "code": "cancelled"}
            path = paths[0]
            skin.validate_png(path)
            with open(path, "rb") as f:
                data_url = "data:image/png;base64," + base64.b64encode(f.read()).decode()
            return {"ok": True, "path": path,
                    "filename": os.path.basename(path), "data_url": data_url}
        except Exception as e:
            return {"ok": False, "code": getattr(e, "code", "skin_error"),
                    "message": getattr(e, "message", None) or str(e)}

    def _skin_call(self, fn, *args):
        try:
            import skin
            return getattr(skin, fn)(*args)
        except Exception as e:
            return {"ok": False, "code": getattr(e, "code", "skin_error"),
                    "message": getattr(e, "message", None) or str(e)}

    def skin_lib_list(self):
        return self._skin_call("lib_list")

    def skin_lib_add(self, name, variant, cape_id, cape_name, path):
        return self._skin_call("lib_add", name, variant, cape_id, cape_name, path)

    def skin_lib_update(self, sid, name, variant, cape_id, cape_name, path=None):
        return self._skin_call("lib_update", sid, name, variant, cape_id, cape_name, path)

    def skin_lib_duplicate(self, sid):
        return self._skin_call("lib_duplicate", sid)

    def skin_lib_delete(self, sid):
        return self._skin_call("lib_delete", sid)

    def skin_lib_use(self, sid):
        return self._skin_call("lib_use", sid)

    def skin_capes(self):
        return self._skin_call("get_capes")

    def skin_reset(self):
        """기본 스킨(스티브/알렉스)으로 초기화."""
        try:
            import skin
            return skin.reset_skin()
        except Exception as e:
            return {"ok": False, "code": getattr(e, "code", "skin_error"),
                    "message": getattr(e, "message", None) or str(e)}

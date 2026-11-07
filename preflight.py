# -*- coding: utf-8 -*-
"""
preflight.py — 앱 실행 전 선행 조건 확인 및 설치 모듈

담당 역할
---------
1. 설정 파일 (config.json) 읽기/쓰기
2. 마인크래프트 설치 경로 탐색
3. Java 설치 여부 확인 + 미설치 시 Adoptium JRE 25 자동 다운로드/실행
4. Fabric 26.2 설치 여부 확인 + 미설치 시 fabric-installer 자동 다운로드/실행

GUI(api.py 브릿지)는 이 모듈의 함수를 백그라운드 스레드에서 호출한다.
"""

import os
import sys
import json
import shutil
import hashlib
import tempfile
import subprocess
import ssl
import urllib.request
import urllib.error
import urllib.parse

# ── 상수 ────────────────────────────────────────────────────────────────────

# Windows에서 콘솔창 깜빡임 방지 (감지/설치 호출에만 사용, 런처 실행엔 미적용)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

GAME_VERSION = "26.2"
APP_NAME     = "YoonseulLauncher"   # 데이터 폴더명 (instance.APP_DIR_NAME 과 동일)
USER_AGENT   = "Yoonseul_Launcher/2.6.8"


# ── SSL 컨텍스트 (PyInstaller exe 의 인증서 검증 실패 방지) ─────────────────────
#
# PyInstaller --onefile exe 는 시스템 루트 인증서를 못 찾아
# [SSL: CERTIFICATE_VERIFY_FAILED] 가 발생할 수 있다 (특히 Adoptium/GitHub).
# certifi 가 있으면 그 인증서 묶음을 사용하고, 없으면 시스템 기본으로 폴백.
def _make_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

_SSL_CONTEXT = _make_ssl_context()

FABRIC_INSTALLER_URL  = (
    "https://maven.fabricmc.net/net/fabricmc/fabric-installer/"
    "1.1.1/fabric-installer-1.1.1.exe"
)
FABRIC_INSTALLER_HOST = "maven.fabricmc.net"

# Java 설치 여부 확인 시 탐색할 일반 경로
JAVA_COMMON_PATHS = [
    r"C:\Program Files\Java",
    r"C:\Program Files\Eclipse Adoptium",
    r"C:\Program Files\Microsoft",
    r"C:\Program Files\OpenJDK",
    r"C:\Program Files\BellSoft",
]

STARTUP_WARNING = (
    "본 프로그램은 마인크래프트 기본(Mojang) 런처 환경을 기준으로 합니다.\n"
    "mods 폴더가 생성되도록, 반드시 마인크래프트 26.2 버전을\n"
    "최소 1회 실행한 후 이 도구를 사용해주세요."
)


# ── 설정 파일 ────────────────────────────────────────────────────────────────
#
# v2.1 까지는 %APPDATA%/dongleland_installer/ 를 따로 썼으나,
# v3 부터는 런처 데이터 폴더(%APPDATA%/DonglelandLauncher/)로 통합한다.
# 기존 설정/로그가 있으면 최초 1회 자동으로 옮긴다.

_LEGACY_APP_NAME = "dongleland_installer"


def _app_dir() -> str:
    """설정·로그가 놓이는 런처 데이터 폴더 (%APPDATA%/DonglelandLauncher)."""
    import instance
    return instance.root_dir()


def _legacy_dir() -> str:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, _LEGACY_APP_NAME)


def _migrate_legacy_once():
    """구 폴더(dongleland_installer)의 config.json / log.txt 를 새 폴더로 이전.

    이미 새 위치에 파일이 있으면 건드리지 않는다. 실패해도 무시.
    """
    try:
        old, new = _legacy_dir(), _app_dir()
        if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(new):
            return
        os.makedirs(new, exist_ok=True)
        for name in ("config.json", "log.txt"):
            src, dst = os.path.join(old, name), os.path.join(new, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
    except Exception:
        pass


def _config_path() -> str:
    _migrate_legacy_once()
    return os.path.join(_app_dir(), "config.json")


def _log_path() -> str:
    return os.path.join(_app_dir(), "log.txt")


def write_log(message: str):
    """로그 파일에 타임스탬프와 함께 한 줄 기록.

    %APPDATA%/DonglelandLauncher/log.txt 에 누적.
    실패해도 앱 동작에 영향을 주지 않도록 모든 예외를 무시한다.
    파일이 1MB 를 넘으면 비우고 새로 시작 (무한 증가 방지).
    """
    try:
        import datetime
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 크기 제한 (1MB)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
                os.remove(path)
        except OSError:
            pass
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def load_config() -> dict:
    """설정 파일 로드. 없거나 손상됐으면 기본값 반환."""
    defaults = {
        "minecraft_dir": "",
        "theme": "system",   # system | dark | light
    }
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        # 알 수 없는 키 무시, 누락된 키는 기본값으로 채움
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(defaults)


def save_config(config: dict):
    """설정 파일 저장 (원자적)."""
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def fetch_mod_policy(config: dict):
    """원격 허용 모드 정책(dongleland 항목). 실패하면 마지막 성공값, 그것도 없으면 None.

    None 이면 호출부는 카탈로그를 그대로 쓴다 — 원격이 죽어도 목록이 비지 않는다.
    성공값은 config["_mod_policy"] 에 저장한다(오프라인 부팅 대비).
    """
    import app_meta
    cached = config.get("_mod_policy")
    if not app_meta.MOD_POLICY_URL:
        return None
    try:
        req = urllib.request.Request(app_meta.MOD_POLICY_URL,
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=5, context=_SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pol = data["servers"][app_meta.MOD_POLICY_SERVER_ID]
        if not isinstance(pol, dict) or not isinstance(pol.get("allowed_mods"), list):
            raise ValueError("형식 오류")
    except Exception:
        return cached
    if pol != cached:
        config["_mod_policy"] = pol
        save_config(config)
    return pol


# ── 마인크래프트 경로 ────────────────────────────────────────────────────────

def find_minecraft_dir(config: dict | None = None) -> str | None:
    """마인크래프트 게임 디렉터리 탐색.

    우선순위 (v3):
    1. 격리 인스턴스 (instances/dongleland/) — 게임/모드/셰이더가 여기 설치됨
    2. config["minecraft_dir"] (사용자 지정, 하위호환)
    3. %APPDATA%\\.minecraft (기본 런처, 최후 폴백)
    """
    # v3: 격리 인스턴스를 최우선. 우리 게임 클라이언트는 여기에만 설치된다.
    try:
        import instance
        inst = instance.instance_dir()
        if os.path.isdir(inst):
            return inst
    except Exception:
        pass

    if config:
        saved = config.get("minecraft_dir", "")
        if saved and os.path.isdir(saved):
            return saved

    default = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        ".minecraft"
    )
    return default if os.path.isdir(default) else None


def get_mods_dir(minecraft_dir: str) -> str:
    """활성 모드 폴더 경로.

    [자동 감지] 동글랜드 공식 모드체커를 먼저 사용한 환경에서는 게임이
    <minecraft>/modcheck/mods 에서 모드를 로드하도록 경로가 바뀐다.
    이 폴더가 존재하면 그쪽을 우선 사용한다. 없으면 표준 <minecraft>/mods.
    (표준 사용자는 modcheck 폴더가 없으므로 기존과 동일하게 동작)
    """
    modcheck_mods = os.path.join(minecraft_dir, "modcheck", "mods")
    if os.path.isdir(modcheck_mods):
        return modcheck_mods
    return os.path.join(minecraft_dir, "mods")


def get_shaderpacks_dir(minecraft_dir: str) -> str:
    """셰이더팩 폴더 경로. 없으면 호출 측에서 생성.

    [자동 감지] 공식 모드체커가 게임 폴더를 modcheck 로 리다이렉트한
    환경에서는 셰이더도 <minecraft>/modcheck/shaderpacks 에 있어야 한다.
    그 폴더가 실제로 존재할 때만 그쪽을 쓰고(안전), 아니면 표준 경로.
    """
    modcheck_sp = os.path.join(minecraft_dir, "modcheck", "shaderpacks")
    if os.path.isdir(modcheck_sp):
        return modcheck_sp
    return os.path.join(minecraft_dir, "shaderpacks")


def shaderpacks_dir_exists(minecraft_dir: str) -> bool:
    """shaderpacks 폴더가 실제로 존재하는지. 이 폴더는 Iris(또는 OptiFine)를
    설치하고 마인크래프트를 1회 이상 실행해야 자동 생성된다.
    (modcheck 리다이렉트 환경도 get_shaderpacks_dir 로 함께 처리)"""
    if not minecraft_dir:
        return False
    return os.path.isdir(get_shaderpacks_dir(minecraft_dir))


def is_iris_installed(mods_dir: str) -> bool:
    """mods 폴더에 Iris 로더 jar 가 있는지 검사(파일명 기반).
    Iris 가 있어야 셰이더팩을 실제로 사용할 수 있다."""
    if not mods_dir or not os.path.isdir(mods_dir):
        return False
    try:
        for fn in os.listdir(mods_dir):
            low = fn.lower()
            if low.endswith(".jar") and "iris" in low:
                return True
    except OSError:
        pass
    return False


def get_versions_dir(minecraft_dir: str) -> str:
    return os.path.join(minecraft_dir, "versions")


# ── Java ─────────────────────────────────────────────────────────────────────

# 폴백 값: 게임/모드 JSON 에서 요구 버전을 못 읽을 때만 사용.
# 실제 요구 버전은 required_java_major() 가 인스턴스에서 동적으로 판정한다.
REQUIRED_JAVA_MAJOR = 21


def _mc_json_java_major(instance_dir: str, version_id: str) -> int | None:
    """마인크래프트 버전 JSON 의 javaVersion.majorVersion 을 읽는다.

    Fabric 처럼 inheritsFrom 으로 바닐라를 상속하는 경우, 상속 원본 JSON 에
    javaVersion 이 들어있으므로 부모까지 따라가 확인한다.
    """
    seen = set()
    vid = version_id
    while vid and vid not in seen:
        seen.add(vid)
        jpath = os.path.join(instance_dir, "versions", vid, f"{vid}.json")
        if not os.path.isfile(jpath):
            return None
        try:
            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        jv = data.get("javaVersion")
        if isinstance(jv, dict) and jv.get("majorVersion"):
            return int(jv["majorVersion"])
        vid = data.get("inheritsFrom")  # 부모(바닐라)로 올라가 재확인
    return None


def _mods_java_major(instance_dir: str) -> int | None:
    """설치된 모드들의 fabric.mod.json 에서 요구하는 최고 Java 메이저.

    모드가 게임보다 높은 Java 를 요구할 수 있으므로(예: Fabric API 가 25 요구),
    각 jar 안의 fabric.mod.json 의 depends.java 범위를 파싱해 최댓값을 취한다.
    형식 예: "depends": {"java": ">=25"}  또는  "*"(제약 없음)
    """
    import re
    import zipfile
    mods_dir = os.path.join(instance_dir, "mods")
    if not os.path.isdir(mods_dir):
        return None
    highest = None
    for name in os.listdir(mods_dir):
        if not name.endswith(".jar"):
            continue
        try:
            with zipfile.ZipFile(os.path.join(mods_dir, name)) as z:
                if "fabric.mod.json" not in z.namelist():
                    continue
                fmj = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"))
        except Exception:
            continue
        dep = (fmj.get("depends") or {}).get("java")
        vals = dep if isinstance(dep, list) else [dep]
        for v in vals:
            if not isinstance(v, str):
                continue
            m = re.search(r"(\d+)", v)  # ">=25", "25", "[25,)" 등에서 숫자 추출
            if m:
                major = int(m.group(1))
                if highest is None or major > highest:
                    highest = major
    return highest


_REQ_CACHE = {"key": None, "value": None}


def required_java_major(instance_dir: str | None = None,
                        version_id: str | None = None) -> int:
    """이 인스턴스가 실제로 요구하는 최소 Java 메이저를 판정.

    = max(게임 JSON 요구, 설치된 모드 요구). 못 읽으면 폴백 상수.
    게임 버전·모드가 바뀌면 자동으로 따라가므로 하드코딩이 필요 없다.
    """
    if instance_dir is None:
        try:
            import instance as _inst
            instance_dir = _inst.instance_dir()
            version_id = version_id or _inst.installed_version_id()
        except Exception:
            return REQUIRED_JAVA_MAJOR
    key = (instance_dir, version_id)
    if _REQ_CACHE["key"] == key and _REQ_CACHE["value"]:
        return _REQ_CACHE["value"]
    candidates = [REQUIRED_JAVA_MAJOR]
    if version_id:
        g = _mc_json_java_major(instance_dir, version_id)
        if g:
            candidates.append(g)
    mods = _mods_java_major(instance_dir)
    if mods:
        candidates.append(mods)
    result = max(candidates)
    _REQ_CACHE["key"] = key
    _REQ_CACHE["value"] = result
    return result


def _java_major(version_str: str) -> int:
    """Java 버전 문자열 → 메이저 정수.
      '1.8.0_491' → 8   (구형 1.x 표기)
      '21.0.5'    → 21
      '24'        → 24
      '17.0.9+11' → 17
    파싱 실패 시 0.
    """
    if not version_str:
        return 0
    s = str(version_str).strip()
    # 앞부분 숫자 토큰만 취함 (예: '24+36' → '24')
    head = s.split("+")[0].split("-")[0]
    parts = head.split(".")
    try:
        first = int("".join(ch for ch in parts[0] if ch.isdigit()) or "0")
    except ValueError:
        first = 0
    if first == 1 and len(parts) >= 2:
        # 구형 표기 '1.8.0' → 메이저 8
        try:
            return int("".join(ch for ch in parts[1] if ch.isdigit()) or "0")
        except ValueError:
            return 0
    return first


def _java_version_of(java_exe: str) -> str | None:
    """특정 java 실행파일의 버전 문자열을 반환. 'java' 를 주면 PATH 의 java."""
    try:
        result = subprocess.run(
            [java_exe, "-version"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        output = (result.stderr or result.stdout).decode("utf-8", errors="ignore")
        for line in output.splitlines():
            if "version" in line.lower():
                parts = line.strip().split('"')
                if len(parts) >= 2:
                    return parts[1]
        return None
    except Exception:
        return None


def get_java_version_string() -> str | None:
    """PATH 의 java -version 결과에서 버전 문자열 추출. 실패 시 None."""
    return _java_version_of("java")


_JAVA_CACHE = {"set": False, "value": None, "req": None}


def invalidate_java_cache():
    """Java 설치 후 등 상태가 바뀌었을 때 캐시를 비운다."""
    _JAVA_CACHE["set"] = False
    _JAVA_CACHE["value"] = None


def find_valid_java(use_cache: bool = True, required: int | None = None) -> dict | None:
    """요구 버전 이상의 Java 를 찾아 {"path","version","major"} 반환. 없으면 None.

    required: 최소 요구 메이저. None 이면 required_java_major() 로 자동 판정
    (게임/모드 JSON 기반). 우선순위:
      1) PATH 의 java 가 요구 이상 → 그걸 사용 (path="java")
      2) 일반 설치 폴더들을 뒤져 각 java.exe 의 실제 버전 확인, 요구 이상 중 최고

    use_cache: 세션 중 Java 는 잘 안 바뀌므로 캐시. 단, 요구 버전이 바뀌면
    캐시를 무시하고 재검사한다(설치 후 등).
    """
    req = required if required is not None else required_java_major()
    if use_cache and _JAVA_CACHE["set"] and _JAVA_CACHE.get("req") == req:
        return _JAVA_CACHE["value"]

    result = _find_valid_java_uncached(req)
    _JAVA_CACHE["set"] = True
    _JAVA_CACHE["req"] = req
    _JAVA_CACHE["value"] = result
    return result


def _find_valid_java_uncached(required: int = REQUIRED_JAVA_MAJOR) -> dict | None:
    # 1) PATH
    path_ver = get_java_version_string()
    if path_ver and _java_major(path_ver) >= required:
        return {"path": "java", "version": path_ver, "major": _java_major(path_ver)}

    # 2) 일반 경로 탐색 (실제 버전 확인)
    best = None
    for base in JAVA_COMMON_PATHS:
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            java_exe = os.path.join(base, entry, "bin", "java.exe")
            if not os.path.isfile(java_exe):
                continue
            ver = _java_version_of(java_exe)
            if not ver:
                continue
            major = _java_major(ver)
            if major >= required:
                if best is None or major > best["major"]:
                    best = {"path": java_exe, "version": ver, "major": major}
    return best


def _find_java_in_common_paths() -> bool:
    """(하위호환 유지) 일반 폴더에 21+ Java 가 있으면 True."""
    return find_valid_java() is not None


def is_java_installed() -> bool:
    """적합한(21+) Java 가 설치되어 있는지 확인.

    주의: 예전에는 'java 가 있기만 하면 True' 였으나, 그러면 Java 8 같은
    구버전을 통과시켜 Fabric 이 구동되지 않는 문제가 있었다. 이제
    Java 21 이상(find_valid_java)일 때만 True 를 반환한다.
    """
    return find_valid_java() is not None


# ── Fabric ───────────────────────────────────────────────────────────────────

def is_fabric_installed(minecraft_dir: str, game_version: str = GAME_VERSION) -> bool:
    """versions 폴더를 검사해 game_version 용 Fabric 로더가 설치되어 있는지 확인."""
    versions_dir = get_versions_dir(minecraft_dir)
    if not os.path.isdir(versions_dir):
        return False

    for name in os.listdir(versions_dir):
        version_path = os.path.join(versions_dir, name)
        if not os.path.isdir(version_path):
            continue

        name_lower = name.lower()
        is_fabric_name = "fabric" in name_lower

        data = None
        json_path = os.path.join(version_path, f"{name}.json")
        if os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None

        is_fabric_json = False
        inherits_from  = None
        if data:
            main_class = str(data.get("mainClass", ""))
            if "fabric" in main_class.lower():
                is_fabric_json = True
            for lib in data.get("libraries", []):
                if "fabricmc" in str(lib.get("name", "")).lower():
                    is_fabric_json = True
                    break
            inherits_from = data.get("inheritsFrom")

        if not (is_fabric_name or is_fabric_json):
            continue

        # Fabric 프로필 확인 → 게임 버전도 일치하는지 확인
        if game_version in name:
            return True
        if inherits_from == game_version:
            return True

    return False


def get_fabric_version(minecraft_dir: str, game_version: str = GAME_VERSION) -> str | None:
    """설치된 Fabric 로더 버전 문자열 반환. 미설치 또는 파싱 실패 시 None."""
    versions_dir = get_versions_dir(minecraft_dir)
    if not os.path.isdir(versions_dir):
        return None

    for name in os.listdir(versions_dir):
        if "fabric" not in name.lower():
            continue
        if game_version not in name:
            continue

        json_path = os.path.join(versions_dir, name, f"{name}.json")
        if not os.path.isfile(json_path):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 폴더명에서 로더 버전 파싱: fabric-loader-0.16.5-26.1.2
            parts = name.split("-")
            # "fabric-loader-<version>-<mc>" 형식 처리
            if len(parts) >= 4 and parts[0] == "fabric" and parts[1] == "loader":
                return parts[2]
            # 버전 정보가 json 내 id 필드에 있는 경우
            return data.get("id", name)
        except Exception:
            return name

    return None


# ── Fabric 로더 업데이트 감지 (독립 기능 — 현재 앱 흐름과 미연결) ────────────
#
# 아래 두 함수는 Fabric 공식 메타 API(meta.fabricmc.net)를 사용해
# 설치된 로더 버전이 최신인지 확인한다.
# 어디서도 자동 호출하지 않으므로 기존 동작에 영향이 없다.
# 2.1 에서 UI에 연결할 때 import 해서 사용하면 된다.

FABRIC_META_BASE = "https://meta.fabricmc.net"
FABRIC_META_HOST = "meta.fabricmc.net"


def get_latest_fabric_loader(game_version: str = GAME_VERSION,
                             stable_only: bool = True) -> str | None:
    """Fabric 메타 API에서 game_version 호환 최신 로더 버전 문자열을 반환.

    GET /v2/versions/loader/{game_version}
      → [{ "loader": {"version": "0.16.5", "stable": true, ...}, ... }, ...]
        (목록은 최신순 정렬)

    stable_only=True 면 stable=True 인 첫 항목, 없으면 None.
    네트워크/파싱 실패 시 None.
    """
    url = f"{FABRIC_META_BASE}/v2/versions/loader/{urllib.parse.quote(game_version)}"

    # 호스트 검증 (보안 일관성)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != FABRIC_META_HOST:
        return None

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

    for entry in data:
        loader = entry.get("loader", {})
        ver = loader.get("version")
        if not ver:
            continue
        if stable_only and not loader.get("stable", False):
            continue
        return ver  # 목록이 최신순이므로 첫 매칭이 최신

    # stable 이 하나도 없으면 (드묾) 전체 최신이라도 반환
    if stable_only:
        first = data[0].get("loader", {}).get("version")
        return first
    return None


def check_fabric_loader_update(minecraft_dir: str,
                               game_version: str = GAME_VERSION) -> dict:
    """설치된 Fabric 로더와 최신 로더를 비교.

    반환 dict:
      {
        "installed": "0.16.5" | None,
        "latest":    "0.17.2" | None,
        "update_available": True/False,
        "status": "up_to_date" | "update_available"
                  | "not_installed" | "check_failed"
      }
    """
    installed = get_fabric_version(minecraft_dir, game_version)
    latest    = get_latest_fabric_loader(game_version, stable_only=True)

    if installed is None:
        return {"installed": None, "latest": latest,
                "update_available": False, "status": "not_installed"}
    if latest is None:
        return {"installed": installed, "latest": None,
                "update_available": False, "status": "check_failed"}

    if _version_tuple(installed) < _version_tuple(latest):
        return {"installed": installed, "latest": latest,
                "update_available": True, "status": "update_available"}
    return {"installed": installed, "latest": latest,
            "update_available": False, "status": "up_to_date"}


def _version_tuple(v: str) -> tuple:
    """'0.16.5' → (0,16,5). 자릿수가 달라도 안전하게 비교되도록 정규화한다.

    ⚠️ 튜플 길이가 다르면 (3,0) < (3,0,0) 이 되어 3.0 을 쓰는 사람에게
       '3.0.0 업데이트 있음' 이라는 오탐이 뜬다. 3자리로 패딩한다.
    ⚠️ 프리릴리스('3.0.0-beta')는 같은 숫자의 정식판보다 낮아야 한다.
       숫자만 뽑으면 3.0.0-beta == 3.0.0 이 되어 정식판으로 넘어가지 못한다.

    반환: (major, minor, patch, pre) — pre 는 정식 1, 프리릴리스 0.
    """
    s = str(v).strip().lstrip("vV")
    # '-' 또는 '+' 뒤는 프리릴리스/빌드 메타데이터
    core = s
    pre = 1
    for sep in ("-", "+"):
        if sep in core:
            core, tail = core.split(sep, 1)
            if sep == "-" and tail:
                pre = 0     # 3.0.0-beta < 3.0.0
    nums = []
    for part in core.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    nums = (nums + [0, 0, 0])[:3]      # 3자리로 패딩/절단
    return tuple(nums) + (pre,)


# ── 앱 자체 업데이트 (GitHub Releases) ─────────────────────────────────────────

GITHUB_API_HOST = "api.github.com"


def _update_repository() -> tuple[str, str]:
    """자동 업데이트용 GitHub owner/repo를 읽는다.

    배포 후에도 재빌드 없이 바꿀 수 있도록 exe 옆 launcher_config.json을
    우선 사용하고, 개발 환경에서는 소스 폴더의 같은 파일을 읽는다.
    영문·숫자·점·밑줄·하이픈 외 문자는 거부해 URL 삽입을 막는다.
    """
    import re
    import app_meta

    owner = getattr(app_meta, "GITHUB_UPDATE_OWNER", "") or ""
    repo = getattr(app_meta, "GITHUB_UPDATE_REPO", "") or ""
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "launcher_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        owner = str(cfg.get("github_owner") or owner).strip()
        repo = str(cfg.get("github_repo") or repo).strip()
    except Exception:
        pass

    valid = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not valid.fullmatch(owner) or not valid.fullmatch(repo):
        return "", ""
    return owner, repo


def get_update_urls() -> tuple[str, str]:
    """(GitHub API 최신 릴리스 URL, 사용자용 릴리스 페이지 URL)."""
    owner, repo = _update_repository()
    if not owner or not repo:
        return "", ""
    return (
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        f"https://github.com/{owner}/{repo}/releases/latest",
    )


def check_java_status(check_latest: bool = True) -> dict:
    """Java 상태를 종합 판단.

    v3 정책: Mojang 이 버전 JSON 에 지정한 공식 런타임(javaVersion)을 인스턴스
    안에 격리 설치해 쓴다. '최신 LTS 로 올리기'는 하지 않는다 —
    요구보다 높은 Java 는 Mixin/ASM 이 새 클래스 파일을 못 읽어
    (Unsupported class file major version) 게임을 깨뜨릴 수 있다.

    반환:
      {
        "status": "ok" | "managed" | "needed" | "check_failed",
        "version": "21.0.5" | None,     # 실제 사용할 Java 버전 표기
        "major": 21 | None,
        "required": 21,                 # 게임/모드가 요구하는 최소 메이저
        "managed": bool,                # 런처가 관리하는 Mojang 런타임 사용 중
        "runtime": "java-runtime-delta" | None,
      }
    status 의미:
      ok       = 사용 가능한 Java 확보 (인스턴스 런타임 또는 적합한 시스템 Java)
      managed  = 인스턴스에 Mojang 런타임 설치됨 (가장 안정적인 상태)
      needed   = Java 없음 → 게임 실행 시 자동으로 내려받음
    """
    result = {"status": "check_failed", "version": None, "major": None,
              "required": required_java_major(), "managed": False,
              "runtime": None}
    try:
        req = result["required"]

        # 1) 인스턴스에 설치된 Mojang 공식 런타임이 최우선
        try:
            import game_installer
            ri = game_installer.runtime_info()
            if ri.get("ok"):
                result["runtime"] = ri.get("name")
                result["major"] = ri.get("major")
                if ri.get("installed"):
                    result["managed"] = True
                    result["status"] = "managed"
                    result["version"] = str(ri.get("major") or "")
                    return result
        except Exception:
            pass

        # 2) 시스템 Java 폴백 (javaVersion 이 없는 구버전 등)
        valid = find_valid_java(use_cache=False, required=req)
        if valid:
            result["version"] = valid.get("version")
            result["major"] = valid.get("major")
            result["status"] = "ok"
        else:
            cur = get_java_version_string()
            result["version"] = cur
            result["major"] = _java_major(cur) if cur else None
            result["status"] = "needed"
    except Exception:
        pass
    return result


def check_app_update(current_version: str) -> dict:
    """GitHub 최신 릴리스와 현재 버전을 비교.

    안전 설계: 다운로드/교체는 하지 않고, 새 버전 정보와 릴리스 페이지 URL 만 반환.
    실제 설치는 사용자가 브라우저에서 직접 받도록 유도한다.

    Returns:
      {
        "status": "update_available" | "up_to_date" | "check_failed",
        "current": "2.0.5",
        "latest": "2.1.0" | None,
        "notes": "릴리스 노트 본문" | "",
        "page_url": "https://github.com/.../releases/latest",
        "download_url": "직접 다운로드 URL" | None,
      }
    """
    api_url, releases_page = get_update_urls()
    result = {
        "status": "check_failed",
        "current": current_version,
        "latest": None,
        "notes": "",
        "message": "",
        "page_url": releases_page,
        "download_url": None,
    }

    if not api_url or not releases_page:
        result["status"] = "not_configured"
        result["message"] = "업데이트 저장소가 아직 설정되지 않았습니다."
        return result

    # 호스트 검증
    parsed = urllib.parse.urlsplit(api_url)
    if parsed.scheme != "https" or parsed.hostname != GITHUB_API_HOST:
        return result

    try:
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 실패 원인을 구분해 알려준다. 예전에는 전부 check_failed 라서
        # 사용자가 "왜 확인이 안 되는지" 알 수 없었다.
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
        if e.code == 403 and remaining == "0":
            result["status"] = "rate_limited"
            result["message"] = ("GitHub 요청 한도를 초과했습니다. "
                                 "잠시 후 다시 확인해주세요.")
        elif e.code == 404:
            result["status"] = "no_release"
            result["message"] = "아직 공개된 릴리스가 없습니다."
        else:
            result["message"] = f"업데이트 확인 실패 (HTTP {e.code})"
        return result
    except Exception as e:
        result["message"] = f"업데이트를 확인하지 못했습니다: {type(e).__name__}"
        return result

    tag = data.get("tag_name", "")
    latest_ver = tag.lstrip("vV").strip()
    if not latest_ver:
        return result

    result["latest"] = latest_ver
    result["notes"]  = data.get("body", "") or ""

    # .exe 자산의 직접 다운로드 URL (있으면)
    for asset in data.get("assets", []):
        name = (asset.get("name") or "").lower()
        if name.endswith(".exe"):
            result["download_url"] = asset.get("browser_download_url")
            break

    # 버전 비교
    try:
        if _version_tuple(latest_ver) > _version_tuple(current_version):
            result["status"] = "update_available"
        else:
            result["status"] = "up_to_date"
    except Exception:
        result["status"] = "check_failed"

    return result


def open_url_in_browser(url: str) -> bool:
    """기본 브라우저로 URL 열기 (릴리스 다운로드 페이지용)."""
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def download_fabric_installer(progress_cb=None) -> str:
    """Fabric 설치 프로그램(.exe)을 임시 폴더에 내려받고 경로를 반환.

    보안: https + maven.fabricmc.net 호스트만 허용.
    """
    parsed = urllib.parse.urlsplit(FABRIC_INSTALLER_URL)
    if parsed.scheme != "https" or parsed.hostname != FABRIC_INSTALLER_HOST:
        raise RuntimeError(f"안전하지 않은 Fabric 다운로드 URL: {FABRIC_INSTALLER_URL}")

    tmp_dir = tempfile.mkdtemp(prefix="dongleland_fabric_")
    dest = os.path.join(tmp_dir, "fabric-installer-1.1.1.exe")

    req = urllib.request.Request(FABRIC_INSTALLER_URL, headers={"User-Agent": USER_AGENT})
    tmp_path = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CONTEXT) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        try:
                            progress_cb(min(100, int(downloaded * 100 / total)))
                        except Exception:
                            pass
        os.replace(tmp_path, dest)
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    return dest


def run_fabric_installer(installer_path: str):
    """Fabric 설치 프로그램 실행 후 완료 대기.

    보안: 파일 존재 + .exe 확장자 검증 후 실행

    주의: Fabric 설치 프로그램은 사용자가 'Install' 을 눌러야 하는 GUI 마법사다.
    CREATE_NO_WINDOW 를 주면 이 창까지 숨겨져 사용자가 아무것도 못 누르고,
    proc.wait() 가 무한 대기하게 되므로 절대 적용하지 않는다.
    """
    abs_path = os.path.abspath(installer_path)
    if not os.path.isfile(abs_path):
        raise RuntimeError(f"설치 프로그램 파일을 찾을 수 없습니다: {abs_path}")
    if not abs_path.lower().endswith(".exe"):
        raise RuntimeError(f"예상치 않은 파일 형식: {abs_path}")
    proc = subprocess.Popen([abs_path])
    proc.wait()


# ── 런처 실행 ────────────────────────────────────────────────────────────────

def launch_minecraft():
    """마인크래프트 Java Edition 런처 실행.

    우선순위:
    1. Windows 레지스트리 3가지 경로에서 InstallLocation 조회
    2. 알려진 Program Files 경로 탐색 (C·D·E 드라이브)
    3. Windows shell 'start' 명령으로 직접 실행 시도
    Bedrock(WindowsApps/Packages) 경로는 의도적으로 제외.
    """
    import string as _string

    # 1) 레지스트리에서 공식 Mojang Java 런처 경로 조회
    try:
        import winreg
        reg_keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Mojang\InstalledProducts\Minecraft Launcher"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Mojang\InstalledProducts\Minecraft Launcher"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Mojang\InstalledProducts\Minecraft Launcher"),
        ]
        for hive, subkey in reg_keys:
            try:
                key = winreg.OpenKey(hive, subkey)
                for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                    try:
                        val, _ = winreg.QueryValueEx(key, val_name)
                        # InstallLocation 이면 폴더 + exe, 나머지는 경로에서 폴더 추출
                        if val_name == "InstallLocation":
                            launcher = os.path.join(val.strip('"'), "MinecraftLauncher.exe")
                        else:
                            launcher = os.path.join(
                                os.path.dirname(val.strip('"')), "MinecraftLauncher.exe"
                            )
                        if os.path.isfile(launcher):
                            subprocess.Popen([launcher])
                            return True
                    except OSError:
                        continue
            except OSError:
                continue
    except ImportError:
        pass

    # 2) 알려진 경로 + 모든 드라이브 탐색 (Bedrock/WindowsApps 제외)
    rel_paths = [
        # Xbox / Microsoft Store 설치 (Java Edition 런처)
        os.path.join("XboxGames", "Minecraft Launcher", "Content", "Minecraft.exe"),
        os.path.join("XboxGames", "Minecraft Launcher", "Content", "MinecraftLauncher.exe"),
        # 독립 설치형 런처
        os.path.join("Program Files (x86)", "Minecraft Launcher", "MinecraftLauncher.exe"),
        os.path.join("Program Files", "Minecraft Launcher", "MinecraftLauncher.exe"),
        os.path.join("Program Files (x86)", "Minecraft", "MinecraftLauncher.exe"),
        os.path.join("Program Files", "Minecraft", "MinecraftLauncher.exe"),
    ]
    drives = [f"{d}:\\" for d in _string.ascii_uppercase
              if os.path.exists(f"{d}:\\")]
    for drive in drives:
        for rel in rel_paths:
            path = os.path.join(drive, rel)
            if os.path.isfile(path):
                subprocess.Popen([path])
                return True

    # LOCALAPPDATA 아래 독립 런처
    local = os.environ.get("LOCALAPPDATA", "")
    for sub in [
        os.path.join(local, "Minecraft Launcher", "MinecraftLauncher.exe"),
    ]:
        if os.path.isfile(sub):
            subprocess.Popen([sub])
            return True

    # 3) Windows shell 명령으로 직접 실행 시도
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "MinecraftLauncher.exe"],
            shell=False,
        )
        return True
    except Exception:
        pass

    return False


# ── Preflight 진행 상황 콜백 타입 ────────────────────────────────────────────
#
# GUI 에서 백그라운드 스레드로 run_preflight() 를 호출하면서
# on_status(step: str, message: str) 콜백으로 진행 상황을 받는다.
#
# step 목록
#   "start"          시작
#   "mc_check"       마인크래프트 경로 확인 중
#   "mc_not_found"   마인크래프트 미발견 (→ GUI 는 오류 팝업 후 종료)
#   "java_check"     Java 확인 중
#   "java_download"  Java 다운로드 중 (message: "N%")
#   "java_install"   Java 설치 프로그램 실행 중
#   "java_ok"        Java 확인 완료
#   "fabric_check"   Fabric 확인 중
#   "fabric_download" Fabric 다운로드 중
#   "fabric_install" Fabric 설치 프로그램 실행 중
#   "fabric_ok"      Fabric 확인 완료
#   "done"           모든 선행 조건 충족
#   "error"          오류 발생 (message: 오류 내용)

def run_preflight(on_status, config: dict | None = None) -> dict | None:
    """선행 조건 전체를 순서대로 확인/설치.

    Args:
        on_status: (step, message) → None 콜백
        config: 설정 딕셔너리 (없으면 파일에서 로드)

    Returns:
        성공 시: {"minecraft_dir": str, "mods_dir": str, "java_version": str}
        실패 시: None  (on_status("mc_not_found", ...) 또는 ("error", ...) 호출됨)
    """
    if config is None:
        config = load_config()

    on_status("start", "선행 조건 확인을 시작합니다")

    # 1) 마인크래프트 경로
    on_status("mc_check", "마인크래프트 설치 경로를 확인하는 중...")
    minecraft_dir = find_minecraft_dir(config)
    if not minecraft_dir:
        on_status("mc_not_found", (
            "마인크래프트 설치 경로를 찾을 수 없습니다.\n"
            "마인크래프트를 먼저 설치하고 한 번 실행해주세요."
        ))
        return None

    mods_dir = get_mods_dir(minecraft_dir)

    # config 에 경로 저장
    if config.get("minecraft_dir") != minecraft_dir:
        config["minecraft_dir"] = minecraft_dir
        save_config(config)

    # 2) Java
    # v3: Java 는 게임 설치/실행 시 mll 이 Mojang 공식 런타임을 인스턴스 안에
    #     격리 설치한다(sha1 검증, 관리자 권한 불필요).
    #     여기서 Adoptium MSI 설치 마법사를 띄우면 (1) 시스템 Java 를 건드리고
    #     (2) UAC 가 필요하며 (3) 요구보다 높은 버전이 깔릴 수 있어 Mixin 이 깨진다.
    #     따라서 상태만 확인하고 안내한다.
    on_status("java_check", "Java 설치 여부를 확인하는 중...")
    if not is_java_installed():
        on_status("java_skip",
                  "Java 는 게임 설치 시 자동으로 함께 준비됩니다.")
    else:
        java_ver = get_java_version_string() or "감지됨"
        on_status("java_ok", f"Java 확인 완료 ({java_ver})")

    # 3) Fabric
    # v3: Fabric 은 게임 설치(game_installer.install → minecraft-launcher-lib)가
    #     격리 인스턴스에 함께 설치한다. 여기서 fabric-installer.exe 를 돌리면
    #     %APPDATA%\.minecraft 에 설치되어 (1) 엉뚱한 경로에 들어가고
    #     (2) 실행 중인 마인크래프트가 jar 를 잠가 FileSystemException 이 난다.
    #     따라서 자동 설치를 하지 않고 상태만 알린다.
    on_status("fabric_check", f"Fabric {GAME_VERSION} 설치 여부를 확인하는 중...")
    if not is_fabric_installed(minecraft_dir):
        on_status("fabric_skip",
                  "Fabric 은 '게임 설치' 단계에서 자동으로 설치됩니다.")
    else:
        on_status("fabric_ok", "Fabric 확인 완료")

    fabric_ver = get_fabric_version(minecraft_dir) or "감지됨"
    on_status("fabric_ok", f"Fabric 확인 완료 ({fabric_ver})")

    on_status("done", "모든 선행 조건이 충족되었습니다")

    return {
        "minecraft_dir": minecraft_dir,
        "mods_dir": mods_dir,
        "java_version": java_ver,
        "fabric_version": fabric_ver,
    }

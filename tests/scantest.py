"""scantest.py — 모드 스캔 규칙 (네트워크 없이).

"파일은 있는데 런처엔 설치된 모드가 0개" 로 보이던 원인을 잠근다:
  1. Modrinth 조회가 실패하면 '모드 없음'이 아니라 '확인 못 함' — 설치 기록(레지스트리)대로 표시하고
     기록을 지우지 않는다
  2. 조회가 성공했을 때만 사라진 모드를 기록에서 지운다
  3. 카탈로그·의존성 어디에도 속하지 않는 jar 는 '직접 추가한 모드(extra)'로 돌려준다
"""

import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="scantest-")

import api  # noqa: E402
import modrinth_api  # noqa: E402
import preflight  # noqa: E402
from mod_catalog import VISIBLE_MODS  # noqa: E402

fails = []


def check(name, cond):
    print(("✅ " if cond else "❌ [FAIL] ") + name)
    if not cond:
        fails.append(name)


def sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


preflight.save_config = lambda cfg: None  # 테스트가 실제 설정 파일을 건드리지 않게

m1, m2 = [m for m in VISIBLE_MODS if m.get("slug")][:2]


def make_api():
    mods = tempfile.mkdtemp(prefix="mods-")
    for name in ("a.jar", "b.jar", "external.jar"):
        with open(os.path.join(mods, name), "wb") as f:
            f.write(name.encode())
    a = api.Api.__new__(api.Api)
    a._config = {}
    a._mods_dir = mods
    a._resourcepacks_dir = tempfile.mkdtemp(prefix="resourcepacks-")
    a._registry = modrinth_api.ModRegistry(mods)
    a._resourcepack_registry = modrinth_api.ModRegistry(a._resourcepacks_dir)
    a._registry.record_install(m1["id"], "a.jar", "1.0", "PID_A")
    a._registry.record_install(m2["id"], "b.jar", "2.0", "PID_B")
    a._pid_cache = {m1["slug"]: "PID_A", m2["slug"]: "PID_B"}
    a._mod_statuses = {}
    a._mod_versions = {}
    a._installed_pids = set()
    return a


def extra_names(r):
    return sorted(e["filename"] for e in r.get("extra", []))


# ── 1. Modrinth 가 죽어 있을 때 ─────────────────────────────────────────────
def boom(*_a, **_k):
    raise OSError("network down")


modrinth_api._post_json = boom
modrinth_api._get_json = boom
a = make_api()
r = a.scan_mods()
check("조회 실패: 스캔 자체는 ok", r.get("ok") is True)
check("조회 실패: 기록된 모드는 installed 유지", r["statuses"].get(m1["id"]) == "installed"
      and r["statuses"].get(m2["id"]) == "installed")
check("조회 실패: 레지스트리를 지우지 않음", a._registry.is_installed(m1["id"])
      and a._registry.is_installed(m2["id"]))
check("조회 실패: lookup_ok=False 로 알림", r.get("lookup_ok") is False)
check("조회 실패: 기록 밖 jar 만 extra", extra_names(r) == ["external.jar"])

# 예전에 런처가 관리했던 항목의 낡은 레지스트리 기록은 사용자가 직접 넣은
# 현재 jar를 숨기면 안 된다. (윤띵을 번들에서 분리한 뒤 실제로 생긴 회귀)
a = make_api()
a._registry.record_install("removed-yoondding", "external.jar", "0.2.3", None)
r = a.scan_mods()
check("구형 레지스트리 기록이 있어도 현재 카탈로그 밖 jar 는 extra",
      "external.jar" in extra_names(r))
check("구형 레지스트리 기록이 남은 extra 도 삭제 가능",
      a.remove_extra_mod("external.jar").get("ok") is True)

# ── 2. 조회 성공 — a.jar 만 Modrinth 가 알아봄 ──────────────────────────────
a = make_api()
sha_a = sha1(os.path.join(a._mods_dir, "a.jar"))


def post_ok(url, payload):
    if url.endswith("/version_files"):
        return {sha_a: {"project_id": "PID_A", "version_number": "1.0"}}
    return {}


modrinth_api._post_json = post_ok
r = a.scan_mods()
check("조회 성공: 매칭된 모드는 installed", r["statuses"].get(m1["id"]) == "installed")
check("조회 성공: 매칭 안 된 모드는 not_installed", r["statuses"].get(m2["id"]) == "not_installed")
check("조회 성공: 사라진 모드만 기록에서 제거", a._registry.is_installed(m1["id"])
      and not a._registry.is_installed(m2["id"]))
check("조회 성공: lookup_ok=True", r.get("lookup_ok") is True)
check("조회 성공: 주인 없는 jar 는 extra", extra_names(r) == ["b.jar", "external.jar"])

# ── 2b. 번들 모드(ModCheck)는 바이트까지 정본이어야 한다 ─────────────────────
# 서버 모드검사는 클라이언트가 자기 jar 바이트 전체를 HMAC 으로 서명해 보내게 한다.
# 이름·버전이 같아도 바이트가 다르면 "잘못된 핸드셰이크 응답"으로 추방된다.
from mod_catalog import MODS  # noqa: E402
bundled = next((m for m in MODS if m.get("bundled_asset")), None)
if bundled:
    src = api._resource(os.path.join("assets", bundled["bundled_asset"]))
    a = make_api()
    dst = os.path.join(a._mods_dir, bundled["bundled_asset"])
    with open(dst, "wb") as f:
        f.write(b"corrupted or different build")
    a.scan_mods()
    check("번들 모드 바이트가 다르면 정본으로 복구", open(dst, "rb").read() == open(src, "rb").read())
    check("복구 후 설치됨", a._mod_statuses.get(bundled["id"]) == "installed")
    mtime = os.path.getmtime(dst)
    a.scan_mods()
    check("정본이면 다시 쓰지 않음", os.path.getmtime(dst) == mtime)

# ── 3. 직접 추가한 모드 삭제 ────────────────────────────────────────────────
d = a.remove_extra_mod("external.jar")
check("extra 삭제: 성공", d.get("ok") is True
      and not os.path.exists(os.path.join(a._mods_dir, "external.jar")))
check("extra 삭제: 경로 탈출 거부", a.remove_extra_mod("../x.jar").get("ok") is False)
check("extra 삭제: jar 아닌 파일 거부", a.remove_extra_mod("x.txt").get("ok") is False)
check("extra 삭제: 카탈로그 관리 파일 거부(필수모드 우회 방지)",
      a.remove_extra_mod("a.jar").get("ok") is False
      and os.path.exists(os.path.join(a._mods_dir, "a.jar")))

print(f"\n{len(fails)} 실패" if fails else "\n전부 통과")
sys.exit(1 if fails else 0)

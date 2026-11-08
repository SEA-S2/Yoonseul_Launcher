# -*- coding: utf-8 -*-
"""
Modrinth API 연동 모듈 (v2)
https://docs.modrinth.com/api/

변경 이력
---------
v2  get_project_info, install_mod_by_slug 추가
    ModRegistry 클래스 추가
"""

import os
import json
import hashlib
import ssl
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

API_BASE = "https://api.modrinth.com/v2"
# Modrinth 는 고유한 User-Agent 를 요구한다 (일반적인 UA 는 차단될 수 있음)
USER_AGENT = "Yoonseul_Launcher/2.7.0"

DEFAULT_GAME_VERSION = "26.2"
DEFAULT_LOADER = "fabric"
# None 이면 release→beta→alpha 우선순위 폴백 사용 (특정 채널 강제 시 튜플 지정)
DEFAULT_VERSION_TYPES = None

ALLOWED_DOWNLOAD_HOSTS = ("cdn.modrinth.com",)


# ── SSL 컨텍스트 (PyInstaller exe 의 인증서 검증 실패 방지) ─────────────────────
#
# PyInstaller --onefile exe 는 시스템 루트 인증서를 못 찾아
# [SSL: CERTIFICATE_VERIFY_FAILED] 가 발생할 수 있다.
# certifi 가 있으면 그 인증서 묶음을 사용하고, 없으면 시스템 기본으로 폴백한다.
def _make_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

_SSL_CONTEXT = _make_ssl_context()


# ── 예외 ────────────────────────────────────────────────────────────────────

class DownloadSecurityError(Exception):
    """URL / 파일명 / 무결성 검증 실패"""


# ── 보안 헬퍼 ───────────────────────────────────────────────────────────────

def _validate_download_url(url: str):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise DownloadSecurityError(f"안전하지 않은 URL 스킴: {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise DownloadSecurityError(f"허용되지 않은 다운로드 호스트: {parsed.hostname!r}")


def _sanitize_filename(filename: str, allowed_exts: tuple = (".jar",)) -> str:
    if not filename:
        raise DownloadSecurityError("파일명이 비어 있습니다")
    base = os.path.basename(filename)
    if base != filename or base in ("", ".", ".."):
        raise DownloadSecurityError(f"안전하지 않은 파일명: {filename!r}")
    if not any(base.lower().endswith(ext) for ext in allowed_exts):
        raise DownloadSecurityError(
            f"허용되지 않은 파일 형식({'/'.join(allowed_exts)}): {filename!r}"
        )
    return base


# ── 저수준 HTTP ──────────────────────────────────────────────────────────────

def _post_json(url: str, payload: dict):
    """POST + JSON 응답. 배치 엔드포인트용."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_versions_from_hashes(hashes: list, algorithm: str = "sha1") -> dict:
    """여러 파일 해시 → 버전 정보 (배치).

    POST /version_files. 개별 get_version_from_hash 를 N 번 호출하는 대신
    한 번에 처리한다 (부팅 시 모드 스캔이 N 회 → 1 회).

    반환: { hash: version_dict, ... }  (매칭 안 된 해시는 키가 없음)
          조회 자체가 실패하면 None — '매칭 없음({})'과 구분해야 호출부가
          설치된 모드를 '없음'으로 오판하지 않는다.
    """
    if not hashes:
        return {}
    # Modrinth 는 대문자 해시를 거부한다 (modrinth/code#2707)
    lowered = [h.lower() for h in hashes]
    try:
        return _post_json(f"{API_BASE}/version_files",
                          {"hashes": lowered, "algorithm": algorithm}) or {}
    except Exception:
        return None


def get_latest_versions_from_hashes(hashes: list, game_version: str,
                                    loader: str | None = None,
                                    algorithm: str = "sha1") -> dict:
    """여러 해시 → 각 프로젝트의 최신 호환 버전 (배치).

    POST /version_files/update. 업데이트 확인을 해시별로 N 번 도는 대신
    한 번에 처리한다.

    반환: { 요청한 hash: 최신 version_dict, ... }
    """
    if not hashes:
        return {}
    payload = {
        # Modrinth 는 대문자 해시를 거부한다 (modrinth/code#2707)
        "hashes": [h.lower() for h in hashes],
        "algorithm": algorithm,
        "game_versions": [game_version],
    }
    if loader:
        payload["loaders"] = [loader]
    try:
        return _post_json(f"{API_BASE}/version_files/update", payload) or {}
    except Exception:
        return {}


# sha1 계산 결과 캐시: (경로, mtime, size) → sha1
# 같은 파일을 여러 번 해싱하지 않도록 (부팅마다 mods 폴더 전체를 읽는 비용 제거)
_SHA1_CACHE: dict = {}


def sha1_of_file_cached(path: str) -> str:
    """sha1_of_file 의 캐시판. 파일이 바뀌면(mtime/size) 자동 무효화."""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return sha1_of_file(path)
    hit = _SHA1_CACHE.get(key)
    if hit:
        return hit
    val = sha1_of_file(path)
    _SHA1_CACHE[key] = val
    # 캐시가 무한정 커지지 않도록 상한
    if len(_SHA1_CACHE) > 256:
        _SHA1_CACHE.pop(next(iter(_SHA1_CACHE)))
    return val


def required_dependency_project_ids(version: dict) -> list:
    """Modrinth 버전 JSON 의 dependencies 에서 '필수' 프로젝트 id 만 추출.

    dependency_type: required | optional | incompatible | embedded
    embedded 는 이미 jar 안에 들어있으므로 설치하지 않는다.
    """
    out = []
    for dep in (version or {}).get("dependencies", []) or []:
        if dep.get("dependency_type") != "required":
            continue
        pid = dep.get("project_id")
        if pid:
            out.append(pid)
    return out


def get_project_slug(project_id: str) -> str | None:
    """project_id → slug (설치 함수가 slug 를 받으므로 필요)."""
    try:
        data = _get_json(f"{API_BASE}/project/{urllib.parse.quote(project_id)}")
        return data.get("slug") or project_id
    except Exception:
        return None


def get_projects_meta(project_ids: list) -> dict:
    """여러 project_id → {id: {"slug","title"}} (배치 1회).

    GET /projects?ids=[...]
    """
    if not project_ids:
        return {}
    try:
        q = urllib.parse.urlencode({"ids": json.dumps(list(project_ids))})
        arr = _get_json(f"{API_BASE}/projects?{q}") or []
        return {p["id"]: {"slug": p.get("slug") or p["id"],
                          "title": p.get("title") or p.get("slug") or p["id"]}
                for p in arr if p.get("id")}
    except Exception:
        return {}


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sha1_of_file(path: str) -> str:
    h = hashlib.sha1(usedforsecurity=False)  # Modrinth 파일 식별용(보안 아님)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url: str, dest_path: str, progress_cb=None, expected_sha1: str | None = None):
    """파일 다운로드 + 무결성 검증.

    - URL 호스트 검증 (cdn.modrinth.com 만 허용)
    - 다운로드 중 sha1 계산 → expected_sha1 과 불일치 시 임시파일 삭제 후 예외
    - .part 임시 파일 사용 → os.replace() 원자적 교체
    """
    _validate_download_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp_path = dest_path + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CONTEXT) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            hasher = hashlib.sha1(usedforsecurity=False)  # Modrinth 파일 식별용(보안 아님)
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(100, int(downloaded * 100 / total)))

        if expected_sha1 and hasher.hexdigest() != expected_sha1:
            raise DownloadSecurityError(
                f"해시 불일치 — 기대: {expected_sha1[:12]}... 실제: {hasher.hexdigest()[:12]}..."
            )
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    os.replace(tmp_path, dest_path)


# ── Modrinth API 호출 ────────────────────────────────────────────────────────

def get_version_from_hash(file_hash: str, algorithm: str = "sha1"):
    """파일 해시로 Modrinth 버전 정보 조회. 미등록 파일이면 None 반환."""
    url = f"{API_BASE}/version_file/{file_hash}?algorithm={algorithm}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def get_project_versions(project_id: str, game_version: str, loader: str | None = None) -> list:
    """게임버전 / (선택)로더 조건에 맞는 버전 목록 반환.

    loader=None 이면 로더 필터 없이 게임 버전만 적용 (셰이더팩 등 로더 무관 항목).
    """
    params = {
        "game_versions": json.dumps([game_version]),
    }
    if loader:
        params["loaders"] = json.dumps([loader])
    url = f"{API_BASE}/project/{project_id}/version?{urllib.parse.urlencode(params)}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def get_version_by_id(version_id: str) -> dict | None:
    """version_id 로 특정 버전 하나를 조회 (이전 버전 되돌리기용)."""
    url = f"{API_BASE}/version/{urllib.parse.quote(version_id)}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _parse_date(version: dict) -> datetime:
    raw = version.get("date_published", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def get_latest_compatible_version(
    project_id: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
) -> dict | None:
    """game_version / loader 조건에 맞는 가장 최신 버전 반환.

    버전 채널 우선순위 (channel fallback):
      1) release 가 있으면 release 중 최신
      2) release 없으면 beta 중 최신
      3) beta 도 없으면 alpha 중 최신

    version_types 인자로 명시적 채널 제한도 가능하지만,
    기본 동작은 위 우선순위 폴백이다.
    """
    versions = get_project_versions(project_id, game_version, loader)
    if not versions:
        return None

    # version_types 가 명시적으로 지정되면 해당 채널만 사용 (특정 채널 강제)
    if version_types:
        filtered = [v for v in versions if v.get("version_type") in version_types]
        if not filtered:
            return None
        filtered.sort(key=_parse_date, reverse=True)
        return filtered[0]

    # 기본: release → beta → alpha 우선순위 폴백
    for channel in ("release", "beta", "alpha"):
        channel_versions = [v for v in versions if v.get("version_type") == channel]
        if channel_versions:
            channel_versions.sort(key=_parse_date, reverse=True)
            return channel_versions[0]

    # version_type 정보가 없는 경우 전체 중 최신
    versions.sort(key=_parse_date, reverse=True)
    return versions[0]


def pick_primary_file(version: dict) -> dict | None:
    files = version.get("files", [])
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def get_project_info(slug_or_id: str) -> dict | None:
    """프로젝트 기본 정보 조회 (title, description, icon_url 등).
    실패 시 None 반환."""
    url = f"{API_BASE}/project/{slug_or_id}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def get_projects_batch(slugs: list) -> dict:
    """slug 목록을 단 1번의 API 호출로 조회 → {slug: project_id} 맵 반환.

    Modrinth GET /v2/projects?ids=["slug1","slug2",...] 사용.
    실패한 slug 는 결과에서 제외된다.
    """
    if not slugs:
        return {}
    ids_param = urllib.parse.quote(json.dumps(slugs))
    url = f"{API_BASE}/projects?ids={ids_param}"
    try:
        data = _get_json(url)
        return {item["slug"]: item["id"] for item in data if "slug" in item and "id" in item}
    except Exception:
        return {}


def get_projects_media_batch(slugs: list) -> dict:
    """slug 목록을 1번의 호출로 조회 → 미디어/메타 맵 반환.

    반환: {slug: {"project_id","title","icon_url","image","downloads"}}
      - image: 대표 갤러리 이미지(featured 우선, 없으면 첫 항목), 없으면 None
      - icon_url: 프로젝트 아이콘, 없으면 None
    카드/라이브러리 썸네일과 다운로드 수 표시에 사용.
    실패한 slug 는 결과에서 제외된다.
    """
    if not slugs:
        return {}
    ids_param = urllib.parse.quote(json.dumps(slugs))
    url = f"{API_BASE}/projects?ids={ids_param}"
    try:
        data = _get_json(url)
    except Exception:
        return {}
    out = {}
    for item in data:
        slug = item.get("slug")
        if not slug:
            continue
        # 대표 이미지 선택
        image = None
        gallery = item.get("gallery") or []
        if gallery:
            feat = [g for g in gallery if g.get("featured")]
            pick = (feat[0] if feat else gallery[0])
            image = pick.get("url") if isinstance(pick, dict) else None
        out[slug] = {
            "project_id": item.get("id"),
            "title": item.get("title"),
            "icon_url": item.get("icon_url"),
            "image": image,
            "downloads": item.get("downloads", 0),
        }
    return out


def get_project_members(slug_or_id: str) -> list:
    """프로젝트 팀 멤버(제작자) 목록 조회. 실패 시 빈 리스트 반환."""
    url = f"{API_BASE}/project/{slug_or_id}/members"
    try:
        return _get_json(url)
    except urllib.error.HTTPError:
        return []


def get_project_author(slug_or_id: str) -> str:
    """제작자 이름을 문자열로 반환. 대표 멤버(Owner) 우선, 없으면 첫 번째 멤버."""
    members = get_project_members(slug_or_id)
    if not members:
        return "알 수 없음"
    owners = [m for m in members if m.get("role", "").lower() == "owner"]
    target = owners[0] if owners else members[0]
    return target.get("user", {}).get("username", "알 수 없음")


def search_mods(
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    limit: int = 40,
    offset: int = 0,
    query: str = "",
) -> dict:
    """Modrinth 전체에서 현재 게임 버전/로더와 호환되는 모드를 검색한다."""
    facets = [
        ["project_type:mod"],
        [f"categories:{loader}"],
        [f"versions:{game_version}"],
    ]
    params = {
        "facets": json.dumps(facets),
        "limit": str(limit),
        "offset": str(offset),
        "index": "relevance" if query else "downloads",
    }
    if query:
        params["query"] = query
    url = f"{API_BASE}/search?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except Exception as e:
        return {"total": 0, "offset": offset, "limit": limit,
                "mods": [], "error": str(e)}

    mods = []
    for hit in data.get("hits", []):
        gallery = hit.get("gallery") or []
        featured = hit.get("featured_gallery")
        image = featured or (gallery[0] if gallery else None)
        mods.append({
            "project_id": hit.get("project_id"),
            "slug": hit.get("slug"),
            "title": hit.get("title"),
            "description": hit.get("description", ""),
            "author": hit.get("author", ""),
            "icon_url": hit.get("icon_url") or None,
            "image": image,
            "downloads": hit.get("downloads", 0),
            "categories": hit.get("display_categories") or hit.get("categories", []),
        })
    return {"total": data.get("total_hits", len(mods)), "offset": offset,
            "limit": limit, "mods": mods}


def install_mod_by_slug(
    slug: str,
    mods_dir: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
    progress_cb=None,
    version_id: str | None = None,
) -> dict:
    """Modrinth에서 최신 호환 버전을 mods_dir 에 다운로드.

    version_id 를 주면 그 버전을 그대로 설치한다(이전 버전 되돌리기용).

    반환값:
        status  "installed" | "up_to_date" | "no_version" | "error"
        filename, version, project_id  (status 가 installed / up_to_date 일 때)
        message  (status 가 error / no_version 일 때)
    """
    if version_id:
        latest = get_version_by_id(version_id)
        if not latest:
            return {"status": "no_version", "message": f"{slug}: 버전을 찾을 수 없음"}
    else:
        latest = get_latest_compatible_version(slug, game_version, loader, version_types)
    if not latest:
        return {"status": "no_version", "message": f"{slug}: {game_version}/{loader} 호환 버전 없음"}

    primary = pick_primary_file(latest)
    if not primary:
        return {"status": "error", "message": f"{slug}: 다운로드 파일 없음"}

    try:
        filename = _sanitize_filename(primary["filename"])
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}

    dest_path = os.path.join(mods_dir, filename)

    # 경로 이중 검증
    if not os.path.abspath(dest_path).startswith(os.path.abspath(mods_dir) + os.sep):
        return {"status": "error", "message": "비정상적인 대상 경로"}

    expected_sha1 = primary.get("hashes", {}).get("sha1")

    # 이미 동일 파일이 있으면 스킵
    if os.path.isfile(dest_path) and expected_sha1:
        if sha1_of_file(dest_path) == expected_sha1:
            return {
                "status": "up_to_date",
                "filename": filename,
                "requires": required_dependency_project_ids(latest),
                "version": latest.get("version_number", "?"),
                "project_id": latest.get("project_id"),
            }

    os.makedirs(mods_dir, exist_ok=True)

    try:
        download_file(primary["url"], dest_path, progress_cb=progress_cb, expected_sha1=expected_sha1)
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"다운로드 실패: {e}"}

    return {
        "status": "installed",
        "filename": filename,
        "version": latest.get("version_number", "?"),
        "project_id": latest.get("project_id"),
        "requires": required_dependency_project_ids(latest),
    }


def install_resourcepack_by_slug(
    slug: str,
    resourcepacks_dir: str,
    game_version: str = DEFAULT_GAME_VERSION,
    version_types: tuple = DEFAULT_VERSION_TYPES,
    progress_cb=None,
    version_id: str | None = None,
) -> dict:
    """Modrinth 리소스팩을 resourcepacks 폴더에 안전하게 설치한다."""
    if version_id:
        latest = get_version_by_id(version_id)
        if not latest:
            return {"status": "no_version", "message": f"{slug}: 버전을 찾을 수 없음"}
    else:
        # 리소스팩은 Fabric/Forge 로더와 무관하므로 게임 버전만 확인한다.
        latest = get_latest_compatible_version(slug, game_version, None, version_types)
    if not latest:
        return {"status": "no_version", "message": f"{slug}: {game_version} 호환 버전 없음"}

    primary = pick_primary_file(latest)
    if not primary:
        return {"status": "error", "message": f"{slug}: 다운로드 파일 없음"}
    try:
        filename = _sanitize_filename(primary["filename"], allowed_exts=(".zip",))
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}

    dest_path = os.path.join(resourcepacks_dir, filename)
    if not os.path.abspath(dest_path).startswith(os.path.abspath(resourcepacks_dir) + os.sep):
        return {"status": "error", "message": "비정상적인 대상 경로"}

    expected_sha1 = primary.get("hashes", {}).get("sha1")
    if os.path.isfile(dest_path) and expected_sha1:
        if sha1_of_file(dest_path) == expected_sha1:
            return {
                "status": "up_to_date", "filename": filename,
                "version": latest.get("version_number", "?"),
                "project_id": latest.get("project_id"), "requires": [],
            }

    os.makedirs(resourcepacks_dir, exist_ok=True)
    try:
        download_file(primary["url"], dest_path, progress_cb=progress_cb,
                      expected_sha1=expected_sha1)
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"다운로드 실패: {e}"}
    return {
        "status": "installed", "filename": filename,
        "version": latest.get("version_number", "?"),
        "project_id": latest.get("project_id"), "requires": [],
    }


def sync_mod_file(
    path: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
) -> dict:
    """로컬 jar 파일을 검사해 최신 버전이 있으면 교체.

    status: up_to_date | updated | unknown | no_compatible_version |
            no_matching_release | error
    """
    filename = os.path.basename(path)

    try:
        local_hash = sha1_of_file(path)
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"파일 읽기 실패: {e}"}

    try:
        version_info = get_version_from_hash(local_hash)
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"Modrinth 조회 실패: {e}"}

    if version_info is None:
        return {"file": filename, "status": "unknown", "message": "Modrinth 미등록 파일"}

    project_id = version_info["project_id"]
    current_version = version_info.get("version_number", "?")

    try:
        latest = get_latest_compatible_version(project_id, game_version, loader, version_types)
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"최신 버전 조회 실패: {e}"}

    if latest is None:
        try:
            any_ver = get_latest_compatible_version(project_id, game_version, loader, None)
        except Exception:
            any_ver = None
        if version_types and any_ver:
            return {
                "file": filename,
                "status": "no_matching_release",
                "message": f"호환 버전 있지만 release 채널 아님 (베타/알파만 존재)",
            }
        return {"file": filename, "status": "no_compatible_version",
                "message": f"{game_version}/{loader} 호환 버전 없음"}

    primary = pick_primary_file(latest)
    if not primary:
        return {"file": filename, "status": "error", "message": "최신 버전에 파일 없음"}

    latest_hash = primary.get("hashes", {}).get("sha1")
    latest_version = latest.get("version_number", "?")

    if latest_hash == local_hash:
        return {"file": filename, "status": "up_to_date",
                "message": f"최신 버전 ({current_version})"}

    # 교체
    try:
        new_filename = _sanitize_filename(primary["filename"])
    except DownloadSecurityError as e:
        return {"file": filename, "status": "error", "message": f"보안 검증 실패: {e}"}

    directory = os.path.dirname(path)
    new_path = os.path.join(directory, new_filename)

    if os.path.commonpath([os.path.abspath(directory), os.path.abspath(new_path)]) \
            != os.path.abspath(directory):
        return {"file": filename, "status": "error", "message": "대상 경로가 mods 폴더를 벗어남"}

    try:
        download_file(primary["url"], new_path, expected_sha1=latest_hash)
    except DownloadSecurityError as e:
        return {"file": filename, "status": "error", "message": f"보안 검증 실패: {e}"}
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"다운로드 실패: {e}"}

    if os.path.abspath(new_path) != os.path.abspath(path) and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass

    return {"file": filename, "status": "updated",
            "message": f"{current_version} → {latest_version} 업데이트 완료",
            "new_file": new_filename}


def sync_directory(
    directory: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
    progress_cb=None,
) -> list:
    if not os.path.isdir(directory):
        return []
    jars = sorted(f for f in os.listdir(directory) if f.lower().endswith(".jar"))
    results = []
    for i, fname in enumerate(jars, 1):
        r = sync_mod_file(os.path.join(directory, fname), game_version, loader, version_types)
        results.append(r)
        if progress_cb:
            progress_cb(i, len(jars), r)
    return results


# ── 셰이더팩 (project_type=shader, shaderpacks/ 폴더) ──────────────────────────
#
# 모드와의 차이:
#   - Modrinth project_type 이 "shader"
#   - 설치 위치가 shaderpacks/ (.zip 파일)
#   - 검색 단계에서 categories:iris 로 Iris 호환 셰이더만 노출 (search_shaders)
#   - 버전 다운로드 단계는 loader=None — 셰이더 버전 파일은 로더 태그가
#     일관되지 않아, 검색에서 이미 Iris 로 거른 뒤 게임 버전만으로 최신 버전을 찾는다
#   - 동글랜드 허용 목록 없음 → Modrinth 전체 검색
#
# 2.1 HTML UI 가 그대로 호출할 수 있도록, 모든 함수는
# JSON 직렬화 가능한 dict / list 만 반환한다.

SHADER_EXTS = (".zip",)


def search_shaders(
    game_version: str = DEFAULT_GAME_VERSION,
    limit: int = 40,
    offset: int = 0,
    sort: str = "downloads",
    query: str = "",
) -> dict:
    """Modrinth 에서 셰이더팩 검색.

    Args:
        game_version: 게임 버전 (호환 필터)
        limit: 가져올 개수
        offset: 페이지네이션 오프셋
        sort: relevance | downloads | follows | newest | updated
        query: 검색어 (빈 문자열이면 전체)

    Returns (HTML UI 가 그대로 렌더할 수 있는 형태):
      {
        "total": int,                 # 전체 검색 결과 수
        "offset": int,
        "limit": int,
        "shaders": [
          {
            "project_id": str,
            "slug": str,
            "title": str,
            "description": str,
            "icon_url": str | None,    # 썸네일
            "downloads": int,
            "follows": int,
            "categories": [str, ...],  # 성능/스타일 태그
            "gallery": [str, ...],     # 스크린샷 URL 목록 (검색 결과의 featured_gallery)
          }, ...
        ]
      }
    실패 시 {"total":0, "shaders":[], "error": "..."}.
    """
    facets = [
        ["project_type:shader"],     # 셰이더팩만
        ["categories:iris"],         # Iris 셰이더 로더 호환만 (loaders는 categories에 포함됨)
        [f"versions:{game_version}"],# 게임 버전 호환만
    ]
    params = {
        "facets": json.dumps(facets),
        "limit": str(limit),
        "offset": str(offset),
        "index": sort,
    }
    if query:
        params["query"] = query
    url = f"{API_BASE}/search?{urllib.parse.urlencode(params)}"

    try:
        data = _get_json(url)
    except Exception as e:
        return {"total": 0, "offset": offset, "limit": limit,
                "shaders": [], "error": str(e)}

    shaders = []
    for hit in data.get("hits", []):
        gallery = hit.get("gallery", []) or []
        featured = hit.get("featured_gallery")
        if featured and featured not in gallery:
            gallery = [featured] + gallery
        shaders.append({
            "project_id":  hit.get("project_id"),
            "slug":        hit.get("slug"),
            "title":       hit.get("title"),
            "description": hit.get("description", ""),
            "icon_url":    hit.get("icon_url") or None,
            "downloads":   hit.get("downloads", 0),
            "follows":     hit.get("follows", 0),
            "categories":  hit.get("display_categories") or hit.get("categories", []),
            "gallery":     gallery,
        })

    return {
        "total":  data.get("total_hits", len(shaders)),
        "offset": offset,
        "limit":  limit,
        "shaders": shaders,
    }


def get_shader_detail(slug_or_id: str) -> dict | None:
    """셰이더팩 상세 정보 (큰 갤러리 포함). 실패 시 None.

    Returns:
      {
        "project_id", "slug", "title", "description", "body"(마크다운 설명),
        "icon_url", "downloads", "followers",
        "categories": [...],
        "gallery": [{"url", "title", "description", "featured"}, ...]
      }
    """
    info = get_project_info(slug_or_id)
    if not info:
        return None
    gallery = []
    for g in info.get("gallery", []) or []:
        gallery.append({
            "url":         g.get("url"),
            "title":       g.get("title", ""),
            "description": g.get("description", ""),
            "featured":    g.get("featured", False),
        })
    return {
        "project_id":  info.get("id"),
        "slug":        info.get("slug"),
        "title":       info.get("title"),
        "description": info.get("description", ""),
        "body":        info.get("body", ""),
        "icon_url":    info.get("icon_url") or None,
        "downloads":   info.get("downloads", 0),
        "followers":   info.get("followers", 0),
        "categories":  info.get("categories", []),
        "gallery":     gallery,
    }


def install_shader_by_slug(
    slug: str,
    shaderpacks_dir: str,
    game_version: str = DEFAULT_GAME_VERSION,
    progress_cb=None,
    version_id: str | None = None,
) -> dict:
    """셰이더팩 최신 호환 버전을 shaderpacks_dir 에 다운로드.

    version_id 를 주면 그 버전을 그대로 설치한다(이전 버전 되돌리기용).
    로더 무관(loader=None) — 게임 버전 호환만 확인.
    채널 폴백(release→beta→alpha)은 모드와 동일.

    Returns:
      status  "installed" | "up_to_date" | "no_version" | "error"
      filename, version, project_id   (성공 시)
      message  (실패 시)
    """
    if version_id:
        latest = get_version_by_id(version_id)
    else:
        latest = get_latest_compatible_version(slug, game_version, loader=None)
    if not latest:
        return {"status": "no_version",
                "message": f"{slug}: {game_version} 호환 셰이더 버전 없음"}

    primary = pick_primary_file(latest)
    if not primary:
        return {"status": "error", "message": f"{slug}: 다운로드 파일 없음"}

    try:
        filename = _sanitize_filename(primary["filename"], allowed_exts=SHADER_EXTS)
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}

    dest_path = os.path.join(shaderpacks_dir, filename)
    if not os.path.abspath(dest_path).startswith(os.path.abspath(shaderpacks_dir) + os.sep):
        return {"status": "error", "message": "비정상적인 대상 경로"}

    expected_sha1 = primary.get("hashes", {}).get("sha1")

    # 이미 동일 파일이 있으면 스킵
    if os.path.isfile(dest_path) and expected_sha1:
        if sha1_of_file(dest_path) == expected_sha1:
            return {"status": "up_to_date", "filename": filename,
                    "version": latest.get("version_number", "?"),
                    "project_id": latest.get("project_id")}

    os.makedirs(shaderpacks_dir, exist_ok=True)
    try:
        download_file(primary["url"], dest_path, progress_cb=progress_cb,
                      expected_sha1=expected_sha1)
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"다운로드 실패: {e}"}

    return {"status": "installed", "filename": filename,
            "version": latest.get("version_number", "?"),
            "project_id": latest.get("project_id")}


def scan_installed_shaders(shaderpacks_dir: str) -> dict:
    """shaderpacks/ 폴더의 .zip 파일을 sha1 → Modrinth 로 식별.

    Returns: { project_id: {"filename", "sha1", "version"} }
    Modrinth 미등록(커스텀) 셰이더는 결과에서 제외된다.
    HTML UI 는 이 맵으로 목록 항목의 '설치됨' 여부를 판단한다.
    """
    result = {}
    if not os.path.isdir(shaderpacks_dir):
        return result
    for fname in os.listdir(shaderpacks_dir):
        if not fname.lower().endswith(".zip") or fname.startswith("."):
            continue
        path = os.path.join(shaderpacks_dir, fname)
        try:
            local_sha1 = sha1_of_file(path)
            info = get_version_from_hash(local_sha1)
        except Exception:
            continue
        if info and info.get("project_id"):
            result[info["project_id"]] = {
                "filename": fname,
                "sha1": local_sha1,
                "version": info.get("version_number", "?"),
            }
    return result


def check_shader_update(slug_or_id: str, local_sha1: str,
                        game_version: str = DEFAULT_GAME_VERSION) -> dict:
    """설치된 셰이더의 업데이트 여부 확인 (sha1 비교).

    Returns:
      {"update_available": bool, "latest_version": str|None,
       "latest_sha1": str|None, "status": "up_to_date"|"update_available"|"no_version"}
    """
    latest = get_latest_compatible_version(slug_or_id, game_version, loader=None)
    if not latest:
        return {"update_available": False, "latest_version": None,
                "latest_sha1": None, "status": "no_version"}
    primary = pick_primary_file(latest)
    latest_sha1 = primary.get("hashes", {}).get("sha1") if primary else None
    if latest_sha1 and latest_sha1 != local_sha1:
        return {"update_available": True,
                "latest_version": latest.get("version_number", "?"),
                "latest_sha1": latest_sha1, "status": "update_available"}
    return {"update_available": False,
            "latest_version": latest.get("version_number", "?"),
            "latest_sha1": latest_sha1, "status": "up_to_date"}


def remove_shader_file(shaderpacks_dir: str, filename: str) -> dict:
    """shaderpacks/ 에서 셰이더 .zip 삭제.

    Returns: {"status": "removed"|"not_found"|"error", "message": str(선택)}
    """
    safe = os.path.basename(filename)
    path = os.path.join(shaderpacks_dir, safe)
    if not os.path.abspath(path).startswith(os.path.abspath(shaderpacks_dir) + os.sep):
        return {"status": "error", "message": "비정상적인 경로"}
    if not os.path.isfile(path):
        return {"status": "not_found"}
    try:
        os.remove(path)
        return {"status": "removed"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── 레지스트리 ────────────────────────────────────────────────────────────────

class ModRegistry:
    """설치된 모드 추적 파일 (.dongleland_registry.json).

    구조:
    {
      "version": 1,
      "mods": {
        "<catalog_id>": {
          "filename": "sodium-fabric-0.8.12.jar",
          "version":  "0.8.12",
          "project_id": "AANobbMI"   # None 이면 번들 모드
        }
      }
    }
    """

    REGISTRY_FILENAME = ".yoonseul_registry.json"

    def __init__(self, mods_dir: str):
        self._mods_dir = mods_dir
        self._path = os.path.join(mods_dir, self.REGISTRY_FILENAME)
        self._data = self._load()

    # ── 내부 ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "mods" not in data:
                raise ValueError
            return data
        except Exception:
            return {"version": 1, "mods": {}}

    def _save(self):
        try:
            os.makedirs(self._mods_dir, exist_ok=True)
            tmp = self._path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ── 조회 ──────────────────────────────────────────────────────────────

    def get(self, mod_id: str) -> dict | None:
        """레지스트리 항목 반환. 없으면 None."""
        return self._data["mods"].get(mod_id)

    def is_installed(self, mod_id: str) -> bool:
        """레지스트리에 있고 실제 파일도 존재하면 True."""
        info = self.get(mod_id)
        if not info:
            return False
        return os.path.isfile(os.path.join(self._mods_dir, info["filename"]))

    def installed_ids(self) -> list[str]:
        """실제 파일이 존재하는 모드 id 목록."""
        result = []
        for mid, info in list(self._data["mods"].items()):
            if os.path.isfile(os.path.join(self._mods_dir, info["filename"])):
                result.append(mid)
            else:
                # 파일 없으면 레지스트리에서도 정리
                del self._data["mods"][mid]
        self._save()
        return result

    # ── 쓰기 ──────────────────────────────────────────────────────────────

    def record_install(
        self,
        mod_id: str,
        filename: str,
        version: str,
        project_id: str | None = None,
    ):
        self._data["mods"][mod_id] = {
            "filename": filename,
            "version": version,
            "project_id": project_id,
        }
        self._save()

    def record_remove(self, mod_id: str):
        self._data["mods"].pop(mod_id, None)
        self._save()

    def filenames(self) -> set[str]:
        """기록된 모든 항목(카탈로그 모드·의존 모드·번들)의 파일명."""
        return {info["filename"] for info in self._data["mods"].values()
                if info.get("filename")}

    def get_installed_version(self, mod_id: str) -> str | None:
        info = self.get(mod_id)
        return info["version"] if info else None

    def get_installed_filename(self, mod_id: str) -> str | None:
        info = self.get(mod_id)
        return info["filename"] if info else None

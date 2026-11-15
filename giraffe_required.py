"""기린월드 26.2 공식 필수 파일 설치·복구.

공식 꾸러미(2026-11-15)의 HTTPS 배포 주소와 SHA-256을 고정해 사용한다.
필수 JAR를 런처에 재배포하지 않고 기린월드/Modrinth 원본에서 직접 받는다.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import urllib.parse
import urllib.request

import instance


USER_AGENT = "Yoonseul_Launcher/2.7.2-test"
ALLOWED_HOSTS = {"dl.giraffemc.com", "cdn.modrinth.com"}

# path, url, sha256, registry id
REQUIRED_FILES = (
    ("mods/PlayerAnimationLibMerged-1.2.6+mc.26.2.jar",
     "https://dl.giraffemc.com/mods-dist/mods/PlayerAnimationLibMerged-1.2.6%2Bmc.26.2.jar?v=97af3288",
     "97af3288dd39643bf3607bf9ff5c3293022793559236586a6783fe71dd66e6bd",
     "giraffe-player-animation-lib"),
    ("mods/emotecraft-for-MC26.2-3.4.0-b.build.165.jar",
     "https://dl.giraffemc.com/mods-dist/mods/emotecraft-for-MC26.2-3.4.0-b.build.165.jar?v=0e2052d3",
     "0e2052d393909b94523bde06d44aae1ce80456179107cc29feca9f0028267da2",
     "giraffe-emotecraft"),
    ("mods/giraffe-assets-1.0.0.jar",
     "https://dl.giraffemc.com/mods-dist/mods/giraffe-assets-1.0.0.jar?v=b8f3efe0",
     "b8f3efe019c6d9626d7cef51c87ee3b60398846abd23602947cd4621f234fb22",
     "giraffe-assets"),
    ("mods/giraffe-clientinfo-1.19.11.jar",
     "https://dl.giraffemc.com/mods-dist/mods-fabric/giraffe-clientinfo-1.19.11.jar?v=d8117378",
     "d8117378aef454abeb5133dee9fa0c2bc07a9dee4f7ae3325e290c575a28efba",
     "giraffe-clientinfo"),
    ("mods/fabric-api-0.161.0+26.2.jar",
     "https://cdn.modrinth.com/data/P7dR8mSH/versions/ewUK83HI/fabric-api-0.161.0%2B26.2.jar",
     "e5b858ceb13290c274e31cb888ff5a1a40cb91067e7ffab0b5b775aec51e216a",
     "fabric-api"),
    ("mods/mcef-modern-0.3.3+mc26.2.jcef146.0.10.jar",
     "https://cdn.modrinth.com/data/YJFfu5pH/versions/FOXbUPNd/mcef-modern-0.3.3%2Bmc26.2.jcef146.0.10.jar",
     "d3399cee2110a77ad44db2f2d999c392a6203620439982e4f62d1cfb08870df9",
     "giraffe-mcef-modern"),
    ("config/giraffe-updater.json",
     "https://dl.giraffemc.com/mods-dist/config/giraffe-updater.json?v=db6bc8e8",
     "db6bc8e8361e2a49724b9eaa768cc3d87b0cde9b81407ab7d23566afe414da43",
     None),
)

REMOVE_FILES = (
    "mods/koreanpatch-fabric-1.9.6+mc1.21.11.jar",
    "mods/koreanpatch-fabric-1.9.6+mc1.21.10.jar",
    "mods/koreanify-fabric-0.1.11+mc26.1.2.jar",
)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_url(url: str):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("허용되지 않은 기린월드 필수 파일 주소입니다.")


def _download(url: str, target: str, expected_sha256: str,
              on_bytes=None):
    _safe_url(url)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".download"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=90, context=ctx) as resp, \
                open(tmp, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
        if _sha256(tmp) != expected_sha256:
            raise ValueError("필수 파일의 무결성 확인에 실패했습니다.")
        os.replace(tmp, target)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def ensure(on_progress=None, registry=None) -> dict:
    """누락·손상된 공식 필수 파일만 다운로드하고 구형 충돌 파일을 제거한다."""
    root = instance.ensure_dirs()
    total = len(REQUIRED_FILES)
    changed = []

    for index, (rel, url, digest, mod_id) in enumerate(REQUIRED_FILES, 1):
        target = os.path.abspath(os.path.join(root, *rel.split("/")))
        if os.path.commonpath((os.path.abspath(root), target)) != os.path.abspath(root):
            raise ValueError("안전하지 않은 필수 파일 경로입니다.")
        valid = os.path.isfile(target) and _sha256(target) == digest
        if on_progress:
            pct = int((index - 1) / total * 100)
            on_progress(pct, f"기린월드 필수 파일 확인 중 ({index}/{total})",
                        {"stage": "필수 모드", "stage_no": index,
                         "stage_total": total, "cur": index - 1,
                         "max": total, "raw": "giraffe_required"})
        if not valid:
            _download(url, target, digest)
            changed.append(rel)
        if registry is not None and mod_id:
            registry.record_install(mod_id, os.path.basename(target),
                                    "official-2026-11-15", None)

    removed = []
    for rel in REMOVE_FILES:
        path = os.path.join(root, *rel.split("/"))
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed.append(rel)
        except OSError:
            pass

    if on_progress:
        on_progress(100, "기린월드 필수 파일 준비 완료",
                    {"stage": "필수 모드", "stage_no": total,
                     "stage_total": total, "cur": total, "max": total,
                     "raw": "giraffe_required_done"})
    return {"ok": True, "changed": changed, "removed": removed}

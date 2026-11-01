"""앱 메타 정보 — 버전, 작성자, 게임 버전 등 공용 상수.

앱 전역에서 공유하는 상수를
tkinter 파일을 쓰지 않으므로 여기로 분리해 백엔드/브릿지가 공유한다.
"""

APP_NAME = "윤슬 런처"
APP_NAME_EN = "Yoonseul_Launcher"
APP_VERSION = "2.6.5"
# 릴리스 채널: "beta" 면 UI 에 베타 표식을 붙인다. 정식 배포 시 "release" 로.
APP_CHANNEL = "release"

# 약관 버전. 내용이 실질적으로 바뀌면 올린다 → 사용자에게 재동의를 받는다.
TERMS_VERSION = "0.1"
# 정식 배포 전 윤슬 런처 저장소 주소로 교체하세요.
_REPO = "https://github.com/SEA-S2/Yoonseul_Launcher"
# blob/HEAD 는 GitHub 가 기본 브랜치(main/master)로 알아서 해석한다.
TERMS_URL = f"{_REPO}/blob/HEAD/TERMS.md" if _REPO else ""
NOTICES_URL = f"{_REPO}/blob/HEAD/THIRD_PARTY_NOTICES.md" if _REPO else ""
# 앱 자동 업데이트 확인용 GitHub 저장소. launcher_config.json 값이 우선하며,
# 그 파일이 없거나 비어 있을 때 아래 값을 사용합니다.
GITHUB_UPDATE_OWNER = "SEA-S2"
GITHUB_UPDATE_REPO = "Yoonseul_Launcher"
APP_AUTHOR = "윤슬마을"
# HKCU\...\Run 값 이름 (Windows 시작 프로그램 등록 — autostart.py).
# 제품마다 달라야 서로의 등록을 덮어쓰지 않는다.
STARTUP_REG_NAME = "YoonseulLauncher"
GAME_VERSION = "26.2"
LOADER = "fabric"

# 윤슬마을 서버 (정식 배포 전에 실제 주소로 교체)
# 빈 문자열이면 서버 상태 조회와 자동 접속을 사용하지 않습니다.
SERVER_HOST = ""
SERVER_PORT = 25565

# 허용 모드 원격 목록. 운영자가 관리 도구로 고치면
# 런처 재시작만으로 반영된다(공개 읽기, 개인정보 전송 없음).
MOD_POLICY_URL = ""
MOD_POLICY_SERVER_ID = "yoonseul"

# v3: Microsoft 로그인용 Azure 앱 Application(client) ID.
# 비밀값 아님(공개 가능). Azure Portal 등록 + Mojang 승인 폼
# (aka.ms/mce-reviewappid) 완료 후 실제 값으로 교체할 것. — auth.py 참고
# 개발 중에는 원본 앱 ID를 재사용하지 않습니다. 본인 Azure 앱 등록 후 입력하세요.
AZURE_CLIENT_ID = "0e66b823-3cc7-42fa-8b47-568f18a02da0"

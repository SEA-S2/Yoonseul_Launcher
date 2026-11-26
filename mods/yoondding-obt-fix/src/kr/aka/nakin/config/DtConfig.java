package kr.aka.nakin.config;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 모드 설정. 저장은 자체 Gson(config/nakin/config.json) — YACL 는 화면 빌드에만 사용.
 * 따라서 YACL 이 없어도 설정 로드/저장은 정상 동작.
 */
public final class DtConfig {
    private static final Logger LOG = LoggerFactory.getLogger("nakin");
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();

    // ── 진단/소스 ──
    public boolean debugProbe = false;           // STEP1 진단 로거 — 잔고 소스 판정 완료(2026-07-20), 배포 기본 OFF
    public String balanceSourceMode = "AUTO";    // AUTO/SCOREBOARD/BOSSBAR/TABLIST/ACTIONBAR
    public String balanceMarker = "쿠키";           // 이 단어가 든 라인의 숫자를 우선 채택(동글랜드 우하단 패널 "쿠키 2,273,762")
    public String balanceRegex = "([0-9][0-9,]*)"; // 금액 토큰(잔고 100쿠키 미만도 읽도록 1자리부터)

    /**
     * 윤슬 서버의 재화 단위와 메시지 형식이 확정되기 전까지 자동 감지를 막는 안전 스위치.
     * false 여도 정산 창, 수동 기록, 통계, 내보내기와 정산문 복사는 사용할 수 있다.
     */
    public boolean automaticCurrencyTracking = false;
    public int girinCurrencyRulesVersion = 0;

    // ── 결합/회계 ──
    public long matchWindowMs = 1500;            // 메시지 ↔ ΔG 결합 시간창
    public boolean transferExcludedFromPnl = true; // 이체는 손익 제외
    public boolean feeCountedAsExpense = true;     // 수수료를 지출에 포함
    public boolean showTransfers = true;         // 정산에 이체(참고) 표시
    public int dayResetHour = 0;                 // 하루 리셋 시각(0~23)

    /**
     * 금액을 못 알아낸 거래를 "미확인"으로 내역에 남길지. 끄면 예전처럼 조용히 버린다.
     * 켜두는 걸 권장 — 빠진 걸 유저가 모르고 지나가는 게 가장 나쁘다(2026-08-13 제보).
     */
    public boolean recordUnresolved = true;

    /**
     * 내 마을 금고 입금·마을 은행 인출을 이체(손익 제외)로 볼지. 기본 false = 지출/수입.
     * 세금·업그레이드 적립처럼 "내 지갑에서 나가 돌아오지 않는 돈"이라 기본은 지출로 센다.
     * 마을 돈을 개인 손익과 분리해 보고 싶으면 켠다(사용자 판단 보류, 2026-09-28).
     */
    public boolean villageVaultAsTransfer = false;

    // ── 업데이트 알림 ──
    /** 새 버전 확인 여부. 끄면 네트워크 요청을 아예 안 한다. */
    public boolean updateCheckEnabled = false;
    /**
     * 버전 정보 JSON 주소. 비워두면 확인하지 않는다.
     * 형식: {"latest":"0.2.2","url":"다운로드 안내 주소","notes":"한 줄 요약"}
     *
     * <p>새 버전을 낼 때는 이 파일의 latest 만 고치면 된다(모드 재배포 불필요).
     * 공개 저장소라 로그인 없이 읽히며, 모드는 GET 한 번만 하고 아무것도 보내지 않는다.
     */
    public String lastSeenWhatsNew = "";         // 「새로워진 점」 카드를 닫은 버전

    public String updateCheckUrl = "";

    // ── HUD ──
    public boolean hudEnabled = true;
    /**
     * 구버전 절대 픽셀 좌표. 이제는 hudXRatio/hudYRatio 가 진짜 값이고 이건 호환용으로만 남는다.
     * 기본값은 960x540(1080p·GUI 크기 2) 기준 <b>왼쪽 가장자리, 높이 30%</b> — 미니맵 바로 아래.
     * 동글랜드는 오른쪽 아래에 서버 잔고 패널(쿠키·포인트·동글머니)이 있어 흔한 우하단 기본값을
     * 쓰면 그 패널을 가린다(2026-09-28 사용자 스크린샷). 첫 화면에서 비율로 승격되므로
     * GUI 크기가 달라도 같은 자리(좌측 30%)에 뜬다.
     */
    public int hudX = 5;
    public int hudY = 162;

    /**
     * HUD 위치를 화면 크기 대비 비율로 저장(0.0~1.0). GUI 크기·해상도가 바뀌어도
     * 화면상 같은 자리에 남는다. 음수면 "아직 승격 안 됨"(구버전 설정) → 화면 크기를 아는
     * 첫 순간에 hudX/hudY 를 그 화면 기준으로 환산해 채운다(ensureHudRatio).
     */
    public double hudXRatio = -1;
    public double hudYRatio = -1;

    /** 비율이 아직 없으면 현재 화면 기준으로 1회 승격. @return 승격이 일어났으면 true(저장 필요) */
    public boolean ensureHudRatio(int screenW, int screenH) {
        if (screenW <= 0 || screenH <= 0) return false;
        boolean changed = false;
        if (!isRatioSet(hudXRatio)) {
            hudXRatio = clampRatio((double) hudX / screenW);
            changed = true;
        }
        if (!isRatioSet(hudYRatio)) {
            hudYRatio = clampRatio((double) hudY / screenH);
            changed = true;
        }
        return changed;
    }

    /** 비율 → 픽셀. 승격 전이면 예전 픽셀값을 그대로 쓴다. */
    public int hudPixelX(int screenW) {
        return isRatioSet(hudXRatio) ? pixelFromRatio(hudXRatio, screenW) : hudX;
    }

    public int hudPixelY(int screenH) {
        return isRatioSet(hudYRatio) ? pixelFromRatio(hudYRatio, screenH) : hudY;
    }

    /** 편집 화면에서 위치가 바뀔 때 — 픽셀과 비율을 함께 갱신한다. */
    public void setHudPixel(int x, int y, int screenW, int screenH) {
        hudX = x;
        hudY = y;
        if (screenW > 0) hudXRatio = clampRatio((double) x / screenW);
        if (screenH > 0) hudYRatio = clampRatio((double) y / screenH);
    }

    private static boolean isRatioSet(double r) {
        return Double.isFinite(r) && r >= 0;
    }

    public static double clampRatio(double r) {
        return !Double.isFinite(r) ? 0 : Math.max(0, Math.min(1, r));
    }

    public static int pixelFromRatio(double ratio, int screenSize) {
        return screenSize <= 0 ? 0 : (int) Math.round(clampRatio(ratio) * screenSize);
    }

    /** HUD 크기 배율. 편집 화면에서 휠/버튼으로 조절(0.5~2.0). */
    public float hudScale = 1.0f;

    public static final float HUD_SCALE_MIN = 0.5f;
    public static final float HUD_SCALE_MAX = 2.0f;
    public static final float HUD_SCALE_STEP = 0.05f;

    /** 설정값이 손상돼도 안전한 범위로 보정한 배율. */
    public float hudScaleClamped() {
        if (!(hudScale > 0)) return 1.0f; // 0·음수·NaN 방어
        return Math.max(HUD_SCALE_MIN, Math.min(HUD_SCALE_MAX, hudScale));
    }
    public float hudOpacity = 1.0f; // 0.2~1.0 — HUD 패널 전체(배경+글자) 불투명도

    /** HUD 카테고리 행 수. -1=전체, 0=숨김, 1~10=표시 개수. */
    public int hudIncomeRows = 4;
    public int hudExpenseRows = 0;

    // ── 플리마켓 ──
    public String fleaMarketMode = "AUTO";
    public double fleaSaleFeePercent = 3.0;
    /** 기린월드 위키 기준 설정 마이그레이션 버전. 구 설정 파일에는 없으므로 0으로 읽힌다. */
    public int girinWikiSettingsVersion = 0;

    // ── 기린월드 서버 연동 ──
    // 서버의 실제 GUI·채팅 문구로 검증하기 전까지 모두 OFF. 일반 장부 기능에는 영향이 없다.
    public boolean girinQuestTracking = false;


    private transient Path path;

    /** @param dir 모드 설정 디렉터리(예: <configDir>/nakin) — 각 로더 진입점이 주입 */
    public static DtConfig load(Path dir) {
        Path p = dir.resolve("config.json");
        DtConfig cfg;
        try {
            if (Files.exists(p)) {
                cfg = GSON.fromJson(Files.readString(p), DtConfig.class);
                if (cfg == null) cfg = new DtConfig();
            } else {
                cfg = new DtConfig();
            }
        } catch (Exception e) {
            LOG.warn("[nakin] 설정 로드 실패, 기본값 사용", e);
            cfg = new DtConfig();
        }
        cfg.path = p;
        cfg.normalizeAfterLoad();
        cfg.save();
        return cfg;
    }

    /**
     * 역직렬화 직후 수치 필드를 유효 범위로 보정한다.
     *
     * <p>설정 화면(GUI)은 잘못된 값을 넣지 못하게 막지만, 유저가 config.json 을 직접 편집하면
     * 방어할 곳이 없다(예: dayResetHour=99). hudScale·HUD 비율은 사용 시점마다 clamp 되지만
     * 나머지 수치는 그대로 쓰이므로 여기서 한 번 정리한다. 각 범위는 필드 주석에 적힌 값과 같다.
     */
    void normalizeAfterLoad() {
        // First upgrade from the pre-OBT disabled default; later user opt-out is preserved.
        if (girinCurrencyRulesVersion < 1) {
            automaticCurrencyTracking = true;
            girinCurrencyRulesVersion = 1;
        }
        // 0.2.3 초기 배포본의 기본값 5%를 기린월드 위키의 기본 수수료 3%로 교정한다.
        if (girinWikiSettingsVersion < 1) {
            if (Double.compare(fleaSaleFeePercent, 5.0) == 0) fleaSaleFeePercent = 3.0;
            girinWikiSettingsVersion = 1;
        }
        dayResetHour = clampInt(dayResetHour, 0, 23);
        hudOpacity = clampFloat(hudOpacity, 0.2f, 1.0f, 1.0f);
        // 무한대·NaN 이 남으면 곧바로 이어지는 save() 에서 Gson 이 예외를 던진다(테스트로 발견)
        hudScale = clampFloat(hudScale, HUD_SCALE_MIN, HUD_SCALE_MAX, 1.0f);
        matchWindowMs = clampLong(matchWindowMs, 200, 60_000);
        hudIncomeRows = clampInt(hudIncomeRows, -1, 10);
        hudExpenseRows = clampInt(hudExpenseRows, -1, 10);
        // 설정 화면의 선택지와 동일하게 1·2·3%만 허용한다.
        fleaSaleFeePercent = Math.round(clampDouble(fleaSaleFeePercent, 1, 3, 3));
        hudXRatio = isRatioSet(hudXRatio) ? clampRatio(hudXRatio) : -1;
        hudYRatio = isRatioSet(hudYRatio) ? clampRatio(hudYRatio) : -1;
    }

    private static int clampInt(int v, int min, int max) {
        return v < min ? min : v > max ? max : v;
    }

    private static long clampLong(long v, long min, long max) {
        return v < min ? min : v > max ? max : v;
    }

    /** 유한하고 범위 안이면 그대로, 아니면 보정(손상된 NaN/무한대는 기본값). */
    private static float clampFloat(float v, float min, float max, float def) {
        if (!Float.isFinite(v)) return def;
        return v < min ? min : v > max ? max : v;
    }

    private static double clampDouble(double v, double min, double max, double def) {
        if (!Double.isFinite(v)) return def;
        return v < min ? min : v > max ? max : v;
    }

    public void save() {
        if (path == null) return; // load() 전 호출 방지
        try {
            Files.createDirectories(path.getParent());
            // 임시 파일에 먼저 쓰고 원자적으로 교체 — Files.writeString 은 기존 파일을 먼저
            // 0바이트로 자르므로, 쓰는 도중 게임이 죽으면 설정이 통째로 날아간다. 저장은
            // load() 마다·HUD 이동·금고 변동마다 일어나 빈도가 높다. (LedgerStore.writeFile 과 동형)
            Path tmp = path.resolveSibling(path.getFileName() + ".tmp");
            Files.writeString(tmp, GSON.toJson(this));
            try {
                Files.move(tmp, path, java.nio.file.StandardCopyOption.REPLACE_EXISTING,
                        java.nio.file.StandardCopyOption.ATOMIC_MOVE);
            } catch (java.nio.file.AtomicMoveNotSupportedException e) {
                // 일부 환경(네트워크 드라이브·OneDrive 등)은 원자적 이동을 지원하지 않는다
                Files.move(tmp, path, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            LOG.warn("[nakin] 설정 저장 실패", e);
        }
    }
}

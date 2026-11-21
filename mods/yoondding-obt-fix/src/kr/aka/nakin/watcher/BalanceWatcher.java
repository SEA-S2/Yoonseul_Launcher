package kr.aka.nakin.watcher;

import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.util.BalanceExtractor;
import kr.aka.nakin.util.GoldFormat;
import net.minecraft.client.Minecraft;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.function.LongConsumer;

/**
 * 매 틱 현재 골드값을 읽어 직전값과 다르면 ΔG 이벤트를 발행.
 * STEP1(BalanceProbe)에서 확정된 소스를 config.balanceSourceMode 로 선택.
 */
public final class BalanceWatcher {
    private static final Logger LOG = LoggerFactory.getLogger("nakin");

    private final DtConfig config;
    private final LongConsumer deltaListener;
    private final ScoreboardBalanceSource scoreboard = new ScoreboardBalanceSource();
    private final BossBarBalanceSource bossbar = new BossBarBalanceSource();
    private final TabListBalanceSource tablist = new TabListBalanceSource();

    // 새 값이 이만큼 연속 같아야 확정(노이즈 억제).
    // 2026-08-11: 깜빡임을 더 거르겠다고 5로 올렸다가 ΔG 가 아예 안 잡혀 되돌림.
    //   잔고 소스는 화면 최대 숫자라 틱마다 값이 흔들릴 수 있어, 요구 틱을 올리면
    //   후보가 매번 초기화돼 영영 확정되지 않는다. 검증 없이 건드리지 말 것.
    private static final int CONFIRM_TICKS = 2;

    /**
     * 지금 잔고를 얼마로 읽고 있는지(진단용, /낙인 진단).
     * "ΔG 가 안 잡힌다"가 <b>못 읽는 것</b>인지 <b>값이 안 변하는 것</b>인지 구분하려면 이게 필요하다.
     */
    private static volatile String lastReadInfo;

    public static String lastReadInfo() { return lastReadInfo; }

    /**
     * 잔고 후보로 <b>모드가 실제로 읽을 수 있는 모든 줄</b>(소스별)을 덤프한다(진단용).
     * "진짜 잔고가 후보에 있긴 한가"를 눈으로 확인하려는 것 — 없으면 텍스트 추적 자체가 불가.
     */
    private static volatile String lastCandidateDump;

    public static String lastCandidateDump() { return lastCandidateDump; }

    private static final int SETTLE_TICKS = 40;   // 월드 전환 후 이만큼 기준선만 따라감(로드 중 오탐 방지)

    private final BalanceExtractor extractor = new BalanceExtractor();
    private Long lastBalance = null;
    /** 연속으로 잔고를 못 읽은 틱 수 — 길어지면 대조를 "확인 불가"로(옛 값으로 '일치'라고 하지 않게). */
    private int failTicks = 0;
    private static final int STALE_TICKS = 40;
    /** 잔고로 잡은 줄이 가짜로 판명됐을 때(대기 ΔG 무효화 등). */
    private Runnable onRejected = () -> { };

    public void setOnRejected(Runnable r) {
        if (r != null) onRejected = r;
    }
    private Long candidate = null;
    private int candidateTicks = 0;
    private int settleTicks = 0;
    private Object lastWorld = null; // ClientWorld 식별 — 월드/서버 전환 감지(교차월드 ΔG 오탐 방지)
    private volatile String lastActionBar = "";

    public BalanceWatcher(DtConfig config, LongConsumer deltaListener) {
        this.config = config;
        this.deltaListener = deltaListener;
    }

    /** 액션바 텍스트 캡처(외부 GAME overlay 리스너가 호출). */
    public void captureActionBar(String text) {
        this.lastActionBar = text == null ? "" : text;
    }

    /** 확정된 현재 잔고. 아직 못 읽었거나 월드 이동 직후 정착 중이면 null. */
    public Long confirmedBalance() {
        return settleTicks > 0 || failTicks >= STALE_TICKS ? null : lastBalance;
    }

    /** 재접속 — 기준선을 버리고, 지난 접속에서 잔고가 아니라고 판정한 줄 모양도 잊는다. */
    public void newSession() {
        reset();
        extractor.forgetRejected();
    }

    /** 현재 잔고 강제 리셋(재접속 등). 다음 변동부터 다시 추적. */
    public void reset() {
        this.lastBalance = null;
        this.candidate = null;
        this.candidateTicks = 0;
        this.extractor.unlock();   // 서버가 바뀌면 골드 표시 모양도 달라질 수 있다
    }

    public void tick(Minecraft client, long now) {
        if (client.player == null || client.level == null) return;

        // 월드/서버 전환(스폰↔마을 등)이면 잔고 기준선 리셋 후 정착 구간 시작 — 이동은 거래가 아님.
        if (client.level != lastWorld) {
            lastWorld = client.level;
            reset();
            settleTicks = SETTLE_TICKS;
            return;
        }

        Long bal = readBalance(client);
        if (bal == null) failTicks++;
        else failTicks = 0;
        if (bal == null) {
            lastReadInfo = "§c읽기 실패(화면에서 금액을 못 찾음)";
            return;
        }
        lastReadInfo = "확정 " + GoldFormat.format(lastBalance == null ? bal : lastBalance)
                + (candidate != null ? " · 후보 " + GoldFormat.format(candidate) + " (" + candidateTicks + "틱)" : "")
                + " · 방금 읽음 " + GoldFormat.format(bal)
                + (extractor.lockedShape() != null ? " · 추적중인 줄 [" + readable(extractor.lockedShape()) + "]" : "");

        // 전환 직후 정착 구간: 로드 중 튀는 값에 속지 않게 기준선만 따라감(ΔG 미발행).
        if (settleTicks > 0) {
            settleTicks--;
            lastBalance = bal;
            candidate = null;
            candidateTicks = 0;
            return;
        }

        if (lastBalance == null) {
            lastBalance = bal;
            return;
        }
        if (bal.equals(lastBalance)) {
            candidate = null;
            candidateTicks = 0;
            return;
        }
        // 변동 감지 — 새 값이 CONFIRM_TICKS 연속 유지될 때만 확정(순간 노이즈 배제)
        if (bal.equals(candidate)) {
            if (++candidateTicks >= CONFIRM_TICKS) {
                long delta = bal - lastBalance;
                lastBalance = bal;
                candidate = null;
                candidateTicks = 0;
                if (extractor.noteDelta(delta, now)) {
                    LOG.info("[nakin] 잔고로 잡은 줄이 작은 값만 오르내림 — 잔고가 아닌 줄로 보고 다시 찾습니다");
                    lastBalance = null;
                    kr.aka.nakin.core.WalletCheck.LIVE.reset(); // 엉뚱한 줄로 잡은 기준선은 버린다
                    onRejected.run();
                    return;
                }
                LOG.info("[nakin] ΔG = {} (now={})", GoldFormat.signed(delta), GoldFormat.format(bal));
                deltaListener.accept(delta);
            }
        } else {
            candidate = bal;
            candidateTicks = 1;
        }
    }

    private Long readBalance(Minecraft client) {
        List<String> lines = new ArrayList<>();
        String mode = config.balanceSourceMode == null ? "AUTO" : config.balanceSourceMode.toUpperCase();
        StringBuilder dump = new StringBuilder();
        switch (mode) {
            case "SCOREBOARD" -> collect(dump, "스코어보드", scoreboard.lines(client), lines);
            case "BOSSBAR" -> collect(dump, "보스바", bossbar.lines(client), lines);
            case "TABLIST" -> collect(dump, "탭리스트", tablist.lines(client), lines);
            case "ACTIONBAR" -> collect(dump, "액션바", List.of(lastActionBar), lines);
            default -> { // AUTO
                collect(dump, "스코어보드", scoreboard.lines(client), lines);
                collect(dump, "보스바", bossbar.lines(client), lines);
                collect(dump, "탭리스트", tablist.lines(client), lines);
                collect(dump, "액션바", List.of(lastActionBar), lines);
            }
        }
        lastCandidateDump = dump.length() == 0 ? "§c읽을 수 있는 줄이 하나도 없음" : dump.toString();
        return extractor.extract(kr.aka.nakin.core.BalanceLines.labelled(lines, config.balanceMarker), config.balanceMarker, config.balanceRegex);
    }

    /** 한 소스의 줄들을 진단 덤프에 소스 라벨과 함께 쌓고, 추출 대상 목록에도 넣는다. */
    private static void collect(StringBuilder dump, String label, List<String> src, List<String> into) {
        for (String line : src) {
            if (line == null || line.isEmpty()) continue;
            into.add(line);
            dump.append("§8[").append(label).append("] §7").append(readable(line)).append('\n');
        }
    }

    /** 진단 표시용 — 리소스팩 아이콘 글리프(사용자 영역 문자)는 채팅에서 깨진 글자로 보이므로 뺀다. */
    private static String readable(String s) {
        return s.replaceAll("[\\uE000-\\uF8FF\\x{A0000}-\\x{10FFFF}]+", " ").replaceAll(" {2,}", " ").strip();
    }
}

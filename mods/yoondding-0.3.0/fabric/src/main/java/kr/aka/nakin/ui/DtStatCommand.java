package kr.aka.nakin.ui;

import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.builder.LiteralArgumentBuilder;
import kr.aka.nakin.aggregate.DailyAggregator;
import kr.aka.nakin.aggregate.DailyBucket;
import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.config.DtConfigScreen;
import kr.aka.nakin.core.TransactionRecord;
import kr.aka.nakin.export.LedgerExport;
import kr.aka.nakin.store.LedgerStore;
import kr.aka.nakin.util.GoldFormat;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandRegistrationCallback;
import net.fabricmc.fabric.api.client.command.v2.FabricClientCommandSource;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.YearMonth;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import static net.fabricmc.fabric.api.client.command.v2.ClientCommands.argument;
import static net.fabricmc.fabric.api.client.command.v2.ClientCommands.literal;

/**
 * /낙인 명령어. 모든 출력은 로컬 채팅 피드백(서버 전송 없음).
 *
 * <p>모든 이름은 <b>한글이 기본, 영문이 별칭</b>이다. 영문을 남기는 이유는 두 가지 —
 * 기존 사용자의 손버릇과 공지·설명서가 그대로 살아 있어야 하고, 무엇보다
 * <b>마인크래프트 채팅창에서 한글 IME 가 안 먹는 환경</b>이 있어서 한글만 남기면
 * 그 사람들은 명령어를 아예 못 쓰게 된다.
 */
public final class DtStatCommand {

    private final DailyAggregator aggregator;
    private final DtConfig config;
    private final LedgerStore store;
    private final LedgerHud hud;
    private final java.util.function.Consumer<TransactionRecord> sink;

    public DtStatCommand(DailyAggregator aggregator, DtConfig config, LedgerStore store,
                         LedgerHud hud,
                         java.util.function.Consumer<TransactionRecord> sink) {
        this.aggregator = aggregator;
        this.config = config;
        this.store = store;
        this.hud = hud;
        this.sink = sink;
    }

    public void register() {
        ClientCommandRegistrationCallback.EVENT.register((dispatcher, registryAccess) -> {
            dispatcher.register(tree("윤띵"));
            dispatcher.register(tree("낙인"));
            dispatcher.register(tree("nakin"));    // 한글 IME 가 안 먹는 환경용 영문 별칭
        });
    }

    /** 같은 하위 명령 묶음을 이름만 바꿔 두 번 만든다(낙인 / nakin). */
    private LiteralArgumentBuilder<FabricClientCommandSource> tree(String name) {
        LiteralArgumentBuilder<FabricClientCommandSource> root = literal(name).executes(ctx -> openUi());
        both(root, "창", "ui", b -> b.executes(ctx -> openUi()));
        both(root, "오늘", "today", b -> b.executes(ctx -> printDay(ctx.getSource(), aggregator.today())));
        both(root, "주간", "week", b -> b.executes(ctx -> printWeek(ctx.getSource())));
        both(root, "날짜", "day", b -> b.then(argument("date", StringArgumentType.string())
                .executes(ctx -> printSpecificDay(ctx.getSource(), StringArgumentType.getString(ctx, "date")))));
        both(root, "미분류", "pending", b -> b.executes(ctx -> printPending(ctx.getSource())));
        both(root, "진단", "gui", b -> b.executes(ctx -> printGuiSnapshot(ctx.getSource())));
        both(root, "내보내기", "export", b -> b.executes(ctx -> exportCsv(ctx.getSource(), ""))
                .then(argument("범위", StringArgumentType.string())
                        .executes(ctx -> exportCsv(ctx.getSource(), StringArgumentType.getString(ctx, "범위")))));
        both(root, "설정", "config", b -> b.executes(ctx -> openConfig(ctx.getSource())));
        both(root, "표시", "hud", b -> b.executes(ctx -> toggleHud(ctx.getSource())));
        both(root, "위치", "move", b -> b.executes(ctx -> openHudEdit()));
        both(root, "초기화", "reset", b -> b.executes(ctx -> resetToday(ctx.getSource())));
        both(root, "추가", "add", b -> {
            both(b, "수입", "income", c -> c.then(argument("args", StringArgumentType.greedyString())
                    .executes(ctx -> addManual(ctx.getSource(), TransactionRecord.Kind.INCOME,
                            StringArgumentType.getString(ctx, "args")))));
            both(b, "지출", "expense", c -> c.then(argument("args", StringArgumentType.greedyString())
                    .executes(ctx -> addManual(ctx.getSource(), TransactionRecord.Kind.EXPENSE,
                            StringArgumentType.getString(ctx, "args")))));
        });
        both(root, "업데이트", "update", b -> b.executes(ctx -> openUpdateScreen()));
        both(root, "테스트", "test", b -> b.executes(ctx -> openTestScreen(ctx.getSource())));
        both(root, "제보", "report", b -> b.executes(ctx -> report(ctx.getSource())));
        both(root, "건의", "feedback", b -> b.executes(ctx -> report(ctx.getSource())));
        return root;
    }

    /**
     * /낙인 제보 — 최근 채팅·잔고 변동·기록을 평문으로 클립보드에 넣고 제보 폼 링크를 보여준다.
     * 모드는 아무것도 보내지 않는다. 폼을 열지, 붙여넣을지는 사용자가 정한다.
     */
    private int report(FabricClientCommandSource src) {
        send(src, copyReport());
        String url = kr.aka.nakin.core.ActivityLog.REPORT_FORM_URL;
        src.sendFeedback(ChatText.of("§b§n » 건의·버그 제보 폼 열기 (여기 클릭)")
                .withStyle(st -> st.withClickEvent(new net.minecraft.network.chat.ClickEvent.OpenUrl(
                        java.net.URI.create(url)))));
        return 1;
    }

    static String copyReport() {
        String ver = net.fabricmc.loader.api.FabricLoader.getInstance()
                .getModContainer("yoonseul_ledger")
                .map(c -> c.getMetadata().getVersion().getFriendlyString())
                .orElse("?");
        String text = kr.aka.nakin.core.ActivityLog.report(kr.aka.nakin.core.ActivityLog.header(
                ver, kr.aka.nakin.watcher.BalanceWatcher.lastReadInfo()));
        Minecraft.getInstance().keyboardHandler.setClipboard(text);
        return "§a제보용 기록을 클립보드에 복사했어요. §7제보 폼의 '관련 채팅 문구' 칸에 Ctrl+V 로 붙여 주세요.";
    }

    /**
     * /낙인 업데이트 — 업데이트 동의 화면. 다운로드는 화면의 "동의 후 다운로드" 버튼에서만 시작된다.
     * 자동 설치를 못 쓰는 환경이어도 화면은 열고, 상태 줄에 사유를 보여준다.
     */
    private int openUpdateScreen() {
        Minecraft mc = Minecraft.getInstance();
        String current = net.fabricmc.loader.api.FabricLoader.getInstance()
                .getModContainer("yoonseul_ledger")
                .map(m -> m.getMetadata().getVersion().getFriendlyString())
                .orElse("0");
        mc.schedule(() -> mc.gui.setScreen(new ModUpdateScreen(
                kr.aka.nakin.update.UpdateInstaller.get(), current,
                kr.aka.nakin.update.UpdateInstaller.unavailableReason())));
        return 1;
    }

    /** /낙인 테스트 — 싱글플레이 전용 동작 확인 창. 실서버에서는 열지 않는다. */
    private int openTestScreen(FabricClientCommandSource src) {
        Minecraft mc = Minecraft.getInstance();
        if (!mc.hasSingleplayerServer()) {
            send(src, "§c/윤띵 테스트 는 싱글플레이 월드에서만 쓸 수 있습니다.");
            return 0;
        }
        mc.schedule(() -> mc.gui.setScreen(new DtTestScreen(config, aggregator, sink, hud)));
        return 1;
    }

    /** 한글 이름과 영문 이름을 같은 내용으로 나란히 등록한다. */
    private static void both(LiteralArgumentBuilder<FabricClientCommandSource> parent,
                             String ko, String en,
                             Consumer<LiteralArgumentBuilder<FabricClientCommandSource>> body) {
        for (String n : new String[]{ko, en}) {
            LiteralArgumentBuilder<FabricClientCommandSource> b = literal(n);
            body.accept(b);
            parent.then(b);
        }
    }

    private int printDay(FabricClientCommandSource src, DailyBucket b) {
        send(src, "§6=== 윤띵 · " + b.date + " ===");
        send(src, "§a수입 §f" + GoldFormat.format(b.income) + " 냥   §c지출 §f" + GoldFormat.format(b.expense) + " 냥");
        long net = b.netPnl();
        send(src, (net >= 0 ? "§a" : "§c") + "순익 " + GoldFormat.signed(net) + " 냥 §7(거래 " + b.count + "건)");
        printCatMap(src, "§a[수입]", b.incomeByCategory);
        printCatMap(src, "§c[지출]", b.expenseByCategory);
        if (config.showTransfers && (b.transferIn > 0 || b.transferOut > 0)) {
            send(src, "§b[이체·참고] §7유입 " + GoldFormat.format(b.transferIn)
                    + " / 유출 " + GoldFormat.format(b.transferOut) + " §8(손익 제외)");
        }
        return 1;
    }

    private void printCatMap(FabricClientCommandSource src, String header, Map<String, Long> map) {
        if (map.isEmpty()) return;
        StringBuilder sb = new StringBuilder(header + " §f");
        boolean first = true;
        for (Map.Entry<String, Long> e : map.entrySet()) {
            if (!first) sb.append("§7, §f");
            sb.append(e.getKey()).append(" ").append(GoldFormat.format(e.getValue()));
            first = false;
        }
        send(src, sb.toString());
    }

    private int printWeek(FabricClientCommandSource src) {
        List<DailyBucket> days = aggregator.lastDays(7);
        send(src, "§6=== 윤띵 · 최근 7일 ===");
        long totIn = 0, totOut = 0, totNet = 0;
        for (DailyBucket b : days) {
            totIn += b.income;
            totOut += b.expense;
            totNet += b.netPnl();
            send(src, "§7" + b.date + " §a+" + GoldFormat.format(b.income)
                    + " §c-" + GoldFormat.format(b.expense)
                    + " §f= " + (b.netPnl() >= 0 ? "§a" : "§c") + GoldFormat.signed(b.netPnl()));
        }
        send(src, "§6합계 §a수입 " + GoldFormat.format(totIn) + " §c지출 " + GoldFormat.format(totOut)
                + " §f순익 " + (totNet >= 0 ? "§a" : "§c") + GoldFormat.signed(totNet));
        return 1;
    }

    private int printSpecificDay(FabricClientCommandSource src, String dateStr) {
        try {
            LocalDate d = LocalDate.parse(dateStr, DateTimeFormatter.ISO_LOCAL_DATE);
            return printDay(src, aggregator.day(d));
        } catch (Exception e) {
            send(src, "§c날짜 형식 오류. 예: /윤띵 날짜 2026-07-20");
            return 0;
        }
    }

    private int printPending(FabricClientCommandSource src) {
        List<TransactionRecord> p = aggregator.pending();
        send(src, "§6=== 금액 미확인 " + p.size() + "건 ===");
        if (p.isEmpty()) {
            send(src, "§7없음 — 모든 거래의 금액이 확인되었습니다.");
        } else {
            for (TransactionRecord r : p) {
                send(src, "§7• " + r.kind + " " + GoldFormat.format(r.amount) + " §8" + r.note);
            }
        }
        return 1;
    }

    /**
     * 진단 — 버전, 최근 인식한 채팅, 잔고를 어느 줄에서 읽었는지, 최근 잔고 변동.
     * 금액이 이상할 때 이 결과 스크린샷으로 원인(채팅 규칙 / 잔고 소스)을 가른다.
     */
    private int printGuiSnapshot(FabricClientCommandSource src) {
        // 어느 빌드인지 먼저 — "고쳤는데 안 된다"의 상당수가 구버전 jar 였다
        String ver = net.fabricmc.loader.api.FabricLoader.getInstance()
                .getModContainer("yoonseul_ledger")
                .map(c -> c.getMetadata().getVersion().getFriendlyString())
                .orElse("?");
        send(src, "§6윤띵 버전: §f" + ver);

        // 채팅 규칙이 먹었는지 / 창에서 비용을 읽었는지 — 원인 구분용
        String sig = kr.aka.nakin.core.TransactionResolver.lastSignalInfo();
        send(src, sig == null ? "§c최근 인식한 채팅 없음" : "§7최근 채팅 인식: §f" + sig);
        String costs = kr.aka.nakin.core.TransactionResolver.lastGuiCostInfo();
        send(src, costs == null || costs.isBlank()
                ? "§7창에서 읽은 비용 없음 §8(대장간 강화 창을 열면 채워짐)"
                : "§7창에서 읽은 비용: §f" + costs);
        String read = kr.aka.nakin.watcher.BalanceWatcher.lastReadInfo();
        send(src, read == null ? "§c잔고를 아직 한 번도 못 읽음" : "§7잔고 읽기: §f" + read);

        // 잔고 후보 전체 덤프 — 진짜 잔고가 읽히는 줄에 있는지, 어느 줄이 오인됐는지 눈으로 확인.
        String dump = kr.aka.nakin.watcher.BalanceWatcher.lastCandidateDump();
        if (dump != null && !dump.isBlank()) {
            send(src, "§6=== 잔고 후보 줄(소스별) — 진짜 잔고가 여기 있나? ===");
            for (String line : dump.split("\n")) if (!line.isBlank()) send(src, line);
        }
        String delta = kr.aka.nakin.core.TransactionResolver.lastDeltaInfo();
        send(src, delta == null ? "§c감지된 잔고 변동 없음" : "§7최근 잔고 변동: §f" + delta);
        String settle = kr.aka.nakin.core.TransactionResolver.lastSettleInfo();
        if (settle != null) send(src, "§7최근 처리 결과: §f" + settle);

        return 1;
    }

    /**
     * @param scope "" (이번 달) · today · week · 2026-06 — 파일이 커지는 게 부담이면 좁혀 쓴다.
     */
    private int exportCsv(FabricClientCommandSource src, String scope) {
        try {
            LedgerExport.Scope sc = LedgerExport.resolve(store, config.dayResetHour, scope);
            if (sc.records().isEmpty()) {
                send(src, "§e해당 기간에 기록이 없습니다: §f" + sc.baseName());
                return 0;
            }
            Path dir = net.fabricmc.loader.api.FabricLoader.getInstance().getConfigDir()
                    .resolve("yoondding"); // 설정·장부 폴더와 일치
            String base = "윤띵-" + sc.baseName();
            Path xlsx = LedgerExport.writeXlsx(sc.records(), dir.resolve(base + ".xlsx"), config.dayResetHour);
            LedgerExport.writeCsv(sc.records(), dir.resolve(base + ".csv"), config.dayResetHour);
            send(src, "§a내보내기 완료(" + sc.records().size() + "건): §f" + xlsx);
            send(src, "§7시트: 거래내역 · 일별 · 주별 · 카테고리 §8(.csv 도 같은 폴더에)");
        } catch (IllegalArgumentException e) {
            send(src, "§c" + e.getMessage());
            send(src, "§7예: §f/윤띵 내보내기 week§7, §f/윤띵 내보내기 2026-06");
        } catch (Exception e) {
            send(src, "§c내보내기 실패: " + e.getMessage());
        }
        return 1;
    }

    /** 채팅창이 닫힌 다음 틱에 열어야 화면이 덮이지 않음 → mc.send 로 지연. */
    private int openUi() {
        Minecraft mc = Minecraft.getInstance();
        mc.schedule(() -> mc.gui.setScreen(new DtStatScreen(config, aggregator, sink, hud)));
        return 1;
    }

    /** /낙인 위치 — HUD 위치 편집 화면. */
    private int openHudEdit() {
        Minecraft mc = Minecraft.getInstance();
        mc.schedule(() -> mc.gui.setScreen(new DtHudEditScreen(config, hud)));
        return 1;
    }

    private int openConfig(FabricClientCommandSource src) {
        Minecraft mc = Minecraft.getInstance();
        mc.schedule(() -> {
            net.minecraft.client.gui.screens.Screen screen = DtConfigScreen.create(config, aggregator, null);
            if (screen != null) {
                mc.gui.setScreen(screen);
            } else {
                src.sendFeedback(ChatText.of("§cYACL 설정 화면을 열 수 없습니다. config/yoondding/config.json 을 직접 편집하세요."));
            }
        });
        return 1;
    }

    /** /낙인 초기화 — 오늘 데이터 초기화. */
    private int resetToday(FabricClientCommandSource src) {
        int n = aggregator.resetToday();
        send(src, "§a오늘 데이터 초기화 완료 §7(" + n + "건 삭제) — 순익/수입/지출이 0부터 다시 집계됩니다.");
        return 1;
    }

    /** /낙인 추가 수입|지출 &lt;금액&gt; [라벨] — 수동 기록 추가. */
    private int addManual(FabricClientCommandSource src, TransactionRecord.Kind kind, String args) {
        String[] parts = args.trim().split("\\s+", 2);
        String digits = parts[0].replaceAll("[^0-9]", "");
        if (digits.isEmpty() || digits.length() > 15) {
            send(src, "§c형식: /윤띵 추가 " + (kind == TransactionRecord.Kind.INCOME ? "수입" : "지출") + " <금액> [설명]");
            return 0;
        }
        long amt = Long.parseLong(digits);
        String label = parts.length > 1 ? parts[1] : "수동 입력";
        TransactionRecord rec = new TransactionRecord(System.currentTimeMillis(), kind, amt,
                "수동", label, 0, true, TransactionRecord.Confidence.HIGH, false, "수동 입력");
        sink.accept(rec);
        send(src, (kind == TransactionRecord.Kind.INCOME ? "§a+수입 " : "§c-지출 ")
                + GoldFormat.format(amt) + " 냥 §7[" + label + "] 추가됨");
        return 1;
    }

    private int toggleHud(FabricClientCommandSource src) {
        config.hudEnabled = !config.hudEnabled;
        config.save();
        send(src, "§eHUD " + (config.hudEnabled ? "§a켜짐" : "§c꺼짐"));
        return 1;
    }

    private void send(FabricClientCommandSource src, String msg) {
        src.sendFeedback(ChatText.of(msg));
    }
}

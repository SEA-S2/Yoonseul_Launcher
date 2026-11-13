package kr.aka.nakin.ui;

import kr.aka.nakin.aggregate.DailyAggregator;
import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.core.TransactionRecord;
import kr.aka.nakin.util.GoldFormat;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;

import java.util.ArrayDeque;
import java.util.Iterator;
import java.util.List;
import java.util.function.Consumer;

/**
 * /낙인 테스트 — <b>싱글플레이 전용</b> 동작 확인 창.
 *
 * <p>기린월드 위키에서 확인되는 거래 종류를 싱글플레이에서 테스트 기록으로 직접 넣고,
 * 사이드바의 가짜 쿠키 잔고도 함께 바꾼다. 서버가 열리기 전에도 HUD·정산 창 표시를 검증할 수 있다.
 *
 * <p>안전장치:
 * <ul>
 *   <li>싱글플레이가 아니면 어떤 명령도 보내지 않는다.</li>
 *   <li>싱글플레이에서 생긴 기록은 월드를 나갈 때 자동 삭제된다(DtLedgerClient).</li>
 *   <li>명령은 채팅이 아니라 내장 서버에 직접 실행한다 — 치트가 꺼진 월드에서도 된다.</li>
 * </ul>
 */
public final class DtTestScreen extends Screen {

    private static final int W = 360;
    private static final int PAD = 12;
    private static final int ROW = 21;
    private static final int TOP = 14;
    private static final String OBJ = "nakin_test";

    private static long balance = 2_000_000;
    private static String status = "① 먼저 '가짜 잔고 패널 만들기'를 누르세요.";

    private final DtConfig config;
    private final DailyAggregator aggregator;
    private final Consumer<TransactionRecord> sink;
    private final LedgerHud hud;

    private record Scenario(String button, long delta, TransactionRecord.Kind kind,
                            String category, String label) {}

    public DtTestScreen(DtConfig config, DailyAggregator aggregator, Consumer<TransactionRecord> sink, LedgerHud hud) {
        super(Component.literal("윤띵 테스트"));
        this.config = config;
        this.aggregator = aggregator;
        this.sink = sink;
        this.hud = hud;
    }

    private static List<Scenario> scenarios() {
        return List.of(
                new Scenario("무역 상인 납품 +4,590", 4_590, TransactionRecord.Kind.INCOME, "무역", "무역 상인 납품"),
                new Scenario("상점 판매 +12,000", 12_000, TransactionRecord.Kind.INCOME, "상점", "일반 상점 판매"),
                new Scenario("거래소 정산 +23,250", 23_250, TransactionRecord.Kind.INCOME, "거래소", "판매 대금 정산"),
                new Scenario("거래소 구매 −20,000", -20_000, TransactionRecord.Kind.EXPENSE, "거래소", "아이템 구매"),
                new Scenario("송금 보냄 −150,000", -150_000, TransactionRecord.Kind.TRANSFER_OUT, "송금", "PlayerA에게 송금"),
                new Scenario("송금 받음 +310,400", 310_400, TransactionRecord.Kind.TRANSFER_IN, "송금", "PlayerA에게서 받음"),
                new Scenario("개인거래 보냄 −30,000", -30_000, TransactionRecord.Kind.TRANSFER_OUT, "개인거래", "PlayerA에게 전달"),
                new Scenario("개인거래 받음 +145,500", 145_500, TransactionRecord.Kind.TRANSFER_IN, "개인거래", "PlayerA에게서 받음"),
                new Scenario("플리마켓 정산 +7,952", 7_952, TransactionRecord.Kind.INCOME, "플리마켓", "판매 대금 정산"),
                new Scenario("플리마켓 구매 −8,000", -8_000, TransactionRecord.Kind.EXPENSE, "플리마켓", "아이템 구매"),
                new Scenario("뽑기 1회 −2,000", -2_000, TransactionRecord.Kind.EXPENSE, "뽑기", "마법 부여 책 뽑기"),
                new Scenario("우편 발송비 −1,000", -1_000, TransactionRecord.Kind.EXPENSE, "이용료", "우편 발송비"));
    }

    private int panelX() { return (this.width - W) / 2; }

    @Override
    protected void init() {
        int x = panelX() + PAD;
        int colW = (W - PAD * 2 - 6) / 2;
        int y = TOP + 50;

        addRenderableWidget(new TexButton(x, y, colW, 18, Component.literal("가짜 잔고 패널 만들기"), this::setup));
        addRenderableWidget(new TexButton(x + colW + 6, y, colW, 18, Component.literal("가짜 패널 지우기"), () -> {
            if (!guard()) return;
            cmd("scoreboard objectives remove " + OBJ);
            status = "가짜 패널을 지웠습니다.";
        }));
        y += ROW + 4;

        List<Scenario> list = scenarios();
        for (int i = 0; i < list.size(); i++) {
            Scenario s = list.get(i);
            int bx = x + (i % 2) * (colW + 6);
            int by = y + (i / 2) * ROW;
            addRenderableWidget(new TexButton(bx, by, colW, 18, Component.literal(s.button()), () -> run(s)));
        }
        int n = list.size();
        y += ((n + 1) / 2) * ROW + 4;

        int thirdW = (W - PAD * 2 - 12) / 3;
        addRenderableWidget(new TexButton(x, y, thirdW, 18, Component.literal("정산 창"), () ->
                this.minecraft.gui.setScreen(new DtStatScreen(config, aggregator, sink, hud))));
        addRenderableWidget(new TexButton(x + thirdW + 6, y, thirdW, 18, Component.literal("진단"), () -> {
            if (this.minecraft.player != null) this.minecraft.player.connection.sendCommand("윤띵 진단");
            this.onClose();
        }));
        addRenderableWidget(new TexButton(x + (thirdW + 6) * 2, y, thirdW, 18, Component.literal("닫기"), this::onClose));
    }

    private int contentBottom() {
        int n = scenarios().size();
        return TOP + 50 + ROW + 4 + ((n + 1) / 2) * ROW + 4 + 18 + 30;
    }

    // ── 동작 ──

    /** 싱글플레이가 아니면 막는다 — 실서버로 명령이 나가면 안 된다. */
    private boolean guard() {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null || !mc.hasSingleplayerServer()) {
            status = "§c싱글플레이 월드에서만 쓸 수 있습니다.";
            return false;
        }
        return true;
    }

    /**
     * 싱글플레이 내장 서버에 서버 권한으로 직접 실행한다 — 채팅으로 명령을 보내지 않으므로
     * 치트가 꺼진 월드에서도 되고, 싱글이 아니면 내장 서버가 없어 아무것도 실행되지 않는다.
     */
    private static void cmd(String c) {
        var server = Minecraft.getInstance().getSingleplayerServer();
        if (server == null) return;
        server.execute(() -> server.getCommands().performPrefixedCommand(
                server.createCommandSourceStack().withSuppressedOutput(), c));
    }

    /**
     * 숫자 칸은 비우고 줄 이름에 금액을 넣는다("쿠키 2,000,000").
     * 점수 칸을 쓰면 Feather 등의 "스코어보드 숫자 숨기기" 옵션에 가려 안 보인다(2026-09-28 사용자 화면).
     * 점수 자체는 정렬용으로만 둔다.
     */
    private static void line(String name, long v) {
        cmd("scoreboard players set " + name + " " + OBJ + " " + Math.min(v, Integer.MAX_VALUE));
        cmd("scoreboard players display name " + name + " " + OBJ + " \"" + name + " " + GoldFormat.format(v) + "\"");
    }

    private static void setBalance(long v) {
        balance = v;
        line("쿠키", v);
    }

    private void setup() {
        if (!guard()) return;
        cmd("scoreboard objectives add " + OBJ + " dummy \"기린월드 테스트\"");
        cmd("scoreboard objectives modify " + OBJ + " numberformat blank");
        cmd("scoreboard objectives setdisplay sidebar " + OBJ);
        line("캔디", 10_000);
        setBalance(2_000_000);
        status = "기린월드 테스트 패널 준비 완료 — 거래 버튼을 눌러 보세요.";
    }

    private void run(Scenario s) {
        if (!guard()) return;
        long amount = Math.abs(s.delta());
        boolean transfer = s.kind() == TransactionRecord.Kind.TRANSFER_IN
                || s.kind() == TransactionRecord.Kind.TRANSFER_OUT;
        sink.accept(new TransactionRecord(System.currentTimeMillis(), s.kind(), amount,
                s.category(), s.label(), 1, !transfer,
                TransactionRecord.Confidence.HIGH, true, "싱글플레이 테스트"));
        long target = balance + s.delta();
        balance = target;
        later(5, () -> setBalance(target));
        status = s.button() + " — 영수증 HUD 반영 완료";
    }

    // ── 지연 실행(틱 단위) ──

    private static final ArrayDeque<Object[]> QUEUE = new ArrayDeque<>();
    private static boolean tickerRegistered;

    private static void later(int ticks, Runnable r) {
        QUEUE.add(new Object[]{ticks, r});
    }

    /** DtLedgerClient 에서 한 번 부른다. */
    public static void registerTicker() {
        if (tickerRegistered) return;
        tickerRegistered = true;
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            Iterator<Object[]> it = QUEUE.iterator();
            java.util.List<Runnable> due = new java.util.ArrayList<>();
            while (it.hasNext()) {
                Object[] e = it.next();
                int left = (int) e[0] - 1;
                if (left <= 0) {
                    due.add((Runnable) e[1]);
                    it.remove();
                } else {
                    e[0] = left;
                }
            }
            due.forEach(Runnable::run);
        });
    }

    // ── 그리기 ──

    @Override
    public void extractBackground(GuiGraphicsExtractor ctx, int mouseX, int mouseY, float delta) {
        ctx.fill(0, 0, this.width, this.height, 0x90000000);
        int x = panelX();
        GuiTex.sprite(ctx, "tex_page", x, TOP, W, contentBottom() - TOP);
        ctx.text(font, "윤띵 테스트 · 싱글 전용", x + PAD, TOP + 22, GuiTex.TITLE, false);
        String bal = "가짜 잔고 " + GoldFormat.format(balance) + " 쿠키";
        ctx.text(font, bal, x + W - PAD - font.width(bal), TOP + 22, GuiTex.LABEL, false);
        ctx.text(font, "치트 없어도 됨 · 이 월드에서 생긴 기록은 나갈 때 자동 삭제",
                x + PAD, TOP + 35, GuiTex.LABEL, false);
        String st = font.plainSubstrByWidth(status, W - PAD * 2);
        ctx.text(font, st, x + PAD, contentBottom() - 24, GuiTex.TEXT, false);
    }

    @Override
    public boolean isPauseScreen() {
        return false; // 멈추면 싱글 서버가 명령을 처리하지 않는다
    }
}

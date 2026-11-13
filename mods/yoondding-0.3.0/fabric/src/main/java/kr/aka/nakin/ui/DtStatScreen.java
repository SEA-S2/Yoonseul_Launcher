package kr.aka.nakin.ui;

import kr.aka.nakin.aggregate.DailyAggregator;
import kr.aka.nakin.aggregate.DailyBucket;
import kr.aka.nakin.aggregate.RecordGrouping;
import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.aggregate.CategoryView;
import kr.aka.nakin.aggregate.ShareText;
import kr.aka.nakin.core.TransactionRecord;
import kr.aka.nakin.core.WalletCheck;
import kr.aka.nakin.util.GoldFormat;
import kr.aka.nakin.util.LedgerDates;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.input.KeyEvent;
import net.minecraft.client.input.MouseButtonEvent;
import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import java.time.format.DateTimeFormatter;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * /낙인 정산 창(양피지 장부) — 오늘 · 주간 · 내역 · 관리 · 설정 5탭.
 * 배경/탭/버튼/구분선/스크롤바 전부 Claude Design 9-slice 텍스처(GuiTex). 전부 read-only 표시.
 */
public final class DtStatScreen extends Screen {

    private static final int DIM = 0x90000000;

    private static final int W = 360;
    private static final int PAD = 12;
    private static final int ROW_H = 13;
    private static final int TOP = 36; // 패널 상단 고정 — 위젯 좌표 안정성
    private static final DateTimeFormatter DAY_FMT = DateTimeFormatter.ofPattern("MM-dd");

    private static final int TAB_STEP = 54;
    private static final int TAB_W = 52;
    private static final int TAB_H = 20;

    private final DtConfig config;
    private final DailyAggregator aggregator;
    private final LedgerHud hud;
    private final java.util.function.Consumer<TransactionRecord> sink;
    private int tab = lastTab; // 0=오늘 1=주간 2=내역 3=관리 4=설정
    /** 창을 닫았다 열어도 보던 탭 유지(게임 켜 있는 동안). 관리 탭 수정 모드는 이어가지 않는다. */
    private static int lastTab = 0;
    private TexButton copyButton;
    private TexButton byCatButton;
    /** 새로워진 점 카드 영역 — 클릭하면 닫는다(렌더에서 갱신). */
    private int whatsNewBottom = -1;

    // 관리 탭
    private EditBox mAmount;
    private EditBox mLabel;
    private TexButton mIncome;
    private TexButton mExpense;
    private TexButton mReset;
    private TexButton mResetWeek;
    private TexButton mResetDate;
    private EditBox mDate;
    // 기록 수정 모드(내역 탭에서 줄 클릭) — null 이면 평소의 수동 입력
    private List<TransactionRecord> editing;
    private String editOriginal = "";
    private EditBox mCategory;
    private TexButton mSave;
    private TexButton mCancel;
    /** 어떤 초기화 버튼이 확인 대기 중인가(0=없음 1=오늘 2=최근7일 3=지정날짜) — 오폭 방지 2단계 확인. */
    private int resetArmedKind;

    // 설정 탭
    private TexButton sHud;
    private TexButton sConfig;
    private TexButton sOpacityDown;
    private TexButton sOpacityUp;
    private TexButton sReport;

    // 통계([통계] 버튼 또는 오늘 탭 카테고리 줄 클릭) — statsOpen 이면 오늘 탭 자리에 그린다.
    // detailCat 이 null 이면 분야 목록, 있으면 그 분야 하나.
    private boolean statsOpen;
    private String detailCat;
    private boolean detailByItem = true; // 거래소·플리마켓 판매를 물건 분야(직업)에 합칠지
    private int fromAgo = 6, toAgo = 0; // 며칠 전 ~ 며칠 전(양끝 포함)
    private int statsScroll;
    private CategoryView.Result detailCache;
    private List<Map.Entry<String, long[]>> listCache;
    private long statsCacheAt;
    private TexButton dBack, dMode, dOlder, dNewer;
    /** 상세 아래쪽: 0=날짜별 1=수입 내역 2=지출 내역 */
    private int listMode;
    /** 누를 수 있는 글자 영역 {x0, x1, y, 바꿀 listMode} — 상세 합계 줄·내역 제목 줄 */
    private final List<int[]> textHits = new java.util.ArrayList<>();
    /** 통계 목록 줄 {y, 분야} */
    private final List<Object[]> statRows = new java.util.ArrayList<>();
    private int mouseXNow, mouseYNow;
    private static final int VISIBLE_STATS = 12;
    private final List<TexButton> dPresets = new java.util.ArrayList<>();
    private static final String[] PRESET_NAMES = {"오늘", "어제", "3일", "7일", "30일"};
    private static final int[][] PRESETS = {{0, 0}, {1, 1}, {2, 0}, {6, 0}, {29, 0}};
    /** 오늘 탭 카테고리 줄 위치(렌더에서 갱신) — 클릭하면 그 분야 상세. */
    private final List<Object[]> catHits = new java.util.ArrayList<>();
    /** 오늘 탭 잔고 대조 줄의 y — 클릭 판정용(렌더에서 갱신). */
    private int walletLineY = -1;

    private static final int VISIBLE_PENDING = 12; // 내역 탭 한 화면 행 수(나머지는 휠 스크롤)
    private int pendingScroll;
    /** 내역 탭 스크롤 손잡이를 마우스로 끌고 있는지. */
    private boolean pendingScrollbarDragging;
    private int pendingScrollbarDragOffset;

    public DtStatScreen(DtConfig config, DailyAggregator aggregator,
                        java.util.function.Consumer<TransactionRecord> sink, LedgerHud hud) {
        super(Component.literal("윤띵 정산"));
        this.config = config;
        this.aggregator = aggregator;
        this.sink = sink;
        this.hud = hud;
    }

    private int panelX() { return (this.width - W) / 2; }

    @Override
    protected void init() {
        int x = panelX();
        int tabY = TOP + 38; // 헤더 텍스트(TOP+24) 바로 아래. 페이지 상단 테두리(20px) 밖
        String[] names = {"오늘", "주간", "내역", "관리", "설정"};
        for (int i = 0; i < names.length; i++) {
            final int idx = i;
            addRenderableWidget(new TabButton(x + PAD + i * TAB_STEP, tabY, TAB_W, TAB_H, Component.literal(names[i]),
                    () -> this.tab == idx, () -> switchTab(idx)));
        }

        int cy = TOP + 62;

        // 오늘·주간 정산 복사(붙여넣기용) — Ctrl+C 와 같은 동작
        copyButton = new TexButton(x + PAD + 34, TOP + 20, 40, 14, Component.literal("복사"), this::copyShare);
        addRenderableWidget(copyButton);
        // 분야별 기간 보기 입구 — 오늘 기록이 없어도 들어갈 수 있게(카테고리 줄 클릭과 같은 화면)
        byCatButton = new TexButton(x + PAD + 78, TOP + 20, 44, 14, Component.literal("통계"), this::toggleStats);
        addRenderableWidget(byCatButton);

        // 관리 탭 위젯 (수동 입력 + 오늘 초기화)
        mAmount = new EditBox(this.font, x + PAD + 6, cy + 18, 100, 12, Component.literal("금액"));
        mAmount.setMaxLength(15);
        mAmount.setBordered(false);
        mAmount.setTextColor(GuiTex.TEXT);
        mAmount.setTextShadow(false); // 양피지 위에서 글자가 두 겹으로 보이지 않게
        addRenderableWidget(mAmount);

        mLabel = new EditBox(this.font, x + PAD + 122, cy + 18, W - PAD * 2 - 128, 12, Component.literal("설명"));
        mLabel.setMaxLength(40);
        mLabel.setBordered(false);
        mLabel.setTextColor(GuiTex.TEXT);
        mLabel.setTextShadow(false); // 양피지 위에서 글자가 두 겹으로 보이지 않게
        addRenderableWidget(mLabel);

        mCategory = new EditBox(this.font, x + PAD + 122, cy + 18, 90, 12, Component.literal("카테고리"));
        mCategory.setMaxLength(20);
        mCategory.setBordered(false);
        mCategory.setTextColor(GuiTex.TEXT);
        mCategory.setTextShadow(false);
        addRenderableWidget(mCategory);
        mSave = new TexButton(x + PAD, cy + 36, 150, 18, Component.literal("수정 저장"), this::saveEdit);
        addRenderableWidget(mSave);
        mCancel = new TexButton(x + W - PAD - 150, cy + 36, 150, 18, Component.literal("취소"), () -> switchTab(2));
        addRenderableWidget(mCancel);

        mIncome = new TexButton(x + PAD, cy + 36, 150, 18, Component.literal("+ 수입 추가"), () -> addManual(true));
        addRenderableWidget(mIncome);

        mExpense = new TexButton(x + W - PAD - 150, cy + 36, 150, 18, Component.literal("− 지출 추가"), () -> addManual(false));
        addRenderableWidget(mExpense);

        mReset = new TexButton(x + PAD, cy + 68, (W - PAD * 2 - 4) / 2, 18,
                Component.literal("오늘 초기화"), this::onResetClick);
        addRenderableWidget(mReset);

        mResetWeek = new TexButton(x + PAD + (W - PAD * 2 - 4) / 2 + 4, cy + 68, (W - PAD * 2 - 4) / 2, 18,
                Component.literal("최근 7일 초기화"), this::onResetWeekClick);
        addRenderableWidget(mResetWeek);

        // 특정 날짜만 지우기 — 잘못 기록된 날짜를 직접 지정(YYYY-MM-DD, 비우면 오늘)
        mDate = new EditBox(this.font, x + PAD + 6, cy + 108, 100, 12, Component.literal("날짜"));
        mDate.setMaxLength(10);
        mDate.setBordered(false);
        mDate.setTextColor(GuiTex.TEXT);
        mDate.setTextShadow(false); // 양피지 위에서 글자가 두 겹으로 보이지 않게
        // placeholder 는 그림자가 붙어 양피지 위에서 글자가 두 겹으로 보여 쓰지 않음 —
        // 형식 안내는 위 라벨에 넣는다(2026-07-28).
        addRenderableWidget(mDate);

        mResetDate = new TexButton(x + PAD + 116, cy + 102, W - PAD * 2 - 116, 18,
                Component.literal("이 날짜 초기화"), this::onResetDateClick);
        addRenderableWidget(mResetDate);

        // 설정 탭 위젯
        sHud = new TexButton(x + PAD, cy + 14, W - PAD * 2, 20, Component.literal("HUD 위치 설정"), () ->
                this.minecraft.gui.setScreen(new DtHudEditScreen(config, hud, this)));
        addRenderableWidget(sHud);

        sConfig = new TexButton(x + PAD, cy + 40, W - PAD * 2, 20, Component.literal("설정 열기"), () -> {
            Screen screen = kr.aka.nakin.config.DtConfigScreen.create(config, aggregator, this);
            if (screen != null) this.minecraft.gui.setScreen(screen);
        });
        addRenderableWidget(sConfig);

        sOpacityDown = new TexButton(x + PAD, cy + 66, 30, 18, Component.literal("-"), () -> adjustOpacity(-0.1f));
        addRenderableWidget(sOpacityDown);
        sOpacityUp = new TexButton(x + W - PAD - 30, cy + 66, 30, 18, Component.literal("+"), () -> adjustOpacity(0.1f));
        addRenderableWidget(sOpacityUp);
        sReport = new TexButton(x + PAD, cy + 92, W - PAD * 2, 20, Component.literal("제보용 기록 복사"), () -> {
            DtStatCommand.copyReport();
            sReport.setMessage(Component.literal("복사했어요 · 제보 폼에 Ctrl+V"));
        });
        addRenderableWidget(sReport);

        // 통계 위젯
        dBack = new TexButton(x + PAD, cy, 44, 16, Component.literal("‹ 목록"), () -> {
            detailCat = null;
            listMode = 0;
            invalidateStats();
            updateWidgets();
        });
        addRenderableWidget(dBack);
        dMode = new TexButton(x + W - PAD - 160, cy, 160, 16, Component.literal(""), () -> {
            detailByItem = !detailByItem;
            statsScroll = 0;
            invalidateStats();
            updateWidgets();
        });
        addRenderableWidget(dMode);
        dPresets.clear();
        for (int i = 0; i < PRESETS.length; i++) {
            final int[] p = PRESETS[i];
            TexButton b = new TexButton(x + PAD + i * 44, cy + 22, 40, 16, Component.literal(PRESET_NAMES[i]), () -> {
                fromAgo = p[0];
                toAgo = p[1];
                statsScroll = 0;
                invalidateStats();
            });
            dPresets.add(b);
            addRenderableWidget(b);
        }
        dOlder = new TexButton(x + PAD + 222, cy + 22, 54, 16, Component.literal("◀ 하루 전"), () -> shiftPeriod(1));
        addRenderableWidget(dOlder);
        dNewer = new TexButton(x + PAD + 280, cy + 22, 54, 16, Component.literal("하루 뒤 ▶"), () -> shiftPeriod(-1));
        addRenderableWidget(dNewer);

        updateWidgets();
    }

    private void invalidateStats() {
        detailCache = null;
        listCache = null;
    }

    /** [통계] — 분야 목록(최근 7일)을 연다. 열려 있으면 닫는다. */
    private void toggleStats() {
        boolean wasOpen = statsOpen && tab == 0;
        switchTab(0);
        if (wasOpen) return;
        statsOpen = true;
        fromAgo = 6;
        toAgo = 0;
        statsScroll = 0;
        invalidateStats();
        updateWidgets();
    }

    /**
     * 분야 하나를 연다. 오늘 탭에서 바로 왔으면 최근 7일로, 목록에서 왔으면 보던 기간 그대로.
     * 판매처 카테고리(거래소 등)를 눌렀으면 "따로 봄"이어야 그 분야가 보인다.
     */
    private void openDetail(String cat) {
        if (!statsOpen) {
            fromAgo = 6;
            toAgo = 0;
        }
        statsOpen = true;
        detailCat = cat;
        if (CategoryView.VENUES.contains(cat)) detailByItem = false;
        listMode = 0;
        invalidateStats();
        updateWidgets();
    }

    /** 기간 창을 길이 그대로 하루씩 옮긴다(+1 = 과거로). 미래로는 못 간다. */
    private void shiftPeriod(int days) {
        if (toAgo + days < 0 || fromAgo + days > 365) return;
        fromAgo += days;
        toAgo += days;
        statsScroll = 0;
        invalidateStats();
    }

    private java.time.LocalDate statsFrom() {
        return LedgerDates.today(config.dayResetHour).minusDays(fromAgo);
    }

    private java.time.LocalDate statsTo() {
        return LedgerDates.today(config.dayResetHour).minusDays(toAgo);
    }

    private java.util.function.Function<TransactionRecord, java.time.LocalDate> dateOf() {
        return r -> LedgerDates.ledgerDate(r.timestamp, config.dayResetHour);
    }

    /** 1초마다 다시 계산(그 사이 새 거래 반영). */
    private boolean statsStale() {
        long now = System.currentTimeMillis();
        if (now - statsCacheAt <= 1_000) return false;
        statsCacheAt = now;
        return true;
    }

    private CategoryView.Result detail() {
        if (detailCache == null || statsStale()) {
            detailCache = CategoryView.of(aggregator.records(statsFrom(), statsTo()), detailCat,
                    detailByItem, statsFrom(), statsTo(), dateOf());
        }
        return detailCache;
    }

    private List<Map.Entry<String, long[]>> statsList() {
        if (listCache == null || statsStale()) {
            listCache = CategoryView.totals(aggregator.records(statsFrom(), statsTo()), detailByItem,
                    statsFrom(), statsTo(), dateOf());
        }
        return listCache;
    }

    private void switchTab(int idx) {
        this.tab = idx;
        lastTab = idx == 3 ? 0 : idx; // 관리 탭은 입력 중 상태라 다음에 열 때 이어가지 않는다
        resetArmedKind = 0; // 탭 이동 시 확인 대기 취소
        if (editing != null) cancelEdit();
        statsOpen = false;
        detailCat = null;
        updateWidgets();
    }

    private void copyShare() {
        String text = statsOpen && detailCat != null ? CategoryView.share(detail())
                : statsOpen ? CategoryView.shareList(statsList(), statsFrom(), statsTo())
                : tab == 1 ? ShareText.week(aggregator.lastDays(7))
                : ShareText.day(aggregator.today());
        this.minecraft.keyboardHandler.setClipboard(text);
        copyButton.setMessage(Component.literal("복사됨"));
    }

    @Override
    public boolean keyPressed(KeyEvent ev) {
        // 입력칸에 쓰는 중이면 숫자·Ctrl+C 는 입력칸 몫
        if (!(getFocused() instanceof EditBox eb && eb.isFocused())) {
            int key = ev.key();
            if (ev.hasControlDown() && key == 67 && (tab == 0 || tab == 1)) { // GLFW_KEY_C
                copyShare();
                return true;
            }
            if (!ev.hasControlDown() && key >= 49 && key <= 53) { // GLFW_KEY_1 .. GLFW_KEY_5
                switchTab(key - 49);
                return true;
            }
        }
        return super.keyPressed(ev);
    }

    private void updateWidgets() {
        if (copyButton != null) {
            copyButton.visible = tab == 0 || tab == 1;
            copyButton.setMessage(Component.literal("복사"));
        }
        if (byCatButton != null) {
            byCatButton.visible = tab == 0 || tab == 1;
            byCatButton.setMessage(Component.literal(tab == 0 && statsOpen ? "닫기" : "통계"));
        }
        boolean st = tab == 0 && statsOpen;
        if (dBack != null) {
            dBack.visible = st && detailCat != null;
            dMode.visible = st;
            dMode.setMessage(Component.literal(detailByItem ? "거래소·플리 판매: 직업에 합침" : "거래소·플리 판매: 따로 봄"));
            dOlder.visible = st;
            dNewer.visible = st;
            for (TexButton b : dPresets) b.visible = st;
        }

        boolean manage = tab == 3;
        boolean edit = manage && editing != null;
        mAmount.visible = manage;
        mLabel.visible = manage;
        // 수정 모드: 금액 | 카테고리 | 설명 세 칸, 평소: 금액 | 설명 두 칸
        int x = panelX();
        mLabel.setX(edit ? x + PAD + 226 : x + PAD + 122);
        mLabel.setWidth(edit ? W - PAD * 2 - 232 : W - PAD * 2 - 128);
        mCategory.visible = edit;
        mSave.visible = edit;
        mCancel.visible = edit;
        mIncome.visible = manage && !edit;
        mExpense.visible = manage && !edit;
        mReset.visible = manage && !edit;
        if (mResetWeek != null) mResetWeek.visible = manage && !edit;
        if (mResetDate != null) mResetDate.visible = manage && !edit;
        if (mDate != null) mDate.visible = manage && !edit;
        // 확인 대기 중인 버튼만 문구가 바뀐다(어느 걸 지우려는지 헷갈리지 않게)
        if (mReset != null) mReset.setMessage(Component.literal(
                resetArmedKind == 1 ? "정말 지울까요?" : "오늘 초기화"));
        if (mResetWeek != null) mResetWeek.setMessage(Component.literal(
                resetArmedKind == 2 ? "정말 지울까요?" : "최근 7일 초기화"));
        if (mResetDate != null) mResetDate.setMessage(Component.literal(
                resetArmedKind == 3 ? "정말 지울까요?" : "이 날짜 초기화"));

        boolean settings = tab == 4;
        if (sHud != null) sHud.visible = settings;
        if (sConfig != null) sConfig.visible = settings;
        if (sOpacityDown != null) sOpacityDown.visible = settings;
        if (sOpacityUp != null) sOpacityUp.visible = settings;
        if (sReport != null) sReport.visible = settings;
    }

    private void adjustOpacity(float delta) {
        config.hudOpacity = Math.round(Math.max(0.2f, Math.min(1.0f, config.hudOpacity + delta)) * 100) / 100f;
        config.save();
    }

    private void addManual(boolean income) {
        String digits = mAmount.getValue().replaceAll("[^0-9]", "");
        if (digits.isEmpty()) return;
        long amt;
        try {
            amt = Long.parseLong(digits);
        } catch (NumberFormatException e) {
            return;
        }
        String label = mLabel.getValue().isBlank() ? "수동 입력" : mLabel.getValue().trim();
        TransactionRecord rec = new TransactionRecord(System.currentTimeMillis(),
                income ? TransactionRecord.Kind.INCOME : TransactionRecord.Kind.EXPENSE,
                amt, "수동", label, 0, true, TransactionRecord.Confidence.HIGH, false, "수동 입력");
        sink.accept(rec);
        mAmount.setValue("");
        mLabel.setValue("");
    }

    /**
     * 내역 탭에서 Shift+클릭한 줄의 기록을 삭제(2026-07-28 요청: 잘못된 데이터만 고치고 싶은 경우).
     * 묶음("×N") 줄이면 그 묶음에 들어간 원본 전부를 지운다. 집계·저장 원장 양쪽에서 제거됨.
     * @return 지웠으면 true
     */
    private boolean deleteRowAt(double mouseX, double mouseY) {
        RecordGrouping.Grouped g = rowAt(mouseX, mouseY);
        if (g == null) return false;
        boolean removed = false;
        for (TransactionRecord r : g.sources()) {
            if (aggregator.deleteRecord(r)) removed = true;
        }
        if (removed) pendingScroll = 0; // 목록이 줄어 스크롤 위치가 어긋나지 않게
        return removed;
    }

    /** 내역 탭에서 마우스 아래 줄. 없으면 null. */
    private RecordGrouping.Grouped rowAt(double mouseX, double mouseY) {
        int x = panelX();
        int left = x + PAD, right = x + W - PAD;
        if (mouseX < left || mouseX > right) return null;

        List<RecordGrouping.Grouped> p = groupedPending();
        if (p.isEmpty()) return null;
        int total = p.size();
        // 스크롤바는 내역 행이 아니다. 손잡이를 누를 때 수정 화면으로 넘어가지 않게 분리한다.
        if (total > VISIBLE_PENDING && mouseX >= right - 8) return null;
        int start = Math.max(0, Math.min(pendingScroll, Math.max(0, total - VISIBLE_PENDING)));
        int end = Math.min(total, start + VISIBLE_PENDING);

        int y0 = TOP + 62; // renderPending 시작 y 와 동일해야 함
        if (mouseY < y0) return null;
        int row = (int) ((mouseY - y0) / ROW_H);
        if (row >= end - start) return null;
        return p.get(total - 1 - (start + row)); // 최신이 위 — 렌더와 동일 순서
    }

    /** 내역 탭 스크롤바의 손잡이를 누르거나 트랙을 누른 경우 드래그를 시작한다. */
    private boolean beginPendingScrollbarDrag(double mouseX, double mouseY) {
        List<RecordGrouping.Grouped> rows = groupedPending();
        int total = rows.size();
        if (total <= VISIBLE_PENDING) return false;

        int right = panelX() + W - PAD;
        int barX = right - 8;
        int trackTop = TOP + 62;
        int trackH = VISIBLE_PENDING * ROW_H;
        if (mouseX < barX || mouseX > right || mouseY < trackTop || mouseY >= trackTop + trackH) return false;

        int maxScroll = total - VISIBLE_PENDING;
        int thumbH = Math.max(16, trackH * VISIBLE_PENDING / total);
        int thumbY = trackTop + (int) ((long) (trackH - thumbH) * pendingScroll / maxScroll);
        if (mouseY >= thumbY && mouseY < thumbY + thumbH) {
            pendingScrollbarDragOffset = (int) mouseY - thumbY;
        } else {
            // 빈 트랙을 누르면 손잡이 중앙이 커서로 이동한 뒤 바로 끌 수 있다.
            pendingScrollbarDragOffset = thumbH / 2;
            updatePendingScrollFromDrag(mouseY);
        }
        pendingScrollbarDragging = true;
        return true;
    }

    private void updatePendingScrollFromDrag(double mouseY) {
        int total = groupedPending().size();
        int maxScroll = Math.max(0, total - VISIBLE_PENDING);
        if (maxScroll == 0) {
            pendingScroll = 0;
            return;
        }
        int trackTop = TOP + 62;
        int trackH = VISIBLE_PENDING * ROW_H;
        int thumbH = Math.max(16, trackH * VISIBLE_PENDING / total);
        int travel = trackH - thumbH;
        int thumbY = Math.max(trackTop, Math.min((int) mouseY - pendingScrollbarDragOffset, trackTop + travel));
        pendingScroll = (int) Math.round((double) (thumbY - trackTop) * maxScroll / travel);
    }

    /** 내역 줄 클릭 → 관리 탭을 수정 모드로. 묶음(×N)이면 N건 모두 같은 값으로 고친다. */
    private void startEdit(RecordGrouping.Grouped g) {
        TransactionRecord first = g.sources().get(0);
        editing = List.copyOf(g.sources());
        editOriginal = (first.kind == TransactionRecord.Kind.INCOME || first.kind == TransactionRecord.Kind.TRANSFER_IN ? "+" : "-")
                + (first.amount == 0 ? "금액 미확인" : GoldFormat.format(first.amount)) + " · " + first.category
                + (first.label == null || first.label.isEmpty() || first.label.equals(first.category) ? "" : " · " + first.label)
                + (g.count() > 1 ? "  (×" + g.count() + "건 모두)" : "");
        mAmount.setValue(first.amount == 0 ? "" : String.valueOf(first.amount));
        mCategory.setValue(first.category == null ? "" : first.category);
        mLabel.setValue(first.label == null ? "" : first.label);
        tab = 3;
        resetArmedKind = 0;
        updateWidgets();
    }

    private void saveEdit() {
        String digits = mAmount.getValue().replaceAll("[^0-9]", "");
        if (editing == null || digits.isEmpty()) return;
        long amt;
        try {
            amt = Long.parseLong(digits);
        } catch (NumberFormatException e) {
            return;
        }
        String cat = mCategory.getValue().isBlank() ? "기타" : mCategory.getValue().trim();
        String label = mLabel.getValue().trim();
        for (TransactionRecord r : editing) aggregator.editRecord(r, amt, cat, label);
        switchTab(2); // 고친 결과를 바로 확인
    }

    private void cancelEdit() {
        editing = null;
        editOriginal = "";
        mAmount.setValue("");
        mCategory.setValue("");
        mLabel.setValue("");
    }

    @Override
    public boolean mouseClicked(MouseButtonEvent ev, boolean doubleClick) {
        double mouseX = ev.x(), mouseY = ev.y();
        if (ev.button() == 0) {
            if (tab == 2) {
                if (beginPendingScrollbarDrag(mouseX, mouseY)) return true;
                if (ev.hasShiftDown()) {
                    if (deleteRowAt(mouseX, mouseY)) return true;
                } else {
                    RecordGrouping.Grouped g = rowAt(mouseX, mouseY);
                    if (g != null) {
                        startEdit(g);
                        return true;
                    }
                }
            }
            if (tab == 0 && statsOpen) {
                if (detailCat == null) {
                    for (Object[] row : statRows) {
                        int ry = (int) row[0];
                        if (mouseY >= ry && mouseY < ry + ROW_H && mouseX >= panelX() + PAD && mouseX <= panelX() + W - PAD) {
                            openDetail((String) row[1]);
                            return true;
                        }
                    }
                } else {
                    for (int[] h : textHits) {
                        if (mouseX >= h[0] && mouseX < h[1] && mouseY >= h[2] - 1 && mouseY < h[2] + 10) {
                            listMode = h[3] == 0 || listMode == h[3] ? 0 : h[3];
                            return true;
                        }
                    }
                }
            }
            if (tab == 0 && !statsOpen) {
                for (Object[] hit : catHits) {
                    int hy = (int) hit[0];
                    if (mouseY >= hy && mouseY < hy + ROW_H && mouseX >= panelX() + PAD && mouseX <= panelX() + W - PAD) {
                        openDetail((String) hit[1]);
                        return true;
                    }
                }
                if (mouseY < whatsNewBottom && mouseY >= TOP + 62 && WhatsNew.unseen(config.lastSeenWhatsNew)) {
                    config.lastSeenWhatsNew = WhatsNew.VERSION;
                    config.save();
                    return true;
                }
                // 잔고 대조 경고 줄 클릭 = "알고 넘어감"(기준을 지금으로)
                Long gap = WalletCheck.LIVE.unexplained();
                if (gap != null && gap != 0 && mouseY >= walletLineY - 2 && mouseY < walletLineY + 11) {
                    WalletCheck.LIVE.acknowledge(System.currentTimeMillis());
                    return true;
                }
            }
        }
        return super.mouseClicked(ev, doubleClick);
    }

    @Override
    public boolean mouseDragged(MouseButtonEvent ev, double dx, double dy) {
        if (pendingScrollbarDragging && ev.button() == 0) {
            updatePendingScrollFromDrag(ev.y());
            return true;
        }
        return super.mouseDragged(ev, dx, dy);
    }

    @Override
    public boolean mouseReleased(MouseButtonEvent ev) {
        if (pendingScrollbarDragging && ev.button() == 0) {
            pendingScrollbarDragging = false;
            return true;
        }
        return super.mouseReleased(ev);
    }

    private void onResetClick() {
        if (armOrConfirm(1)) aggregator.resetToday();
    }

    private void onResetWeekClick() {
        if (armOrConfirm(2)) aggregator.resetLastDays(7);
    }

    /** 지정 날짜(YYYY-MM-DD, 비우면 오늘)만 초기화 — 잘못 기록된 특정 날짜 수정용. */
    private void onResetDateClick() {
        java.time.LocalDate date = parseDateInput();
        if (date == null) { // 형식이 틀리면 아무것도 지우지 않고 대기 해제
            resetArmedKind = 0;
            updateWidgets();
            return;
        }
        if (armOrConfirm(3)) aggregator.resetDay(date);
    }

    private java.time.LocalDate parseDateInput() {
        String s = mDate == null ? "" : mDate.getValue().trim();
        if (s.isEmpty()) return LedgerDates.today(config.dayResetHour);
        try {
            return java.time.LocalDate.parse(s);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * 2단계 확인: 처음 누르면 해당 버튼만 "정말 지울까요?"로 바뀌고, 한 번 더 눌러야 실행.
     * 다른 초기화 버튼을 누르면 이전 대기는 취소된다(오폭 방지).
     * @return 실제로 실행할 차례면 true
     */
    private boolean armOrConfirm(int kind) {
        if (resetArmedKind != kind) {
            resetArmedKind = kind;
            updateWidgets();
            return false;
        }
        resetArmedKind = 0;
        updateWidgets();
        return true;
    }

    @Override
    public void extractBackground(GuiGraphicsExtractor ctx, int mouseX, int mouseY, float delta) {
        mouseXNow = mouseX; // 통계 화면의 밑줄·줄 강조용
        mouseYNow = mouseY;
        ctx.fill(0, 0, this.width, this.height, DIM);
        drawPanel(ctx);
    }

    private void drawPanel(GuiGraphicsExtractor ctx) {
        int x = panelX();
        int h = 68 + contentHeight() + PAD; // cy(TOP+62) + 6
        int right = x + W;

        GuiTex.sprite(ctx, "tex_page", x, TOP, W, h);

        // 헤더 — 9-slice 테두리(20px) 바로 아래(4px 여백)
        ctx.text(font, "윤띵", x + PAD, TOP + 24, GuiTex.TITLE, false);
        String today = LedgerDates.today(config.dayResetHour).toString();
        ctx.text(font, today, right - PAD - font.width(today), TOP + 24, GuiTex.LABEL, false);

        int cy = TOP + 62;
        switch (tab) {
            case 0 -> {
                if (statsOpen && detailCat != null) renderDetail(ctx, x, cy);
                else if (statsOpen) renderStatsList(ctx, x, cy);
                else renderToday(ctx, x, cy);
            }
            case 1 -> renderWeek(ctx, x, cy);
            case 2 -> renderPending(ctx, x, cy);
            case 3 -> renderManage(ctx, x, cy);
            case 4 -> renderSettings(ctx, x, cy);
        }
    }

    private int contentHeight() {
        return switch (tab) {
            case 0 -> {
                if (statsOpen && detailCat != null) yield detailHeight(detail());
                if (statsOpen) yield statsListHeight();
                DailyBucket b = aggregator.today();
                int cats = shownCats(b.incomeByCategory) + shownCats(b.expenseByCategory);
                yield 64 + (cats > 0 ? 16 + cats * ROW_H : 0) + (showTransfers(b) ? 16 : 0) + 14 + whatsNewHeight();
            }
            case 1 -> 7 * (ROW_H + 1) + 26 + ROW_H; // +범례 한 줄
            case 2 -> Math.max(28, Math.min(Math.max(groupedPending().size(), 1), VISIBLE_PENDING) * ROW_H + 16);
            case 3 -> editing != null ? 84 : 140; // 수동입력 + 초기화 3종(날짜칸·안내문 포함)
            default -> 118; // 설정
        };
    }

    private static int shownCats(Map<String, Long> m) { return Math.min(m.size(), 5); }

    private boolean showTransfers(DailyBucket b) {
        return config.showTransfers && (b.transferIn > 0 || b.transferOut > 0);
    }

    // ── 오늘 탭 ──
    private int whatsNewHeight() {
        return WhatsNew.unseen(config.lastSeenWhatsNew) ? 22 + WhatsNew.LINES.size() * 11 : 0;
    }

    private void renderToday(GuiGraphicsExtractor ctx, int x, int y) {
        DailyBucket b = aggregator.today();
        long net = b.netPnl();
        int left = x + PAD, right = x + W - PAD;

        int wn = whatsNewHeight();
        if (wn > 0) {
            GuiTex.sprite(ctx, "tex_card", left, y, right - left, wn - 4);
            ctx.text(font, WhatsNew.VERSION + " 새로워진 점", left + 8, y + 5, GuiTex.TITLE, false);
            String close = "클릭하면 닫힘";
            ctx.text(font, close, right - 8 - font.width(close), y + 5, GuiTex.LABEL, false);
            int ly = y + 16;
            for (String l : WhatsNew.LINES) {
                ctx.text(font, font.plainSubstrByWidth("· " + l, right - left - 16), left + 8, ly, GuiTex.TEXT, false);
                ly += 11;
            }
            y += wn;
        }
        whatsNewBottom = y;

        GuiTex.sprite(ctx, "tex_card", left, y, right - left, 30);
        ctx.text(font, "오늘 순익", left + 8, y + 5, GuiTex.LABEL, false);
        String netStr = GoldFormat.signed(net) + " 냥";
        int netColor = net > 0 ? GuiTex.GREEN : (net < 0 ? GuiTex.RED : GuiTex.NEUTRAL);
        ctx.text(font, netStr, left + 8, y + 16, netColor, false);
        String cnt = b.count + "건";
        ctx.text(font, cnt, right - 8 - font.width(cnt), y + 16, GuiTex.LABEL, false);
        DailyBucket yday = aggregator.day(b.date.minusDays(1));
        if (yday.count > 0) {
            long diff = net - yday.netPnl();
            String vs = "어제보다 " + GoldFormat.signed(diff);
            ctx.text(font, vs, right - 8 - font.width(vs), y + 5, diff >= 0 ? GuiTex.GREEN : GuiTex.RED, false);
        }
        y += 36;

        int half = (right - left - 8) / 2;
        ctx.text(font, "▲ 수입", left, y, GuiTex.GREEN, false);
        String in = GoldFormat.format(b.income);
        ctx.text(font, in, left + half - font.width(in), y, GuiTex.TEXT, false);
        ctx.text(font, "▼ 지출", left + half + 8, y, GuiTex.RED, false);
        String out = GoldFormat.format(b.expense);
        ctx.text(font, out, right - font.width(out), y, GuiTex.TEXT, false);
        y += 16;

        int cats = shownCats(b.incomeByCategory) + shownCats(b.expenseByCategory);
        catHits.clear();
        if (cats > 0) {
            GuiTex.tileH(ctx, "tex_divider", left, right, y, 48, 6);
            y += 6;
            long max = 1;
            for (long v : b.incomeByCategory.values()) max = Math.max(max, v);
            for (long v : b.expenseByCategory.values()) max = Math.max(max, v);
            y = catRows(ctx, b.incomeByCategory, left, right, y, max, GuiTex.GREEN);
            y = catRows(ctx, b.expenseByCategory, left, right, y, max, GuiTex.RED);
        }

        if (showTransfers(b)) {
            y += 4;
            String tr = "이체(손익 제외)  +" + GoldFormat.format(b.transferIn) + " / -" + GoldFormat.format(b.transferOut);
            ctx.text(font, tr, left, y, GuiTex.BLUE, false);
            y += 12;
        }

        y += 4;
        walletLineY = y;
        Long gap = WalletCheck.LIVE.unexplained();
        String line;
        int color;
        if (!WalletCheck.LIVE.started()) {
            line = "잔고 대조 · 잔고를 못 읽어 확인할 수 없어요";
            color = GuiTex.LABEL;
        } else if (gap == null) {
            line = "잔고 대조 · 확인 중…";
            color = GuiTex.LABEL;
        } else if (gap == 0) {
            line = "✔ 잔고 대조 · 이번 접속 동안 기록과 잔고가 일치";
            color = GuiTex.GREEN;
        } else {
            line = "⚠ 기록에 없는 잔고 변동 " + GoldFormat.signed(gap) + " 냥 · 클릭하면 넘어감";
            color = GuiTex.RED;
        }
        ctx.text(font, font.plainSubstrByWidth(line, right - left), left, y, color, false);
    }

    // ── 통계 ──
    private String periodText() {
        java.time.LocalDate from = statsFrom(), to = statsTo();
        return from.equals(to) ? from + "  (하루)" : from + " ~ " + to + "  (" + (fromAgo - toAgo + 1) + "일)";
    }

    private int statsListHeight() {
        int n = statsList().size();
        return 44 + 12 + 14 + 6 + (n == 0 ? 14 : Math.min(n, VISIBLE_STATS) * ROW_H) + (n > VISIBLE_STATS ? 12 : 0) + 4;
    }

    private void renderStatsList(GuiGraphicsExtractor ctx, int x, int y) {
        List<Map.Entry<String, long[]>> rows = statsList();
        int left = x + PAD, right = x + W - PAD;
        ctx.text(font, "분야별 수익", left, y + 4, GuiTex.TITLE, false);
        y += 44;
        ctx.text(font, periodText(), left, y, GuiTex.LABEL, false);
        y += 12;
        long in = 0, out = 0, max = 1;
        for (var e : rows) {
            in += e.getValue()[0];
            out += e.getValue()[1];
            max = Math.max(max, Math.max(e.getValue()[0], e.getValue()[1]));
        }
        String sum = "수입 " + GoldFormat.format(in) + "  ·  지출 " + GoldFormat.format(out)
                + "  ·  순익 " + GoldFormat.signed(in - out);
        ctx.text(font, font.plainSubstrByWidth(sum, right - left), left, y,
                in - out >= 0 ? GuiTex.GREEN : GuiTex.RED, false);
        y += 14;
        GuiTex.tileH(ctx, "tex_divider", left, right, y, 48, 6);
        y += 6;

        statRows.clear();
        if (rows.isEmpty()) {
            ctx.text(font, "이 기간에는 기록이 없어요", left, y + 2, GuiTex.LABEL, false);
            return;
        }
        int n = rows.size();
        statsScroll = Math.max(0, Math.min(statsScroll, Math.max(0, n - VISIBLE_STATS)));
        int end = Math.min(n, statsScroll + VISIBLE_STATS);
        boolean scrollable = n > VISIBLE_STATS;
        for (int i = statsScroll; i < end; i++) {
            String cat = rows.get(i).getKey();
            long[] v = rows.get(i).getValue();
            statRows.add(new Object[]{y, cat});
            boolean hover = mouseYNow >= y && mouseYNow < y + ROW_H && mouseXNow >= left && mouseXNow <= right;
            if (hover) ctx.fill(left - 2, y, right + 2, y + ROW_H, 0x22000000);
            GuiTex.sprite(ctx, GuiTex.icon(cat), left, y, 12, 12);
            ctx.text(font, font.plainSubstrByWidth(cat, 60), left + 15, y + 2, GuiTex.LABEL, false);
            int barLeft = left + 80, barMax = right - barLeft - 130;
            ctx.fill(barLeft, y + 3, barLeft + barMax, y + 9, GuiTex.TRACK);
            if (v[0] > 0) ctx.fill(barLeft, y + 3, barLeft + (int) Math.max(2, barMax * v[0] / max), y + 6, GuiTex.GREEN);
            if (v[1] > 0) ctx.fill(barLeft, y + 6, barLeft + (int) Math.max(2, barMax * v[1] / max), y + 9, GuiTex.RED);
            // 오른쪽 끝: 수입(있으면) — 그 왼쪽: 지출(있으면)
            int ax = right;
            if (v[0] > 0) {
                String a = "+" + GoldFormat.format(v[0]);
                ax -= font.width(a);
                ctx.text(font, a, ax, y + 2, GuiTex.GREEN, false);
                ax -= 6;
            }
            if (v[1] > 0) {
                String a = "-" + GoldFormat.format(v[1]);
                ax -= font.width(a);
                ctx.text(font, a, ax, y + 2, GuiTex.RED, false);
            }
            y += ROW_H;
        }
        if (scrollable) {
            ctx.text(font, (statsScroll + 1) + "–" + end + " / " + n + "  · 휠 스크롤", left, y + 2, GuiTex.LABEL, false);
        }
    }

    private int detailHeight(CategoryView.Result r) {
        int body;
        if (listMode != 0) body = 14 + Math.max(1, (listMode == 1 ? r.top() : r.spent()).size()) * ROW_H;
        else body = r.count() == 0 || r.days() > 7 ? 14 : r.days() * ROW_H;
        return 44 + 12 + 14 + 6 + body + 4;
    }

    /** 누를 수 있는 글자 — 마우스를 올리면 밑줄. @return 다음 글자 x */
    private int clickText(GuiGraphicsExtractor ctx, String text, int x, int y, int color, int mode) {
        int w = font.width(text);
        ctx.text(font, text, x, y, color, false);
        if (mouseXNow >= x && mouseXNow < x + w && mouseYNow >= y - 1 && mouseYNow < y + 10) {
            ctx.fill(x, y + 9, x + w, y + 10, color);
        }
        textHits.add(new int[]{x, x + w, y, mode});
        return x + w;
    }

    private void renderDetail(GuiGraphicsExtractor ctx, int x, int y) {
        CategoryView.Result r = detail();
        int left = x + PAD, right = x + W - PAD;
        GuiTex.sprite(ctx, GuiTex.icon(r.category()), left + 50, y + 2, 12, 12);
        ctx.text(font, font.plainSubstrByWidth(r.category(), 110), left + 66, y + 4, GuiTex.TITLE, false);
        y += 44;
        ctx.text(font, periodText(), left, y, GuiTex.LABEL, false);
        y += 12;

        // 합계 줄 — 수입·지출 글자를 누르면 아래가 그 내역으로 바뀐다
        textHits.clear();
        int sx = clickText(ctx, "수입 " + GoldFormat.format(r.income()) + (listMode == 1 ? " ▾" : " ▸"),
                left, y, GuiTex.GREEN, 1);
        ctx.text(font, "  ·  ", sx, y, GuiTex.LABEL, false);
        sx += font.width("  ·  ");
        sx = clickText(ctx, "지출 " + GoldFormat.format(r.expense()) + (listMode == 2 ? " ▾" : " ▸"),
                sx, y, GuiTex.RED, 2);
        ctx.text(font, "  ·  순익 " + GoldFormat.signed(r.net()), sx, y,
                r.net() >= 0 ? GuiTex.GREEN : GuiTex.RED, false);
        y += 14;
        GuiTex.tileH(ctx, "tex_divider", left, right, y, 48, 6);
        y += 6;

        if (listMode != 0) {
            itemList(ctx, r, listMode == 1 ? r.top() : r.spent(), listMode == 1, left, right, y);
        } else if (r.count() == 0) {
            ctx.text(font, "이 기간에는 기록이 없어요", left, y + 2, GuiTex.LABEL, false);
        } else if (r.days() > 7) { // 30일 막대는 화면을 넘친다
            ctx.text(font, "기간이 길어 날짜별은 생략했어요 · 수입·지출을 누르면 내역이 나와요",
                    left, y + 2, GuiTex.LABEL, false);
        } else {
            long max = 1;
            for (long[] d : r.byDay().values()) max = Math.max(max, Math.max(d[0], d[1]));
            for (var e : r.byDay().entrySet()) {
                long[] d = e.getValue();
                ctx.text(font, e.getKey().format(DAY_FMT), left, y + 2, GuiTex.LABEL, false);
                int barLeft = left + 40, barMax = right - barLeft - 90;
                ctx.fill(barLeft, y + 3, barLeft + barMax, y + 9, GuiTex.TRACK);
                if (d[0] > 0) ctx.fill(barLeft, y + 3, barLeft + (int) Math.max(2, barMax * d[0] / max), y + 6, GuiTex.GREEN);
                if (d[1] > 0) ctx.fill(barLeft, y + 6, barLeft + (int) Math.max(2, barMax * d[1] / max), y + 9, GuiTex.RED);
                boolean empty = d[0] == 0 && d[1] == 0; // 거래 없는 날은 회색 "-"
                String v = empty ? "-" : GoldFormat.signed(d[0] - d[1]);
                ctx.text(font, v, right - font.width(v), y + 2,
                        empty ? GuiTex.LABEL : d[0] - d[1] >= 0 ? GuiTex.GREEN : GuiTex.RED, false);
                y += ROW_H;
            }
        }
    }

    /** 수입 내역 / 지출 내역(금액 큰 순, 최대 10줄). 제목 줄 오른쪽 "‹ 날짜별"로 돌아간다. */
    private void itemList(GuiGraphicsExtractor ctx, CategoryView.Result r, List<CategoryView.Item> items,
                          boolean income, int left, int right, int y) {
        ctx.text(font, income ? "수입 내역" : "지출 내역", left, y + 1, GuiTex.LABEL, false);
        String back = "‹ 날짜별";
        clickText(ctx, back, right - font.width(back), y + 1, GuiTex.TEXT, 0);
        y += 14;
        if (items.isEmpty()) {
            ctx.text(font, income ? "이 기간에 들어온 돈이 없어요" : "이 기간에 나간 돈이 없어요",
                    left, y + 2, GuiTex.LABEL, false);
            return;
        }
        for (var it : items) {
            GuiTex.sprite(ctx, GuiTex.icon(r.category()), left, y, 12, 12);
            String amt = (income ? "+" : "-") + GoldFormat.format(it.amount()) + "  (" + it.count() + "건)";
            ctx.text(font, font.plainSubstrByWidth(it.label(), right - left - 20 - font.width(amt) - 8),
                    left + 15, y + 2, GuiTex.TEXT, false);
            ctx.text(font, amt, right - font.width(amt), y + 2, income ? GuiTex.GREEN : GuiTex.RED, false);
            y += ROW_H;
        }
    }

    private int catRows(GuiGraphicsExtractor ctx, Map<String, Long> map, int left, int right, int y,
                        long max, int accent) {
        int shown = 0;
        for (Map.Entry<String, Long> e : sortDesc(map).entrySet()) {
            if (shown++ >= 5) break;
            catHits.add(new Object[]{y, e.getKey()});
            GuiTex.sprite(ctx, GuiTex.icon(e.getKey()), left, y, 12, 12); // 12px 카테고리 아이콘
            int labelLeft = left + 15;
            String label = font.plainSubstrByWidth(e.getKey(), 60);
            ctx.text(font, label, labelLeft, y + 2, GuiTex.LABEL, false);
            int barLeft = left + 80;
            int barMax = right - barLeft - 76;
            int bw = (int) Math.max(2, barMax * e.getValue() / max);
            ctx.fill(barLeft, y + 3, barLeft + barMax, y + 9, GuiTex.TRACK);
            ctx.fill(barLeft, y + 3, barLeft + bw, y + 9, accent);
            String amt = GoldFormat.format(e.getValue());
            ctx.text(font, amt, right - font.width(amt), y + 2, GuiTex.TEXT, false);
            y += ROW_H;
        }
        return y;
    }

    private static Map<String, Long> sortDesc(Map<String, Long> m) {
        Map<String, Long> out = new LinkedHashMap<>();
        m.entrySet().stream()
                .sorted((a, b) -> Long.compare(b.getValue(), a.getValue()))
                .forEach(e -> out.put(e.getKey(), e.getValue()));
        return out;
    }

    // ── 주간 탭 ──
    private void renderWeek(GuiGraphicsExtractor ctx, int x, int y) {
        List<DailyBucket> days = aggregator.lastDays(7);
        int left = x + PAD, right = x + W - PAD;
        // 수입·지출을 같이 보여준다(2026-07-28 요청) — 가운데 축 기준 오른쪽=수입, 왼쪽=지출.
        // 막대 기준은 순익이 아니라 수입/지출 각각의 최대값이라 규모 비교가 된다.
        long maxAbs = 1, totIn = 0, totOut = 0;
        for (DailyBucket d : days) {
            maxAbs = Math.max(maxAbs, Math.max(d.income, d.expense));
            totIn += d.income;
            totOut += d.expense;
        }

        int cx = left + 120;
        int barMax = 70;
        for (DailyBucket d : days) {
            long net = d.netPnl();
            ctx.text(font, d.date.format(DAY_FMT), left, y + 2, GuiTex.LABEL, false);
            ctx.fill(cx, y + 1, cx + 1, y + 11, GuiTex.RULE);
            if (d.income > 0) {
                int bw = Math.max((int) (barMax * d.income / maxAbs), 1);
                ctx.fill(cx + 1, y + 2, cx + 1 + bw, y + 6, GuiTex.GREEN);   // 위: 수입
            }
            if (d.expense > 0) {
                int bw = Math.max((int) (barMax * d.expense / maxAbs), 1);
                ctx.fill(cx - bw, y + 6, cx, y + 10, GuiTex.RED);            // 아래: 지출
            }
            // 오른쪽: 순익(맨 끝) + 그날 지출(빨강, 바로 왼쪽) — 빨간 막대가 얼마인지 숫자로도 표시
            String v = GoldFormat.signed(net);
            int vc = net > 0 ? GuiTex.GREEN : (net < 0 ? GuiTex.RED : GuiTex.NEUTRAL);
            ctx.text(font, v, right - font.width(v), y + 2, vc, false);
            if (d.expense > 0) {
                String ex = "-" + GoldFormat.format(d.expense);
                int exX = right - font.width(v) - 8 - font.width(ex);
                ctx.text(font, ex, exX, y + 2, GuiTex.RED, false);
            }
            y += ROW_H + 1;
        }

        // 범례 — 막대 두 개가 각각 무엇인지
        ctx.fill(cx + 1, y + 3, cx + 9, y + 7, GuiTex.GREEN);
        ctx.text(font, "수입", cx + 12, y + 1, GuiTex.LABEL, false);
        ctx.fill(cx + 40, y + 3, cx + 48, y + 7, GuiTex.RED);
        ctx.text(font, "지출", cx + 51, y + 1, GuiTex.LABEL, false);
        y += ROW_H;

        y += 4;
        GuiTex.tileH(ctx, "tex_divider", left, right, y, 48, 6);
        y += 5;
        long totNet = totIn - totOut;
        ctx.text(font, "7일 합계", left, y, GuiTex.LABEL, false);
        String sum = "+" + GoldFormat.format(totIn) + " / -" + GoldFormat.format(totOut)
                + "  =  " + GoldFormat.signed(totNet);
        int sc = totNet >= 0 ? GuiTex.GREEN : GuiTex.RED;
        ctx.text(font, sum, right - font.width(sum), y, sc, false);
    }

    // ── 내역 탭 (오늘 전체 거래, 카테고리 표시, 휠 스크롤, 최신 위) ──
    /** 표시용 캐시 — 스크롤·렌더 양쪽에서 같은 그룹핑 결과를 쓰도록 한 프레임 동안 재사용. */
    private List<RecordGrouping.Grouped> groupedPending() {
        return RecordGrouping.collapseConsecutive(aggregator.recent());
    }

    private void renderPending(GuiGraphicsExtractor ctx, int x, int y) {
        List<RecordGrouping.Grouped> p = groupedPending(); // 연속 동일거래 묶음("×N건") — 도배 방지
        int left = x + PAD, right = x + W - PAD;
        if (p.isEmpty()) {
            ctx.text(font, "오늘 거래 내역이 없습니다.", left, y + 4, GuiTex.LABEL, false);
            return;
        }
        int total = p.size();
        int maxScroll = Math.max(0, total - VISIBLE_PENDING);
        pendingScroll = Math.max(0, Math.min(pendingScroll, maxScroll));
        int start = pendingScroll;
        int end = Math.min(total, start + VISIBLE_PENDING);

        int y0 = y;
        boolean scrollable = total > VISIBLE_PENDING;
        int rowRight = scrollable ? right - 10 : right;
        for (int i = start; i < end; i++) {
            RecordGrouping.Grouped r = p.get(total - 1 - i); // 최신이 위로
            boolean plus = r.kind() == TransactionRecord.Kind.INCOME || r.kind() == TransactionRecord.Kind.TRANSFER_IN;
            int color = switch (r.kind()) {
                case INCOME -> GuiTex.GREEN;
                case EXPENSE -> GuiTex.RED;
                default -> GuiTex.BLUE; // 이체
            };
            GuiTex.sprite(ctx, GuiTex.icon(r.category()), left, y, 12, 12); // 12px 카테고리 아이콘
            int textLeft = left + 15;
            boolean unknown = r.amount() == 0; // 금액 미확인(0원) — 클릭해서 금액을 넣으면 된다
            String amt = (unknown ? "금액 미확인" : (plus ? "+" : "-") + GoldFormat.format(r.amount()))
                    + (r.qty() > 0 ? " (" + r.qty() + "개)" : "");
            ctx.text(font, amt, textLeft, y + 2, unknown ? GuiTex.NEUTRAL : color, false);
            int amtW = font.width(amt);
            String cat = r.category() == null ? "" : r.category();
            String lbl = r.label() == null || r.label().isEmpty() || r.label().equals(cat) ? "" : "  " + r.label();
            String mult = r.count() > 1 ? "  ×" + r.count() : "";
            String catLine = font.plainSubstrByWidth(cat + lbl + mult, rowRight - textLeft - amtW - 8);
            ctx.text(font, catLine, rowRight - font.width(catLine), y + 2, GuiTex.LABEL, false);
            y += ROW_H;
        }

        if (scrollable) {
            int trackTop = y0, trackH = VISIBLE_PENDING * ROW_H;
            int barX = right - 8;
            GuiTex.tileV(ctx, "tex_scroll_track", barX, trackTop, trackTop + trackH, 8, 16);
            int thumbH = Math.max(16, trackH * VISIBLE_PENDING / total);
            int thumbY = trackTop + (int) ((long) (trackH - thumbH) * start / maxScroll);
            GuiTex.sprite(ctx, "tex_scroll_thumb", barX, thumbY, 8, thumbH);
            String pos = (start + 1) + "–" + end + " / " + total + "  · 휠 스크롤";
            ctx.text(font, pos, left, y + 2, GuiTex.LABEL, false);
        }
        // 잘못 들어간 기록만 지우는 방법 안내(2026-07-28)
        String tip = "클릭 = 수정 · Shift+클릭 = 삭제";
        ctx.text(font, tip, right - font.width(tip), y + 2, GuiTex.LABEL, false);
    }

    @Override
    public boolean mouseScrolled(double mx, double my, double horiz, double vert) {
        if (tab == 0 && statsOpen && detailCat == null) {
            statsScroll -= (int) Math.signum(vert);
            return true;
        }
        if (tab == 2) {
            pendingScroll -= (int) Math.signum(vert);
            int maxScroll = Math.max(0, groupedPending().size() - VISIBLE_PENDING);
            pendingScroll = Math.max(0, Math.min(pendingScroll, maxScroll));
            return true;
        }
        return super.mouseScrolled(mx, my, horiz, vert);
    }

    // ── 관리 탭 (수동 입력 + 오늘 초기화) ──
    private void renderManage(GuiGraphicsExtractor ctx, int x, int y) {
        int left = x + PAD;
        if (editing != null) {
            ctx.text(font, "금액", left, y, GuiTex.LABEL, false);
            ctx.text(font, "카테고리", x + PAD + 116, y, GuiTex.LABEL, false);
            ctx.text(font, "설명", x + PAD + 220, y, GuiTex.LABEL, false);
            GuiTex.sprite(ctx, "tex_input", x + PAD, y + 12, 110, 18);
            GuiTex.sprite(ctx, "tex_input", x + PAD + 116, y + 12, 100, 18);
            GuiTex.sprite(ctx, "tex_input", x + PAD + 220, y + 12, W - PAD * 2 - 220, 18);
            ctx.text(font, font.plainSubstrByWidth("원래  " + editOriginal, W - PAD * 2),
                    left, y + 60, GuiTex.TEXT, false);
            ctx.text(font, "고친 값은 비고에 원래 값과 함께 남습니다.", left, y + 72, GuiTex.LABEL, false);
            return;
        }
        // 반투명 placeholder 대신 박스 위에 또렷한 라벨을 따로 그림(가독성).
        ctx.text(font, "금액", left, y, GuiTex.LABEL, false);
        ctx.text(font, "설명 (선택)", x + PAD + 116, y, GuiTex.LABEL, false);
        GuiTex.sprite(ctx, "tex_input", x + PAD, y + 12, 110, 18);
        GuiTex.sprite(ctx, "tex_input", x + PAD + 116, y + 12, W - PAD * 2 - 116, 18);
        ctx.text(font, "'수동' 카테고리로 오늘 집계에 반영됩니다.", left, y + 58, GuiTex.LABEL, false);

        // 날짜 지정 초기화 — 잘못 기록된 특정 날짜만 지울 때
        ctx.text(font, "날짜  예) 2026-07-28 · 비우면 오늘", left, y + 90, GuiTex.LABEL, false);
        GuiTex.sprite(ctx, "tex_input", x + PAD, y + 102, 110, 18);
        ctx.text(font, "내역 탭: 클릭하면 수정, Shift+클릭하면 삭제됩니다.",
                left, y + 124, GuiTex.LABEL, false);
    }

    // ── 설정 탭 (HUD 위치 · 설정 열기) ──
    private void renderSettings(GuiGraphicsExtractor ctx, int x, int y) {
        int left = x + PAD;
        ctx.text(font, "HUD 위치·표시 및 상세 설정을 여기서 엽니다.", left, y, GuiTex.LABEL, false);
        // 버튼(y+14, y+40) 은 위젯이 렌더
        String pct = "HUD 투명도  " + Math.round(config.hudOpacity * 100) + "%";
        int tw = font.width(pct);
        ctx.text(font, pct, x + (W - tw) / 2, y + 71, GuiTex.TEXT, false);
        // -/+ 버튼(y+66) 은 위젯이 렌더
    }
}

package kr.aka.nakin.config;

import kr.aka.nakin.aggregate.DailyAggregator;
import java.util.List;
import java.util.stream.IntStream;
import java.util.function.Consumer;
import java.util.function.Supplier;

/** YACL 없이 사용하는 윤띵 설정 모델. 화면과 저장 로직을 분리해 값 검증도 한곳에서 한다. */
public final class LedgerSettings {
    public static final List<String> PAGES = List.of("영수증", "집계", "플리마켓", "고급·진단");
    private final DtConfig c;
    private final DailyAggregator aggregator;

    public LedgerSettings(DtConfig config, DailyAggregator aggregator) {
        this.c = config;
        this.aggregator = aggregator;
    }

    public List<Entry> page(int page) {
        return switch (page) {
            case 0 -> List.of(
                    bool("영수증 표시", "화면의 영수증 HUD를 표시합니다.", () -> c.hudEnabled, v -> c.hudEnabled = v),
                    rows("수입 분야 표시", () -> c.hudIncomeRows, v -> c.hudIncomeRows = v),
                    rows("지출 분야 표시", () -> c.hudExpenseRows, v -> c.hudExpenseRows = v),
                    bool("송금 내역 표시", "다른 유저와 주고받은 송금을 참고 항목으로 표시합니다.", () -> c.showTransfers, v -> c.showTransfers = v));
            case 1 -> List.of(
                    integer("하루 기준 시각", "0~23시. 변경하면 기존 기록도 새 기준으로 다시 집계합니다.", () -> c.dayResetHour, v -> c.dayResetHour = v, 0, 23),
                    bool("송금 손익 제외", "다른 유저와 주고받은 송금을 손익에서 제외합니다.", () -> c.transferExcludedFromPnl, v -> c.transferExcludedFromPnl = v),
                    bool("수수료 지출 포함", "별도 수수료를 지출로 기록합니다.", () -> c.feeCountedAsExpense, v -> c.feeCountedAsExpense = v),
                    bool("미확인 거래 기록", "금액을 확정하지 못한 거래도 남깁니다.", () -> c.recordUnresolved, v -> c.recordUnresolved = v));
            case 2 -> List.of(
                    feePercent());
            case 3 -> List.of(
                    bool("자동 재화 감지", "OBT에서 확인한 쿠키 입출금을 기록합니다. 변경 즉시 적용됩니다.", () -> c.automaticCurrencyTracking, v -> c.automaticCurrencyTracking = v),
                    bool("퀘스트·의뢰 보상 감지", "퀘스트와 의뢰에서 받은 쿠키 보상을 감지합니다.", () -> c.girinQuestTracking, v -> c.girinQuestTracking = v),
                    bool("진단 로거", "재실행 후 적용됩니다. 평소에는 꺼두세요.", () -> c.debugProbe, v -> c.debugProbe = v));
            default -> throw new IllegalArgumentException("알 수 없는 설정 페이지");
        };
    }

    private Entry bool(String label, String desc, Supplier<Boolean> get, Consumer<Boolean> set) {
        return new Entry(label, desc, List.of("켜짐", "꺼짐"), () -> get.get() ? "켜짐" : "꺼짐", v -> set.accept("켜짐".equals(v)));
    }
    private Entry rows(String label, Supplier<Integer> get, Consumer<Integer> set) {
        return new Entry(label, "전체·숨김·1~10개 중 선택합니다.", List.of("숨김", "1개", "2개", "3개", "4개", "5개", "6개", "7개", "8개", "9개", "10개", "전체"),
                () -> get.get() < 0 ? "전체" : get.get() == 0 ? "숨김" : get.get() + "개",
                v -> set.accept("전체".equals(v) ? -1 : "숨김".equals(v) ? 0 : Integer.parseInt(v.replace("개", ""))));
    }
    private Entry integer(String label, String desc, Supplier<Number> get, Consumer<Integer> set, int min, int max) {
        List<String> values = IntStream.rangeClosed(min, max).mapToObj(String::valueOf).toList();
        return new Entry(label, desc, values, () -> String.valueOf(get.get()), v -> { int n = Integer.parseInt(v); if (n < min || n > max) throw new IllegalArgumentException(); set.accept(n); });
    }
    private Entry feePercent() {
        return new Entry("판매 수수료율", "플리마켓 정산함에서 판매 대금을 받을 때 차감되는 수수료율입니다.",
                List.of("1%", "2%", "3%"),
                () -> Math.round(c.fleaSaleFeePercent) + "%",
                v -> c.fleaSaleFeePercent = Integer.parseInt(v.replace("%", "")));
    }

    public final class Entry {
        public final String label, description;
        public final List<String> choices;
        private final Supplier<String> get;
        private final Consumer<String> set;
        private String error;
        Entry(String label, String description, List<String> choices, Supplier<String> get, Consumer<String> set) {
            this.label = label; this.description = description; this.choices = choices; this.get = get; this.set = set;
        }
        public String value() { return get.get(); }
        public String error() { return error; }
        public boolean next() { return choices.isEmpty() ? false : apply(choices.get((choices.indexOf(value()) + 1) % choices.size())); }
        public boolean apply(String value) {
            int oldHour = c.dayResetHour;
            try { set.accept(value == null ? "" : value.trim()); c.save(); error = null; }
            catch (RuntimeException ex) { error = "입력값을 확인하세요"; return false; }
            if (aggregator != null && oldHour != c.dayResetHour) aggregator.rebuild();
            return true;
        }
    }
}

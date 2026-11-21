package kr.aka.nakin.core;

import kr.aka.nakin.core.TradeSignal.Flow;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.function.Function;
import java.util.function.Supplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static kr.aka.nakin.core.DongleCategory.*;

/**
 * 동글랜드 채팅 한 줄 → 거래 신호(0개 이상).
 *
 * <p>규칙 원문은 전부 사용자 로그 실측(2026-07-09 ~ 09-28, docs/원문_분석_요약.md).
 * 판정 순서: <b>유저 대화 채널 무시 → 여러 줄 블록 이어받기 → 한 줄 규칙</b>.
 *
 * <p><b>여러 줄 블록</b> — 동글랜드는 한 거래를 여러 줄로 보낸다. 금액 줄만 보면 방향을 알 수 없어
 * 직전 머리 줄을 기억하는 작은 상태를 둔다.
 * <ul>
 *   <li>직거래: {@code X님과의 거래가 완료되었습니다!} 뒤 {@code - N쿠키}(준 돈) / {@code + N쿠키}(받은 돈, 수수료 차감 후)</li>
 *   <li>플리마켓 영수증: {@code l 성공적으로 판매했습니다:}/{@code 구매했습니다:} 뒤 {@code l N개의 X (총 N쿠키)}</li>
 *   <li>판매 영수증: {@code [판매 완료]} 뒤 품목 줄들, 마지막 {@code 합계: N쿠키} 한 줄만 센다</li>
 * </ul>
 *
 * <p><b>남의 행동도 뜨는 문구</b>(마을 금고 입금, 마을 은행 인출)는
 * 내 닉네임일 때만 기록한다. 닉네임을 아직 모르면 "내 잔고가 움직였을 때만"(requireDelta)으로 넘긴다.
 */
public final class CurrencyParser {

    /** 금액: 쉼표·소수 허용(플리마켓은 "7952.0쿠키", "10453.579999999783쿠키"처럼 소수가 온다). */
    private static final String AMT = "([0-9][0-9,]*(?:\\.[0-9]+)?)";

    /** 머리 줄 뒤에 금액 줄이 이만큼 안에 와야 같은 블록으로 본다. */
    private static final int BLOCK_MAX_LINES = 8;
    private static final long BLOCK_MAX_MS = 3_000;

    // ── 무시: 유저 대화 채널 ──
    private static final Pattern PLAYER_CHAT = Pattern.compile(
            "\\[전챗\\]"                                       // 전체채팅(앞에 아이콘 글리프)
          + "|^\\[지역채팅\\]"
          + "|^<[^>]{1,24}> "                                  // 바닐라 형식 대화 "<닉> …"
          + "|^\\[[^\\]]{1,12}\\] <"                           // [눈꽃] <닉> · [구름] <닉> 등 채널
          + "|^(?:마을원|마을장|부마을장) \\S+ : "              // 마을 채팅
          + "|\\S+ → \\S+ \\|"                                 // 귓속말(닉 → 나 | …)
          + "|님이 질문을 등록|님의 질문|답변이 등록"
          + "|^\\[가이드\\]");

    private record Rule(Pattern pattern, Function<Matcher, List<TradeSignal>> mapper) {}

    private final GirinCurrencyParser girin = new GirinCurrencyParser();
    private final List<Rule> rules = new ArrayList<>();
    private final Supplier<String> selfName;

    /**
     * 무역 카드팩 2·3장째 뒤집기 가격 후보. 기본값은 위키 표(2장 3만 / 3장 10만 — VVIP 는 2장 무료).
     * /무역카드팩 을 치면 뜨는 "카드를 뒤집을 땐 [1장:무료 / 2장:3만쿠키 / 3장:10만쿠키]" 줄로 그 사람
     * 등급에 맞는 값으로 갱신한다. 무료 장은 잔고가 안 움직여 기록되지 않으니 등급을 알 필요가 없다.
     */
    private volatile long[] cardCosts = {30_000, 100_000};
    /** 유물 감정 비용 — 고정 1,000쿠키(사용자 확인 2026-09-28, 로그 "[감정] 돈이 부족합니다: 1,000쿠키"). */
    static final long APPRAISE_COST = 1_000;
    private static final Pattern CARD_PRICE = Pattern.compile("([0-9][0-9,.]*)\\s*(만)?\\s*쿠키");

    // 블록 상태
    private enum Block { NONE, DIRECT_TRADE, FLEA_SOLD, FLEA_BOUGHT, SALE_RECEIPT }
    private Block block = Block.NONE;
    private String blockPartner = "";
    private int blockLines;
    private long blockStartMs;
    private final List<String> receiptItems = new ArrayList<>();

    /** @param selfName 내 닉네임 공급자(접속 전이면 null 반환 가능). null 이면 항상 "모름"으로 취급. */
    public CurrencyParser(Supplier<String> selfName) {
        this.selfName = selfName != null ? selfName : () -> null;
        registerRules();
    }

    public static CurrencyParser createDefault() {
        return new CurrencyParser(null);
    }

    /** 유저 대화 채널 줄인가 — 제보 기록에 남의 대화를 넣지 않으려고 밖에서도 쓴다. */
    public static boolean isPlayerChat(String rawLine) {
        return rawLine != null && PLAYER_CHAT.matcher(normalize(rawLine)).find();
    }

    public List<TradeSignal> parse(String line) {
        return parse(line, System.currentTimeMillis());
    }

    public synchronized List<TradeSignal> parse(String rawLine, long nowMs) {
        List<TradeSignal> out = new ArrayList<>(1);
        if (rawLine == null) return out;
        String line = normalize(rawLine);
        if (line.isEmpty()) return out;
        if (PLAYER_CHAT.matcher(line).find()) return out; // 대화는 블록 상태도 건드리지 않는다

        List<TradeSignal> confirmed = girin.parse(rawLine, nowMs);
        if (confirmed != null) {
            endBlock();
            return confirmed;
        }

        // 1) 여러 줄 블록 이어받기
        if (block != Block.NONE) {
            blockLines++;
            if (blockLines > BLOCK_MAX_LINES || nowMs - blockStartMs > BLOCK_MAX_MS) {
                endBlock();
            } else if (continueBlock(line, rawLine, out)) {
                return out;
            }
        }

        // 2) 블록 머리 줄
        if (startBlock(line, nowMs)) return out;

        // 3) 한 줄 규칙(첫 매칭 1개)
        for (Rule r : rules) {
            Matcher m = r.pattern.matcher(line);
            if (m.find()) {
                List<TradeSignal> sigs = r.mapper.apply(m);
                if (sigs != null) {
                    for (TradeSignal s : sigs) {
                        if (s != null) out.add(new TradeSignal(s.flow, s.category, s.amount, s.qty,
                                s.label, rawLine, s.amountFromDelta, s.requireDelta, s.note, s.expectedCosts));
                    }
                }
                return out;
            }
        }
        return out;
    }

    // ─────────────────────────── 블록 ───────────────────────────

    private static final Pattern DIRECT_HEAD = Pattern.compile("^(\\S+?)님과의 거래가 완료되었습니다!");
    private static final Pattern DIRECT_MONEY = Pattern.compile("^([+-]) " + AMT + "쿠키$");
    private static final Pattern DIRECT_ITEM = Pattern.compile("^[+-] .+ [0-9]+개$");
    private static final Pattern FLEA_HEAD = Pattern.compile("^l 성공적으로 (판매|구매)했습니다:$");
    private static final Pattern FLEA_LINE = Pattern.compile("^l ([0-9]+)개의 (.+?) \\(총 " + AMT + "쿠키\\)$");
    /** 금액 없는 형태 "l 1471개의 레드스톤 블록"(2026-10-03 실서버, 남의 판매 상점에서 구매) → 잔고 변동으로 금액. */
    private static final Pattern FLEA_LINE_NOAMT = Pattern.compile("^l ([0-9]+)개의 (.+)$");
    private static final Pattern RECEIPT_HEAD =Pattern.compile("^\\[판매 완료\\]$");
    private static final Pattern RECEIPT_ITEM = Pattern.compile("^\\[(.+?)\\] x([0-9]+)(.*?) : " + AMT + "쿠키$");
    private static final Pattern RECEIPT_TOTAL = Pattern.compile("^합계: " + AMT + "쿠키$");

    private boolean startBlock(String line, long nowMs) {
        Matcher m;
        if ((m = DIRECT_HEAD.matcher(line)).find()) {
            begin(Block.DIRECT_TRADE, nowMs);
            blockPartner = m.group(1);
            return true;
        }
        if ((m = FLEA_HEAD.matcher(line)).find()) {
            begin("판매".equals(m.group(1)) ? Block.FLEA_SOLD : Block.FLEA_BOUGHT, nowMs);
            return true;
        }
        if (RECEIPT_HEAD.matcher(line).find()) {
            begin(Block.SALE_RECEIPT, nowMs);
            receiptItems.clear();
            return true;
        }
        return false;
    }

    private void begin(Block b, long nowMs) {
        block = b;
        blockLines = 0;
        blockStartMs = nowMs;
    }

    private void endBlock() {
        block = Block.NONE;
        blockPartner = "";
        receiptItems.clear();
    }

    /** @return 이 줄을 블록의 일부로 소비했으면 true */
    private boolean continueBlock(String line, String raw, List<TradeSignal> out) {
        Matcher m;
        switch (block) {
            case DIRECT_TRADE -> {
                if ((m = DIRECT_MONEY.matcher(line)).find()) {
                    long amt = amount(m.group(2));
                    String label = blockPartner + "님과 직거래";
                    if ("-".equals(m.group(1))) {
                        out.add(sig(Flow.EXPENSE, DIRECT_TRADE, amt, 0, label, raw, null));
                    } else {
                        // 받은 쪽 표시는 수수료 3% 가 빠진 실수령액(150,000 → +145,500 실측)
                        out.add(sig(Flow.INCOME, DIRECT_TRADE, amt, 0, label, raw, "수수료 차감 후 실수령"));
                    }
                    return true;
                }
                if (DIRECT_ITEM.matcher(line).find()) return true; // 주고받은 아이템 줄
                endBlock();
                return false;
            }
            case FLEA_SOLD, FLEA_BOUGHT -> {
                if ((m = FLEA_LINE.matcher(line)).find()) {
                    boolean sold = block == Block.FLEA_SOLD;
                    int qty = parseInt(m.group(1));
                    out.add(sig(sold ? Flow.INCOME : Flow.EXPENSE, FLEA, amount(m.group(3)), qty,
                            m.group(2), raw, null));
                    endBlock();
                    return true;
                }
                if ((m = FLEA_LINE_NOAMT.matcher(line)).find()) {
                    boolean sold = block == Block.FLEA_SOLD;
                    out.add(new TradeSignal(sold ? Flow.INCOME : Flow.EXPENSE, FLEA, 0, parseInt(m.group(1)),
                            m.group(2), raw, true, false, sold ? "수수료 차감 후 실수령" : null));
                    endBlock();
                    return true;
                }
                if (line.startsWith("+---")) return true;
                endBlock();
                return false;
            }
            case SALE_RECEIPT -> {
                if ((m = RECEIPT_ITEM.matcher(line)).find()) {
                    receiptItems.add(m.group(1) + (m.group(3).contains("만료") ? "(만료)" : ""));
                    return true;
                }
                if ((m = RECEIPT_TOTAL.matcher(line)).find()) {
                    String first = receiptItems.isEmpty() ? "" : receiptItems.get(0);
                    String label = receiptItems.isEmpty() ? "판매"
                            : first + (receiptItems.size() > 1 ? " 외 " + (receiptItems.size() - 1) + "종" : "");
                    out.add(sig(Flow.INCOME, jobOf(first.replace("(만료)", ""), TRADE_SHOP),
                            amount(m.group(1)), 0, label, raw, null));
                    endBlock();
                    return true;
                }
                endBlock();
                return false;
            }
            default -> { return false; }
        }
    }

    // ─────────────────────────── 한 줄 규칙 ───────────────────────────

    private void rule(String regex, Function<Matcher, List<TradeSignal>> mapper) {
        rules.add(new Rule(Pattern.compile(regex), mapper));
    }

    private void registerRules() {
        // ── 무시(돈 이동 없음 / 남의 거래 / 확인창) — 먼저 걸러 아래 규칙에 새지 않게 ──
        rule("님에게 " + AMT + " ?쿠키을 송금하시겠습니까\\?", m -> List.of());       // 송금 확인창
        rule("^└ 원금 ", m -> List.of());                                            // 송금 상세 줄(본문과 중복)
        rule("^거래소 • .+ " + AMT + "쿠키에 판매 등록했습니다\\.", m -> List.of());      // 등록만, 돈 이동 없음
        rule("^오프라인 동안 [0-9]+개의 아이템이 총 ", m -> List.of());                 // 접속 시 요약 — 수령 때 센다
        rule("^비용은 .+ 상점 주인의 잔액이 부족합니다", m -> List.of());
        rule("^l .+의 (?:[0-9]+개당|개당) 가격 - ", m -> List.of());                    // 플리 상점 정보
        rule("^등록 가격 : |^개당 평균 시세 : ", m -> List.of());
        rule("^\\[길라잡이\\] \\[완료\\]", m -> List.of());                            // "N쿠키 모으기" 퀘스트 완료

        // ── 무역상점 / 변동상점 / NPC ──
        // [동글상점] 보석ㅣ영원 x1 판매에 성공했습니다. 획득 금액: 132 쿠키, 8.97 대장포인트!
        // (같은 문구로 모험포인트·숙련 포인트·전쟁 코인 결제도 오므로 단위 '쿠키' 필수)
        rule("^\\[동글상점\\] (.+?) x([0-9]+) 판매에 성공했습니다\\. 획득 금액: " + AMT + " ?쿠키",
                m -> one(sig(Flow.INCOME, jobOf(m.group(1), TRADE_SHOP), amount(m.group(3)),
                        parseInt(m.group(2)), m.group(1), null, null)));
        // [동글상점] 다이아몬드 x128 구매에 성공했습니다. 비용: 19,200 쿠키!
        rule("^\\[동글상점\\] (.+?) x([0-9]+) 구매에 성공했습니다\\. 비용: " + AMT + " ?쿠키",
                m -> one(sig(Flow.EXPENSE, NPC_SHOP, amount(m.group(3)), parseInt(m.group(2)),
                        m.group(1), null, null)));

        // ── 거래소 ── (대금은 광장에 있을 때만 자동 입금된다 — 사용자 확인 2026-09-28)
        // ① 광장에서 실시간으로 팔림: 체결 알림 한 줄만 오고 돈이 바로 들어온다. N 은 수수료 7% 뺀 실수령액
        //    (27,900 + 2,100 = 등록가 30,000 실측). 단 광장에 들어갈 때는 같은 판매가 '수령' 줄과
        //    체결 알림 1~2줄로 겹쳐 온다(36,166 수령 직후 같은 금액 체결 ×2 실측) → 내 잔고가 실제로
        //    그만큼 늘었을 때만 기록(requireDelta). 월드 이동 직후엔 ΔG 감지가 멈춰 겹친 알림은 버려진다.
        rule("^거래소 • \\S+ 님이 (.+?) x([0-9]+) 을 " + AMT + " ?쿠키에 구매했습니다\\.",
                m -> one(sig(Flow.INCOME, EXCHANGE, amount(m.group(3)), parseInt(m.group(2)), m.group(1), null,
                        "수수료 7% 차감 후").requiringDelta()));
        // ② 광장 밖(마을 등)에서 팔린 것: 광장에 들어가면 "오프라인 동안…" 요약 + 수령 줄과 함께 입금
        rule("^거래소 • " + AMT + "쿠키을 수령했습니다\\.",
                m -> one(sig(Flow.INCOME, EXCHANGE, amount(m.group(1)), 0, "판매 대금 수령", null,
                        "수수료 7% 차감 후")));
        // 거래소 • 원석ㅣ아메트린 x10 을 20,000에 구매했습니다.   ← '쿠키' 글자가 없다
        rule("^거래소 • (.+?) x([0-9]+) 을 " + AMT + "에 구매했습니다\\.",
                m -> one(sig(Flow.EXPENSE, EXCHANGE, amount(m.group(3)), parseInt(m.group(2)),
                        m.group(1), null, null)));

        // ── 송금(/pay) — 보내는 쪽은 원금 전액, 받는 쪽은 3% 차감된 실수령액 ──
        rule("^(\\S+?)에게 " + AMT + "쿠키을 송금했습니다\\.",
                m -> one(sig(Flow.EXPENSE, TRANSFER, amount(m.group(2)), 0, m.group(1) + "에게 송금", null, null)));
        rule("^(\\S+?)에게서 " + AMT + "쿠키을 받았습니다\\.",
                m -> one(sig(Flow.INCOME, TRANSFER, amount(m.group(2)), 0, m.group(1) + "에게서 받음", null,
                        "수수료 차감 후 실수령")));

        // ── 플리마켓(내 상점) ──
        // 남이 내 판매 상점에서 삼 → 수입
        rule("^(\\S+) 님이 당신의 상점 ?에서 ([0-9]+) (.+?) 을\\(를\\) 구입했으며 당신은 " + AMT + "쿠키",
                m -> one(sig(Flow.INCOME, FLEA, amount(m.group(4)), parseInt(m.group(2)), m.group(3), null, null)));
        // 금액 없는 짧은 형태 "PlayerA 님이 당신의 상점 에서 2 다이아몬드"(2026-10-01 실서버, 직후 ΔG +297
        //   = 수수료 5% 뗀 실수령) → 잔고 변동으로 금액. 다음 줄 "32, 106, -50 에 있는 당신의 가게의 물품 …"은 위치 안내.
        rule("^(\\S+) 님이 당신의 상점 ?에서 ([0-9]+) (.+)$",
                m -> one(new TradeSignal(Flow.INCOME, FLEA, 0, parseInt(m.group(2)), m.group(3), null,
                        true, false, "수수료 차감 후 실수령")));
        // 남이 내 구매 상점에 팖 → 내가 산 것(지출). 원문이 영어다.
        rule("^(\\S+) sold ([0-9]+) (.+?) to your shop for " + AMT + "쿠키",
                m -> one(sig(Flow.EXPENSE, FLEA, amount(m.group(4)), parseInt(m.group(2)), m.group(3), null, null)));
        // 금액 없는 짧은 형태 "PlayerA sold 2 다이아몬드"(2026-10-01 실서버, 직후 ΔG −328) → 잔고 변동으로 금액.
        //   잔고 변동이 없으면 금액 미확인으로 남기고 알린다 — 내가 없을 때 체결되면 돈은 이미 빠졌고 알림만
        //   접속 후에 온다("PlayerA sold 1728 다이아몬드", 2026-10-05 실서버, 직후 ΔG 없음).
        rule("^(\\S+) sold ([0-9]+) (.+)$",
                m -> one(new TradeSignal(Flow.EXPENSE, FLEA, 0, parseInt(m.group(2)), m.group(3), null,
                        true, false, null)));

        // ── 낚시 ──
        rule("• 총 ([0-9]+)마리를 판매하여 " + AMT + "쿠키을 벌었습니다",
                m -> one(sig(Flow.INCOME, FISHING, amount(m.group(2)), parseInt(m.group(1)), "물고기 판매", null, null)));

        // ── 무역 카드팩 ──
        // 가격표 줄(돈 이동 없음) — 내 등급의 뒤집기 가격을 배운다
        rule("카드를 뒤집을 땐 \\[(.+)\\]이 소모됩니다", m -> {
            List<Long> costs = new ArrayList<>();
            Matcher c = CARD_PRICE.matcher(m.group(1));
            while (c.find()) {
                BigDecimal v = decimal(c.group(1));
                if (c.group(2) != null) v = v.multiply(BigDecimal.valueOf(10_000));
                long n = v.setScale(0, RoundingMode.HALF_UP).longValue();
                if (n > 0) costs.add(n);
            }
            if (!costs.isEmpty()) cardCosts = costs.stream().mapToLong(Long::longValue).toArray();
            return List.of();
        });
        // 한 장 뒤집을 때마다 결과가 한 줄 뜬다. 쿠키이 나오면 그 수입 + 뒤집기 비용 후보.
        rule("^\\[무역 카드팩\\] " + AMT + " ?쿠키을 뽑았습니다",
                m -> List.of(sig(Flow.INCOME, REWARD, amount(m.group(1)), 0, "무역 카드팩", null, null),
                        TradeSignal.costHint(CARD_PACK, "카드팩 뒤집기", cardCosts, null)));
        rule("^\\[무역 카드팩\\] .+ 뽑았습니다",
                m -> one(TradeSignal.costHint(CARD_PACK, "카드팩 뒤집기", cardCosts, null)));

        // ── 유물 감정(고정 1,000쿠키) — 성공 문구가 로그에 없어 [감정] 안내가 뜬 뒤 정확히 1,000 빠지면 ──
        rule("^\\[감정\\]",
                m -> one(TradeSignal.costHint(SERVICE, "유물 감정", new long[]{APPRAISE_COST}, null)));

        // ── 보상 ──
        rule("^" + AMT + "쿠키을 획득했습니다\\.",
                m -> one(sig(Flow.INCOME, REWARD, amount(m.group(1)), 0, "보스 보상", null, null)));
        rule("^\\[이벤트\\] 이벤트 코인 ([0-9]+)개 판매 완료! \\(개당 " + AMT + "쿠키\\)",
                m -> {
                    int n = parseInt(m.group(1));
                    return one(sig(Flow.INCOME, REWARD, Math.round(decimal(m.group(2)).doubleValue() * n), n,
                            "이벤트 코인 판매", null, null));
                });
        rule("^\\[이벤트\\] " + AMT + "쿠키 교환 완료",
                m -> one(sig(Flow.INCOME, REWARD, amount(m.group(1)), 0, "이벤트 교환", null, null)));
        rule("^\\[길라잡이\\] 보상금 " + AMT + "원이 지급되었습니다",
                m -> one(sig(Flow.INCOME, REWARD, amount(m.group(1)), 0, "길라잡이 보상금", null, null)));

        // ── 이용료($ 표기 — 서버 경제 플러그인 기본 표기, 쿠키과 같은 돈) ──
        rule("^동글워프 » +\\$" + AMT + "가 차감되었습니다",
                m -> nonZero(sig(Flow.EXPENSE, SERVICE, amount(m.group(1)), 0, "워프", null, null)));
        rule("^\\[보호\\] 오브젝트를 성공적으로 보호했습니다\\. 비용: \\$" + AMT,
                m -> nonZero(sig(Flow.EXPENSE, SERVICE, amount(m.group(1)), 0, "상자 잠금", null, null)));

        // ── 마을 금고/은행 — 마을원 전원에게 방송된다 → 내 닉일 때만 ──
        rule("^금고 \\| (\\S+?)님이 마을 금고에 " + AMT + "쿠키을\\(를\\) 입금했습니다",
                m -> self(m.group(1), sig(Flow.EXPENSE, VILLAGE, amount(m.group(2)), 0, "마을 금고 입금", null, null)));
        rule("^은행 \\| (\\S+?)님이 마을 은행에서 \\$" + AMT + "을\\(를\\) 인출했습니다",
                m -> self(m.group(1), sig(Flow.INCOME, VILLAGE, amount(m.group(2)), 0, "마을 은행 인출", null, null)));
        rule("^금고 \\| ", m -> List.of()); // 업그레이드에 금고 돈 사용 등 — 개인 지갑 무관

        // ── 금액이 채팅에 없는 지출(잔고 변동으로 금액 확정) ──
        rule("^\\[인챈트\\] 인챈트에 성공했습니다",
                m -> one(TradeSignal.byDelta(Flow.EXPENSE, ENCHANT, "모루 인챈트", null)));
        rule("^전직에 성공하였습니다",
                m -> one(TradeSignal.byDelta(Flow.EXPENSE, JOB_CHANGE, "전직", null)));

        // ── 금액이 채팅에 없는 수입 — 추천·출석 보상(1,000쿠키 등)은 우편함으로 와서 수령 문구엔 금액이 없다.
        //    15초 안에 잔고가 늘면 그 금액을 보상으로, 아이템만 받았으면(잔고 그대로) 아무것도 안 남는다.
        rule("^[0-9]+개의 아이템을 수령했습니다",
                m -> one(TradeSignal.byDelta(Flow.INCOME, REWARD, "우편 수령", null).requiringDelta()));
        // "[추천보상] <닉>님이 추천보상을 획득했습니다!" — 모두에게 방송된다. 내 닉일 때만. 1,000쿠키 날은
        //   바로 잔고가 늘고(2026-10-02 실측 +1,000), 아이템 날은 우편으로 가서 잔고가 그대로다.
        rule("^\\[추천보상\\] (\\S+?)님이 추천보상을 획득했습니다",
                m -> self(m.group(1), TradeSignal.byDelta(Flow.INCOME, REWARD, "추천 보상", null).requiringDelta()));
        rule("^\\[동글 패스\\] 보상을 수령하였습니다",
                m -> one(TradeSignal.byDelta(Flow.INCOME, REWARD, "동글 패스", null).requiringDelta()));
        // +5강 이상 성공 서버 방송은 기록에 쓰지 않는다. 생활장비 강화는 대장간 창의 비용 줄
        // ("- 120000원")과 잔고 변동을 맞춰 성공·실패 모두 잡는다(GuiCostLore). 방송까지 ΔG 신호로
        // 만들면 같은 잔고 변동을 두고 다투고, 15초 창에 섞인 다른 지출을 가져갈 위험만 늘어난다.
        rule("^\\[!\\] \\S+?님께서 [0-9]+->[0-9]+강 강화에 성공하셨습니다", m -> List.of());
    }

    // ─────────────────────────── 도우미 ───────────────────────────

    /** 색코드·앞머리 아이콘 글리프를 걷고 양끝 공백 정리. 본문 안쪽 공백은 그대로 둔다. */
    static String normalize(String s) {
        String t = s.replaceAll("§.", "");
        t = t.replaceFirst("^[\\s\\uE000-\\uF8FF\\uA000-\\uA4CF]+", "");
        return t.strip();
    }

    private List<TradeSignal> self(String who, TradeSignal s) {
        String me = selfName.get();
        if (me == null || me.isBlank()) return one(s.requiringDelta()); // 모르면 내 잔고가 움직였을 때만
        return Objects.equals(me, who) ? one(s) : List.of();
    }

    private static TradeSignal sig(Flow flow, String cat, long amount, int qty, String label,
                                   String raw, String note) {
        return new TradeSignal(flow, cat, amount, qty, label, raw, false, false, note);
    }

    private static List<TradeSignal> one(TradeSignal s) {
        return List.of(s);
    }

    private static List<TradeSignal> nonZero(TradeSignal s) {
        return s.amount > 0 ? List.of(s) : List.of();
    }

    static BigDecimal decimal(String s) {
        return new BigDecimal(s.replace(",", ""));
    }

    /** 쿠키 금액 → long(반올림). 플리마켓 소수 금액은 1쿠키 단위로 맞춘다. */
    static long amount(String s) {
        return decimal(s).setScale(0, RoundingMode.HALF_UP).longValueExact();
    }

    private static int parseInt(String s) {
        try {
            return Integer.parseInt(s.replace(",", ""));
        } catch (NumberFormatException e) {
            return 0;
        }
    }
}

package kr.aka.nakin.core;

import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import kr.aka.nakin.core.TradeSignal.Flow;

/** Confirmed Girinworld OBT system messages (2026-11-21); no guessed rewards or fees. */
public final class GirinCurrencyParser {
    private static final Pattern CLOCK = Pattern.compile("^\\[\\d{2}:\\d{2}:\\d{2}\\]\\s*");
    private static final Pattern MONEY = Pattern.compile("^([0-9][0-9,]*)\\s*쿠키\\s*\\(이\\)가\\s*(입금|출금)되었습니다\\.\\s*\\(잔액:\\s*([0-9][0-9,]*)\\)$");
    private static final Pattern SHOP = Pattern.compile("^\\[상점\\]\\s+(.+?)\\(을\\)를\\s+([0-9][0-9,]*)개\\s+(구매|판매)하여\\s+([0-9][0-9,]*)\\s*쿠키\\s*\\(이\\)가\\s*(입금|출금)되었습니다\\.\\s*\\(잔액:\\s*([0-9][0-9,]*)\\)$");
    private static final Pattern TOTAL = Pattern.compile("^\\[상점\\]\\s*모두 판매\\s*·\\s*([0-9][0-9,]*)종\\s*([0-9][0-9,]*)개를 판매하여\\s*([0-9][0-9,]*)\\s*쿠키\\s*\\(이\\)가 입금되었습니다\\.$");
    private long saleAmount, saleQty, lastSale = Long.MIN_VALUE;
    private int saleRows;

    /** null means not this protocol; an empty list means a consumed non-transaction summary. */
    public List<TradeSignal> parse(String raw, long now) {
        try { return parseChecked(raw, now); }
        catch (NumberFormatException | ArithmeticException e) { return List.of(); }
    }

    private List<TradeSignal> parseChecked(String raw, long now) {
        if (raw == null) return null;
        String line = CLOCK.matcher(raw.replaceAll("§.", "").strip()).replaceFirst("");
        Matcher m = SHOP.matcher(line);
        if (m.matches()) {
            boolean income = m.group(3).equals("판매");
            if (income != m.group(5).equals("입금")) return List.of();
            long amount = number(m.group(4)), qty = number(m.group(2));
            if (income) {
                if (now < lastSale || now - lastSale > 3000) resetSales();
                saleAmount = Math.addExact(saleAmount, amount);
                saleQty = Math.addExact(saleQty, qty);
                saleRows++;
                lastSale = now;
            } else resetSales();
            if (amount == 0) return List.of();
            return List.of(TradeSignal.of(income ? Flow.INCOME : Flow.EXPENSE, "상점", amount,
                    Math.toIntExact(qty), m.group(1) + (income ? " 판매" : " 구매"), raw));
        }
        m = TOTAL.matcher(line);
        if (m.matches()) {
            long total = number(m.group(3)), qty = number(m.group(2));
            boolean details = saleRows > 0 && now >= lastSale && now - lastSale <= 3000;
            if (details && saleAmount == total && saleQty == qty) {
                resetSales();
                return List.of(); // Each item was already counted.
            }
            if (details) {
                resetSales();
                return List.of(); // Never count an overlapping or inconsistent summary again.
            }
            resetSales();
            if (total == 0) return List.of();
            return List.of(TradeSignal.of(Flow.INCOME, "상점", total, Math.toIntExact(qty),
                    "상점 모두 판매", raw));
        }
        m = MONEY.matcher(line);
        if (m.matches()) {
            long amount = number(m.group(1));
            if (amount == 0) return List.of();
            boolean in = m.group(2).equals("입금");
            return List.of(TradeSignal.of(in ? Flow.INCOME : Flow.EXPENSE, "기타", amount, 0,
                    in ? "쿠키 입금 (출처 미확인)" : "쿠키 출금 (출처 미확인)", raw)
                    .withNote("서버 입출금 메시지 기준 · 출처 미확인"));
        }
        return null;
    }

    public void resetSales() {
        saleAmount = 0; saleQty = 0; saleRows = 0; lastSale = Long.MIN_VALUE;
    }
    private static long number(String s) { return Long.parseLong(s.replace(",", "")); }
}

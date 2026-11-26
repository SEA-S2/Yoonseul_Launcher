package kr.aka.nakin.core;
import java.nio.file.*;
import java.time.*;
import java.util.*;
import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.aggregate.DailyBucket;

public final class GirinCurrencyCheck {
    static void check(boolean ok,String s) { if(!ok) throw new AssertionError(s); }
    static TradeSignal one(CurrencyParser p,String line,long t) {
        var got=p.parse(line,t);check(got.size()==1,"expected one: "+line+" got "+got);return got.get(0);
    }
    public static void main(String[] args) throws Exception {
        var parser=CurrencyParser.createDefault();long now=System.currentTimeMillis();
        var s=one(parser,"[20:13:26] 100 쿠키 (이)가 입금되었습니다. (잔액: 100)",now);
        check(s.amount==100 && s.flow==TradeSignal.Flow.INCOME,"generic income");
        check(s.category.equals("기타") && s.label.contains("출처 미확인"),"do not invent reward source");
        s=one(parser,"[20:52:09] [상점] 오이 씨앗(을)를 64개 구매하여 64 쿠키(이)가 출금되었습니다. (잔액: 6,536)",now+1);
        check(s.amount==64 && s.qty==64 && s.flow==TradeSignal.Flow.EXPENSE && s.category.equals("상점"),"shop purchase");
        s=one(parser,"[21:30:41] [상점] 비닐하우스(을)를 64개 구매하여 6,400 쿠키(이)가 출금되었습니다. (잔액: 2,826)",now+2);
        check(s.amount==6400,"comma amount and quantity are different");
        one(parser,"[상점] 상추(을)를 1개 판매하여 13 쿠키(이)가 입금되었습니다. (잔액: 6,399)",now+3);
        one(parser,"[상점] 상추(을)를 1개 판매하여 1 쿠키(이)가 입금되었습니다. (잔액: 6,400)",now+4);
        check(parser.parse("[상점] 모두 판매 · 2종 2개를 판매하여 14 쿠키(이)가 입금되었습니다.",now+5).isEmpty(),"sale summary not double counted");
        for(String line:List.of(
            "500 꿀 (이)가 입금되었습니다. (잔액: 601)",
            "30 도토리 (이)가 입금되었습니다. (잔액: 60)",
            "[상점] 가방 확장권(을)를 1개 구매하여 20 꿀(이)가 출금되었습니다. (잔액: 452)",
            "<PlayerA> 100 쿠키 (이)가 입금되었습니다. (잔액: 100)",
            "[지역] PLAYER PlayerA » 100 쿠키 (이)가 입금되었습니다. (잔액: 100)",
            "[보상] 기린 보증 수표 (500쿠키)를(을) 1개 획득하셨습니다.",
            "100 쿠키 (이)가 입금되었습니다. (잔액: 100) 라고 친구가 말했다",
            "999999999999999999999999999 쿠키 (이)가 입금되었습니다. (잔액: 100)"
        ))check(parser.parse(line,now+6).isEmpty(),"must ignore: "+line);
        parser=CurrencyParser.createDefault();
        one(parser,"[상점] 모두 판매 · 2종 2개를 판매하여 14 쿠키(이)가 입금되었습니다.",now+7);
        one(parser,"[상점] 상추(을)를 1개 판매하여 13 쿠키(이)가 입금되었습니다. (잔액: 100)",now+8);
        one(parser,"[상점] 모두 판매 · 2종 2개를 판매하여 14 쿠키(이)가 입금되었습니다.",now+5000);

        Path dir=Files.createTempDirectory("yoondding-obt-rules-");
        Files.writeString(dir.resolve("config.json"),"{\"automaticCurrencyTracking\":false,\"hudScale\":1.5}");
        DtConfig cfg=DtConfig.load(dir);
        check(cfg.automaticCurrencyTracking && cfg.hudScale==1.5f,"enable pre-OBT default once, preserve other settings");
        cfg.automaticCurrencyTracking=false;cfg.save();cfg=DtConfig.load(dir);
        check(!cfg.automaticCurrencyTracking && cfg.girinCurrencyRulesVersion==1,"respect subsequent opt-out");
        cfg.automaticCurrencyTracking=true;
        var rows=new ArrayList<TransactionRecord>();
        var resolver=new TransactionResolver(cfg,new TransferClassifier(cfg),rows::add);
        parser=CurrencyParser.createDefault();
        String[] fixture={
            "100 쿠키 (이)가 입금되었습니다. (잔액: 100)",
            "100 쿠키 (이)가 입금되었습니다. (잔액: 200)",
            "[상점] 상추(을)를 1개 판매하여 13 쿠키(이)가 입금되었습니다. (잔액: 213)",
            "[상점] 상추(을)를 1개 판매하여 1 쿠키(이)가 입금되었습니다. (잔액: 214)",
            "[상점] 모두 판매 · 2종 2개를 판매하여 14 쿠키(이)가 입금되었습니다.",
            "[상점] 오이 씨앗(을)를 64개 구매하여 64 쿠키(이)가 출금되었습니다. (잔액: 150)",
            "[상점] 오이 씨앗(을)를 64개 구매하여 64 쿠키(이)가 출금되었습니다. (잔액: 86)"
        };
        for(int i=0;i<fixture.length;i++) for(TradeSignal sig:parser.parse(fixture[i],now+i)) resolver.onSignal(sig,now+i);
        resolver.tick(now+10_000);
        check(rows.size()==6,"repeat transactions kept and summary excluded");
        DailyBucket bucket=new DailyBucket(LocalDate.now());
        for(var row:rows)bucket.add(row);
        check(bucket.income==214 && bucket.expense==128 && bucket.netPnl()==86,"HUD/copy totals from real protocol");
        check(bucket.transferIn==0 && bucket.transferOut==0,"no invented transfer");
        System.out.println("PASS: OBT cookie protocol, comma amounts, shop qty, summary dedup, non-cookie/chat/item-reward rejection, repeated equal deposits, resolver and bucket totals, config migration and opt-out.");
    }
}

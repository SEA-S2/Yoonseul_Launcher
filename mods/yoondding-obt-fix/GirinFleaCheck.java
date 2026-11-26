package kr.aka.nakin.core;
import java.util.*;
import java.nio.file.*;
import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.aggregate.DailyBucket;
import kr.aka.nakin.aggregate.ShareText;
public final class GirinFleaCheck {
 static void check(boolean b,String s){if(!b)throw new AssertionError(s);}
 public static void main(String[] args)throws Exception{
  var parser=CurrencyParser.createDefault();long now=System.currentTimeMillis();
  var records=new ArrayList<TransactionRecord>();var c=new DtConfig();
  var resolver=new TransactionResolver(c,new TransferClassifier(c),records::add);
  String[] lines={
   "[22:06:41] [플리마켓] 석탄 1개를 구매했습니다.",
   "[22:06:41]    · 상점 PlayerA의 상점 (PlayerA · life_1 채널)",
   "[22:06:41]    · 개당 10 쿠키 · 합계 10 쿠키",
   "[22:06:41]    · 남은 잔액 3,319 쿠키",
   "[22:06:41]    · 물건은 가방으로 지급됩니다. 가방이 가득 차 있으면 발밑에 떨어집니다.",
   "[22:06:57] [플리마켓] 석탄 1개를 구매했습니다.",
   "[22:06:57]    · 상점 PlayerA의 상점 (PlayerA · life_1 채널)",
   "[22:06:57]    · 개당 10 쿠키 · 합계 10 쿠키",
   "[22:06:57]    · 남은 잔액 3,309 쿠키",
   "[22:06:57]    · 물건은 가방으로 지급됩니다. 가방이 가득 차 있으면 발밑에 떨어집니다."
  };
  for(int i=0;i<lines.length;i++)for(var s:parser.parse(lines[i],now+i))resolver.onSignal(s,now+i);
  resolver.tick(now+10000);check(records.size()==2,"two receipts, not unit price/balance/detail lines");
  var b=new DailyBucket(java.time.LocalDate.now());for(var r:records){b.add(r);check(r.category.equals("플리마켓"),"flea category");check(r.qty==1&&r.amount==10,"flea qty/amount");}
  check(b.income==0&&b.expense==20&&b.netPnl()==-20,"HUD total is expense 20");
  check(ShareText.day(b).contains("지출 20"),"copy includes expense 20");
  parser.parse("[플리마켓] 다이아몬드 3개를 구매했습니다.",now+20000);
  var got=parser.parse("· 개당 10 쿠키 · 합계 30 쿠키",now+20001);
  check(got.size()==1&&got.get(0).amount==30&&got.get(0).qty==3,"total not unit price");
  check(parser.parse("· 개당 10 쿠키 · 합계 30 쿠키",now+20002).isEmpty(),"orphan or duplicate total ignored");
  parser.parse("[플리마켓] 석탄 1개를 구매했습니다.",now+30000);
  check(parser.parse("· 개당 10 쿠키 · 합계 10 쿠키",now+34000).isEmpty(),"expired receipt");
  parser.parse("[플리마켓] 석탄 1개를 구매했습니다.",now+40000);
  check(parser.parse("· 개당 10 꿀 · 합계 10 꿀",now+40001).isEmpty(),"other currency rejected");
  var candidates=BalanceLines.labelled(List.of("16:28","147","좌표 -807, 95, 2580","꿀 601","쿠키 3,309"),"쿠키");
  check(candidates.equals(List.of("쿠키 3,309")),"unlabelled HUD numbers excluded");
  check(BalanceLines.labelled(List.of("147"),"").isEmpty(),"no marker means no guess");
  // Replay all actual GAME/CHAT lines locally without uploading player conversations.
  if(args.length>0){
   parser=CurrencyParser.createDefault();records.clear();resolver=new TransactionResolver(c,new TransferClassifier(c),records::add);long t=now+50000;
   for(String line:Files.readAllLines(Path.of(args[0]))){int pos=line.indexOf("[CHAT] ");if(pos<0)continue;for(var s:parser.parse(line.substring(pos+7),t))resolver.onSignal(s,t);t+=10;}
   resolver.tick(t+10000);long expense=records.stream().filter(r->r.kind==TransactionRecord.Kind.EXPENSE).mapToLong(r->r.amount).sum();
   check(records.size()==2&&expense==20,"actual latest log replay: exactly two purchases / expense 20");
   System.out.println("PASS: actual latest log replay, 2 purchases, expense 20 cookies.");
  }
  System.out.println("PASS: observed multi-line flea receipts -> resolver -> HUD/copy totals; quantities, total vs unit price, expiry, orphan totals, other currency, labelled balance filtering.");
 }
}

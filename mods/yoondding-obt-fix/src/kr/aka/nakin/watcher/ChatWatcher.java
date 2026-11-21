package kr.aka.nakin.watcher;

import kr.aka.nakin.core.CurrencyParser;
import kr.aka.nakin.core.TradeSignal;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.minecraft.network.chat.Component;
import java.util.function.Consumer;

/**
 * 채팅/시스템 메시지를 CurrencyParser 로 파싱해 TradeSignal 발행.
 * 액션바(overlay=true)는 잔고 캡처용이므로 파싱 대상에서 제외.
 */
public final class ChatWatcher {
    private final java.util.function.BooleanSupplier enabled;
    private final CurrencyParser parser;
    private final Consumer<TradeSignal> signalSink;
    private final Consumer<String> actionBarSink;

    public ChatWatcher(CurrencyParser parser, Consumer<TradeSignal> signalSink,
                       Consumer<String> actionBarSink) {
        this(parser, signalSink, actionBarSink, () -> true);
    }

    public ChatWatcher(CurrencyParser parser, Consumer<TradeSignal> signalSink,
                       Consumer<String> actionBarSink, java.util.function.BooleanSupplier enabled) {
        this.enabled = enabled;
        this.parser = parser;
        this.signalSink = signalSink;
        this.actionBarSink = actionBarSink;
    }

    public void register() {
        ClientReceiveMessageEvents.GAME.register((message, overlay) -> {
            if (!enabled.getAsBoolean()) return;
            if (overlay) {
                actionBarSink.accept(message.getString());
            } else {
                handle(message);
            }
        });
        // 유저가 보낸 채팅(CHAT)은 해석하지 않는다 — 거래 알림은 전부 서버 시스템 메시지로 온다.
        //   유저가 "[동글상점] … 획득 금액: N 쿠키" 을 흉내 내 쳐도 기록되지 않고, 제보 기록에 남의 대화가
        //   들어가지도 않는다(2026-10-05 Codex 리뷰).
    }

    private void handle(Component message) {
        String s = message.getString();
        var signals = parser.parse(s);
        for (TradeSignal sig : signals) {
            signalSink.accept(sig);
        }
        if (signals.isEmpty() && !CurrencyParser.isPlayerChat(s)) {
            kr.aka.nakin.core.ActivityLog.unmatched(s);
        }
    }
}

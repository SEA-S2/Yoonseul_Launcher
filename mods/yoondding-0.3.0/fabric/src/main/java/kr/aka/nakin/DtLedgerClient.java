package kr.aka.nakin;

import kr.aka.nakin.aggregate.DailyAggregator;
import kr.aka.nakin.config.DtConfig;
import kr.aka.nakin.core.CurrencyParser;
import kr.aka.nakin.core.TransactionRecord;
import kr.aka.nakin.core.TransactionResolver;
import kr.aka.nakin.core.TransferClassifier;
import kr.aka.nakin.debug.BalanceProbe;
import kr.aka.nakin.store.LedgerStore;
import kr.aka.nakin.update.UpdateChecker;
import kr.aka.nakin.ui.DtKeyBindings;
import kr.aka.nakin.ui.DtStatCommand;
import kr.aka.nakin.ui.LedgerHud;
import kr.aka.nakin.watcher.BalanceWatcher;
import kr.aka.nakin.watcher.ChatWatcher;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.ClickEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.HoverEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.YearMonth;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.function.Consumer;

/**
 * 진입점. 감지(Watcher) → 파싱(Parser) → 결합(Resolver) → 분류(Classifier)
 * → 저장(Store)·집계(Aggregator) → 표시(HUD/Command) 파이프라인을 연결한다.
 *
 * 모든 구성요소는 read-only 관찰. 서버로 아무것도 전송하지 않는다.
 */
public final class DtLedgerClient implements ClientModInitializer {
    private static final Logger LOG = LoggerFactory.getLogger("nakin");

    /** 클라이언트 게임테스트(gametest 소스셋)가 집계 결과를 확인하는 읽기 전용 통로. */
    private static volatile DailyAggregator aggregatorRef;
    public static DailyAggregator aggregatorForTests() { return aggregatorRef; }

    @Override
    public void onInitializeClient() {
        Path configRoot = net.fabricmc.loader.api.FabricLoader.getInstance().getConfigDir();
        Path dir = configRoot.resolve("yoondding");
        migrateLegacyData(configRoot.resolve("yoonseul-ledger"), dir);
        migrateLegacyData(configRoot.resolve("nakin"), dir);
        DtConfig config = DtConfig.load(dir);
        // 독립 배포 저장소가 정해질 때까지 원작 저장소를 향한 자동 업데이트는 연결하지 않는다.
        LedgerStore store = new LedgerStore(dir);
        DailyAggregator aggregator = new DailyAggregator(config, store);
        aggregator.ensureMonthLoaded(YearMonth.now()); // 당월 원장 복구
        // 이전 실행이 비정상 종료됐거나 접속 종료 이벤트를 놓쳐 남은 가짜 테스트 기록도 복구 시 제거한다.
        removeTestRecords(aggregator, aggregator.recent());
        aggregatorRef = aggregator;

        TransferClassifier classifier = new TransferClassifier(config);
        LedgerHud hud = new LedgerHud(config, aggregator);

        // 싱글플레이(테스트 창 등)에서 생긴 기록은 월드를 나갈 때 지운다 — 진짜 가계부를 오염시키지 않게.
        final boolean[] inSingleplayer = {false};
        final java.util.List<TransactionRecord> singleplayerRecords = new java.util.ArrayList<>();
        Consumer<TransactionRecord> sink = rec -> {
            store.commit(rec);
            aggregator.addLive(rec);
            // JOIN 시점 판정에 의존하지 않고 테스트 기록 자체를 표시해 반드시 회수한다.
            if (inSingleplayer[0] || isTestRecord(rec)) singleplayerRecords.add(rec);
            kr.aka.nakin.core.ActivityLog.record(rec);
        };
        TransactionResolver resolver = new TransactionResolver(config, classifier, sink);
        // 금액을 못 알아낸 거래를 채팅으로 알린다 — 조용히 사라지면 유저가 알 방법이 없다.
        resolver.setNotifier(msg -> {
            Minecraft mc = Minecraft.getInstance();
            mc.execute(() -> {
                if (mc.player == null) return;
                for (String line : msg.split("\n")) mc.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of(line));
            });
        });

        // 내 닉네임 — 마을 금고 입금·+5강 강화 방송처럼 남의 행동도 같은 문구로 뜨는 메시지를 가린다.
        CurrencyParser parser = new CurrencyParser(() -> {
            var user = Minecraft.getInstance().getUser();
            return user == null ? null : user.getName();
        });
        BalanceWatcher balanceWatcher = new BalanceWatcher(config, delta -> {
            kr.aka.nakin.core.ActivityLog.delta(delta);
            resolver.onDelta(delta);
        });
        balanceWatcher.setOnRejected(resolver::discardPendingDeltas);
        ChatWatcher chatWatcher = new ChatWatcher(parser, resolver::onSignal,
                balanceWatcher::captureActionBar);

        if (config.automaticCurrencyTracking) chatWatcher.register();
        hud.register();
        kr.aka.nakin.ui.DtTestScreen.registerTicker();
        if (config.automaticCurrencyTracking) kr.aka.nakin.watcher.GuiCostWatcher.register();
        new DtKeyBindings(config, aggregator, hud, sink).register();
        new DtStatCommand(aggregator, config, store, hud, sink).register();

        if (config.debugProbe) {
            new BalanceProbe().register();
            LOG.info("[nakin] 진단 로거 ON — 잔고 소스 A/B 판정 후 config.debugProbe=false 로 끄세요.");
        }

        // 메인 틱 펌프: 창 비용 읽기 → 잔고 감지 → 결합 확정 → 저장 flush
        final int[] updateTick = {0};
        final int[] guiScanTick = {0};
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            long now = System.currentTimeMillis();

            // 대장간처럼 채팅에 금액이 없는 결제 — 창이 열려 있을 때만, 3틱(약 150ms)마다 설명을 읽는다
            if (config.automaticCurrencyTracking
                    && client.gui.screen() instanceof net.minecraft.client.gui.screens.inventory.AbstractContainerScreen<?>
                    && ++guiScanTick[0] % 3 == 0) {
                resolver.noteGuiCosts(kr.aka.nakin.watcher.GuiCostWatcher.scan(client), now);
            }

            if (config.automaticCurrencyTracking) balanceWatcher.tick(client, now);
            resolver.tick(now);
            kr.aka.nakin.core.WalletCheck.LIVE.observe(now, balanceWatcher.confirmedBalance() != null,
                    kr.aka.nakin.util.LedgerDates.today(config.dayResetHour),
                    resolver.unexplainedTotal(), resolver.isIdle());
            store.tick(now);

            // 새 버전 재확인 — 실제 네트워크 요청은 UpdateChecker 가 1시간 간격으로만 낸다.
            // 여기선 60초마다 두드려서, 구버전을 계속 쓰면 1시간마다 알림이 다시 뜨게 한다.
            if (++updateTick[0] % 1200 == 0) checkForUpdate(config, client);
        });

        // 접속 시 잔고 기준선 리셋, 종료 시 저장 flush
        ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> {
            inSingleplayer[0] = client.hasSingleplayerServer();
            if (config.automaticCurrencyTracking) balanceWatcher.newSession();
            resolver.newSession();
            kr.aka.nakin.core.WalletCheck.LIVE.reset();
            checkForUpdate(config, client);
        });
        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> {
            // 추적 목록뿐 아니라 오늘 원장도 다시 훑어 JOIN 판정 누락·강제 종료 잔여분까지 정리한다.
            java.util.List<TransactionRecord> cleanup = new java.util.ArrayList<>(singleplayerRecords);
            for (TransactionRecord r : aggregator.recent()) {
                if (isTestRecord(r) && !cleanup.contains(r)) cleanup.add(r);
            }
            int n = removeTestRecords(aggregator, cleanup);
            singleplayerRecords.clear();
            if (n > 0) LOG.info("[yoondding] 싱글플레이 테스트 기록 {}건 삭제(진짜 가계부 보호)", n);
            inSingleplayer[0] = false;
            store.flushNow();
        });
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> store.flushNow());

        LOG.info("[yoonseul-ledger] 윤띵 초기화 완료 (자동 재화 감지: {}, 서버 전송 없음).",
                config.automaticCurrencyTracking);
    }

    private static boolean isTestRecord(TransactionRecord r) {
        return r != null && "싱글플레이 테스트".equals(r.note);
    }

    private static int removeTestRecords(DailyAggregator aggregator, java.util.Collection<TransactionRecord> records) {
        int removed = 0;
        for (TransactionRecord r : java.util.List.copyOf(records)) {
            if (isTestRecord(r) && aggregator.deleteRecord(r)) removed++;
        }
        return removed;
    }

    /**
     * 예전 이름의 설정·장부·내보내기 파일을 새 폴더로 옮긴다.
     * 같은 이름이 이미 있으면 새 폴더의 파일을 우선해 기록을 덮어쓰지 않는다.
     */
    private static void migrateLegacyData(Path oldDir, Path newDir) {
        if (oldDir.equals(newDir) || !Files.isDirectory(oldDir)) return;
        try {
            Files.createDirectories(newDir);
            try (var paths = Files.walk(oldDir)) {
                for (Path source : paths.sorted(java.util.Comparator.comparingInt(Path::getNameCount)).toList()) {
                    if (source.equals(oldDir) || Files.isDirectory(source)) continue;
                    Path target = newDir.resolve(oldDir.relativize(source));
                    Files.createDirectories(target.getParent());
                    if (!Files.exists(target)) Files.move(source, target, StandardCopyOption.ATOMIC_MOVE);
                }
            }
            // 옮기고 비어 있는 예전 하위 폴더와 루트만 정리한다.
            try (var paths = Files.walk(oldDir)) {
                for (Path path : paths.sorted(java.util.Comparator.reverseOrder()).toList()) {
                    if (Files.isDirectory(path)) {
                        try (var children = Files.list(path)) {
                            if (children.findAny().isEmpty()) Files.deleteIfExists(path);
                        }
                    }
                }
            }
        } catch (Exception e) {
            LOG.warn("[yoondding] 예전 설정 폴더를 옮기지 못했습니다: {} → {}", oldDir, newDir, e);
        }
    }

    /**
     * 새 버전이 있으면 채팅에 한 줄 안내. 구버전으로 이미 고친 버그를 제보하는 일이 반복돼 추가.
     * 게임 서버와 무관한 GET 한 번이며 어떤 식별 정보도 보내지 않는다(설정에서 끌 수 있음).
     */
    /**
     * 동의 기반 자동 설치기를 연결한다. 여기서는 <b>아무것도 받지 않는다</b> —
     * 실제 다운로드는 유저가 확인 화면에서 승인해야만 일어난다.
     *
     * <p>실패해도 조용히 넘어간다(개발 환경엔 신원 파일이 없고, mods 폴더가 아닌 데서
     * 로드될 수도 있다). 그 경우 기존의 "링크 눌러 직접 받기" 안내만 뜬다.
     */
    private void bindUpdater() {
        try {
            var loader = net.fabricmc.loader.api.FabricLoader.getInstance();
            var mod = loader.getModContainer("yoonseul_ledger")
                    .orElseThrow(() -> new java.io.IOException("모드 메타데이터를 찾을 수 없습니다"));
            // 칼띵과 같은 기준: 실제 로드된 경로가 '.jar 파일 하나'일 때만 자동 교체를 지원한다.
            //   (개발 환경은 클래스 폴더라 여기서 걸러진다)
            var jars = mod.getOrigin().getPaths().stream()
                    .filter(java.nio.file.Files::isRegularFile)
                    .filter(p -> p.getFileName() != null && p.getFileName().toString().endsWith(".jar"))
                    .toList();
            if (jars.size() != 1) throw new java.io.IOException("실행 JAR 경로가 하나가 아니어서 자동 교체를 지원하지 않습니다");
            // 동글랜드 공식 설치기(ModCheck 프로필)는 .minecraft/modcheck/mods 를, Feather 등은
            //   게임폴더/mods 를 쓴다. 로더가 실제로 이 JAR 를 읽어 온 폴더를 기준으로 하되,
            //   이름이 mods 인 폴더에 직접 들어 있을 때만 허용한다(하위 폴더·임시 경로 교체 방지).
            java.nio.file.Path mods = jars.get(0).toAbsolutePath().normalize().getParent();
            if (mods == null || mods.getFileName() == null || !"mods".equals(mods.getFileName().toString()))
                throw new java.io.IOException("mods 폴더에 직접 설치된 JAR가 아닙니다");
            kr.aka.nakin.update.UpdateInstaller.bind(
                    new kr.aka.nakin.update.ModUpdater(
                            "nakin", mods, jars.get(0),
                            msg -> {
                                Minecraft mc = Minecraft.getInstance();
                                mc.execute(() -> {
                                    if (mc.player != null) mc.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of("§6[윤띵] §r" + msg));
                                });
                            }));
        } catch (Exception e) {
            // 자동 설치 미지원 환경 — 알림은 수동 안내로만 뜨고, 업데이트 화면은 이 사유를 보여준다.
            kr.aka.nakin.update.UpdateInstaller.unavailable(
                    "현재 실행 파일을 확인할 수 없어 자동 업데이트할 수 없습니다: " + e.getMessage());
        }
    }

    private void checkForUpdate(kr.aka.nakin.config.DtConfig config, Minecraft client) {
        if (!config.updateCheckEnabled || config.updateCheckUrl == null || config.updateCheckUrl.isBlank()) return;
        String current = net.fabricmc.loader.api.FabricLoader.getInstance()
                .getModContainer("yoonseul_ledger")
                .map(m -> m.getMetadata().getVersion().getFriendlyString())
                .orElse("0");

        UpdateChecker.checkAsync(config.updateCheckUrl, current, release -> client.execute(() -> {
            if (client.player == null) return;
            // 새 버전 알림이 떴을 때 릴리즈 정보를 미리 받아둔다 — 업데이트 화면을 열면 바로 보이게.
            //   ModUpdater 에 1시간 쿨다운이 내장돼 있어 GitHub 비인증 한도(시간당 60회)는 걱정 없다.
            var updater = kr.aka.nakin.update.UpdateInstaller.get();
            if (updater != null) updater.check();
            client.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of("§6§m                                              "));
            client.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of("§6§l 윤띵§r§f  새 버전 §a§l" + release.version()
                    + "§r §7(현재 " + current + ")"));
            if (release.notes() != null && !release.notes().isBlank()) {
                client.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of("§7   " + release.notes()));
            }
            String url = release.url();
            if (url != null && (url.startsWith("https://") || url.startsWith("http://"))) {
                client.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of("")
                        .append(kr.aka.nakin.ui.ChatText.of("§b§n » 다운로드 (여기 클릭)")
                                .withStyle(st -> st
                                        .withClickEvent(new ClickEvent.OpenUrl(java.net.URI.create(url)))
                                        .withHoverEvent(new HoverEvent.ShowText(
                                                kr.aka.nakin.ui.ChatText.of("§7" + url)))))
                        .append(kr.aka.nakin.ui.ChatText.of("§r§8   · 기존 파일 삭제 후 교체")));
            }
            // 자동 설치가 가능한 환경이면 한 줄 더. 클릭은 '확인 화면'을 열 뿐,
            // 바로 받지 않는다 — 다운로드는 그 화면에서 동의해야 시작된다.
            if (kr.aka.nakin.update.UpdateInstaller.available()) {
                client.player.sendSystemMessage(kr.aka.nakin.ui.ChatText.of("")
                        .append(kr.aka.nakin.ui.ChatText.of("§a§n » 모드가 대신 설치 (여기 클릭)")
                                .withStyle(st -> st
                                        .withClickEvent(new ClickEvent.RunCommand("윤띵 업데이트"))
                                        .withHoverEvent(new HoverEvent.ShowText(
                                                kr.aka.nakin.ui.ChatText.of("§7확인 화면을 엽니다. 바로 받지 않습니다.")))))
                        .append(kr.aka.nakin.ui.ChatText.of("§r§8   · 동의 후 진행")));
            }
        }));
    }

}

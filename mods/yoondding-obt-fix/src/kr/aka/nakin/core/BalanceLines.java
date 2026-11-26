package kr.aka.nakin.core;
import java.util.List;
/** Unlabelled HUD numbers are not evidence of a cookie balance. */
public final class BalanceLines {
    private BalanceLines() {}
    public static List<String> labelled(List<String> lines, String marker) {
        if (marker == null || marker.isBlank()) return List.of();
        return lines.stream().filter(s -> s != null && s.replaceAll("§.", "").contains(marker)).toList();
    }
}

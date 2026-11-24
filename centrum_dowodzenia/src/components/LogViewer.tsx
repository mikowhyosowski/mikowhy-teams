import React, { useEffect, useRef, useState } from "react";
import { Animated, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { colors, motion, radius, spacing, type } from "../theme";
import { Icon } from "./Icon";
import { ErrorState, Skeleton } from "./States";

type Stream = "stdout" | "stderr";

/**
 * Terminalowy podgląd logu runu: zakładki stdout/stderr, auto-scroll do dołu,
 * skeleton przy ładowaniu, błąd z retry — stan pierwszej klasy.
 */
export function LogViewer({ runId }: { runId: string }) {
  const [log, setLog] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [stream, setStream] = useState<Stream>("stdout");
  const scrollRef = useRef<ScrollView>(null);
  const fade = useRef(new Animated.Value(0)).current;

  const load = React.useCallback(() => {
    let alive = true;
    setLog(null);
    setErr(null);
    api.runLog(runId).then(
      (t) => {
        if (!alive) return;
        setLog(t);
        Animated.timing(fade, { toValue: 1, duration: motion.base, useNativeDriver: true }).start();
      },
      (e) => alive && setErr(String(e)),
    );
    return () => {
      alive = false;
    };
  }, [runId, fade]);

  useEffect(() => load(), [load]);

  useEffect(() => {
    // auto-scroll do najnowszej linii po załadowaniu
    const t = setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 60);
    return () => clearTimeout(t);
  }, [log, stream]);

  const lines = (log ?? "").split("\n");
  const isErrLine = (l: string) => /^\s*(error|traceback|fatal|panic)/i.test(l);
  const shown = stream === "stderr" ? lines.filter((l) => isErrLine(l) || l.includes("stderr")) : lines;

  return (
    <View style={styles.wrap}>
      <View style={styles.bar}>
        <Icon.terminal size={13} color={colors.textDim} />
        {(["stdout", "stderr"] as Stream[]).map((s) => (
          <Pressable
            key={s}
            style={[styles.tab, stream === s && styles.tabActive]}
            onPress={() => setStream(s)}
          >
            <Text style={[styles.tabText, stream === s && { color: colors.accent }]}>{s}</Text>
          </Pressable>
        ))}
        <View style={{ flex: 1 }} />
        <Pressable style={styles.miniBtn} onPress={load} accessibilityRole="button">
          <Icon.refresh size={12} color={colors.textDim} />
        </Pressable>
      </View>
      {log === null && !err && (
        <View style={{ gap: 6, padding: spacing.md }}>
          <Skeleton height={10} width="82%" />
          <Skeleton height={10} width="64%" />
          <Skeleton height={10} width="74%" />
        </View>
      )}
      {err && <ErrorState message={`Nie udało się pobrać logu: ${err}`} onRetry={load} />}
      {log !== null && (
        <Animated.View style={{ opacity: fade }}>
          <ScrollView ref={scrollRef} style={styles.scroll} horizontal>
            <Text style={styles.text}>
              {(shown.length ? shown : ["(brak linii)"]).join("\n")}
            </Text>
          </ScrollView>
        </Animated.View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    marginTop: spacing.sm,
    backgroundColor: colors.bgInset,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: radius.md,
    overflow: "hidden",
  },
  bar: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.sm,
    paddingVertical: 6,
    borderBottomColor: colors.border,
    borderBottomWidth: 1,
  },
  tab: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: radius.sm },
  tabActive: { backgroundColor: colors.accentDim },
  tabText: { fontFamily: colors.mono, fontSize: type.meta, color: colors.textDim, fontWeight: "700" },
  miniBtn: { padding: 4 },
  scroll: { maxHeight: 300, padding: spacing.md },
  text: { fontFamily: colors.mono, fontSize: 11.5, color: "#C9D6E8", lineHeight: 16 },
});

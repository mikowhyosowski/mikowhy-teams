import React, { useEffect, useRef, useState } from "react";
import { Animated, Pressable, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { colors, elevation, isLive, motion, radius, spacing, statusColorOf, type } from "../theme";
import type { ProcessRun } from "../types";
import { Icon } from "./Icon";
import { LogViewer } from "./LogViewer";
import { StatusPill } from "./StatusPill";

function fmtDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function fmtClock(iso: string | null): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(11, 19) + "Z";
}

/**
 * Wiersz runu procesu w pipeline'u: status, komenda (mono), PID/PGID, czas,
 * pasek postępu względem timeout_s dla żywych runów, akcja kill, rozwiany log.
 */
export function RunRow({ run, onKilled }: { run: ProcessRun; onKilled?: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const live = isLive(run.status);
  const accent = statusColorOf(run.status);

  // rozwinięcie/złożenie — animowana wysokość
  const anim = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    Animated.timing(anim, {
      toValue: open ? 1 : 0,
      duration: motion.base,
      useNativeDriver: false,
    }).start();
  }, [open, anim]);
  const bodyOpacity = anim;
  const bodyMaxH = anim.interpolate({ inputRange: [0, 1], outputRange: [0, 600] });

  // press feedback na przycisku kill
  const press = useRef(new Animated.Value(1)).current;
  const onPressIn = () => Animated.timing(press, { toValue: 0.88, duration: motion.fast, useNativeDriver: true }).start();
  const onPressOut = () => Animated.timing(press, { toValue: 1, duration: motion.fast, useNativeDriver: true }).start();

  const kill = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.killRun(run.run_id);
      setMsg(r.ok ? "Proces zabity (cała grupa pgid)" : JSON.stringify(r));
      onKilled?.();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={[styles.row, { borderLeftColor: accent }, elevation.card]}>
      <Pressable style={styles.head} onPress={() => setOpen((o) => !o)} accessibilityRole="button">
        <Animated.View style={{ transform: [{ rotate: anim.interpolate({ inputRange: [0, 1], outputRange: ["0deg", "90deg"] }) }] }}>
          <Icon.chevron size={14} color={colors.textDim} />
        </Animated.View>
        <StatusPill status={run.status} />
        <Text style={styles.cmd} numberOfLines={1}>
          $ {run.command}
        </Text>
        {live && (
          <Animated.View style={{ transform: [{ scale: press }] }}>
            <Pressable
              style={[styles.killBtn, busy && { opacity: 0.5 }]}
              disabled={busy}
              onPress={kill}
              onPressIn={onPressIn}
              onPressOut={onPressOut}
              accessibilityRole="button"
            >
              <Icon.kill size={13} color={colors.danger} />
              <Text style={styles.killText}>{busy ? "Zabijanie…" : "Ubierz proces"}</Text>
            </Pressable>
          </Animated.View>
        )}
      </Pressable>

      <View style={styles.metaRow}>
        <Text style={styles.meta}>pid {run.pid ?? "—"}</Text>
        <Text style={styles.metaSep}>·</Text>
        <Text style={styles.meta}>pgid {run.pgid ?? "—"}</Text>
        <Text style={styles.metaSep}>·</Text>
        <Text style={styles.meta}>{fmtDuration(run.duration_ms)}</Text>
        <Text style={styles.metaSep}>·</Text>
        <Text style={styles.meta}>{fmtClock(run.started_at)}</Text>
        {run.substep && (
          <>
            <Text style={styles.metaSep}>·</Text>
            <Text style={styles.meta}>krok {run.substep}</Text>
          </>
        )}
      </View>

      {live && run.timeout_s ? <TimeoutBar run={run} /> : null}
      {msg && <Text style={styles.msg}>{msg}</Text>}

      <Animated.View style={{ maxHeight: bodyMaxH, opacity: bodyOpacity, overflow: "hidden" }}>
        {run.stdout_tail ? <Text style={styles.tail}>stdout │ {run.stdout_tail.slice(-300)}</Text> : null}
        {run.stderr_tail ? <Text style={[styles.tail, { color: colors.danger }]}>stderr │ {run.stderr_tail.slice(-300)}</Text> : null}
        <LogViewer runId={run.run_id} />
      </Animated.View>
    </View>
  );
}

/** Cienki pasek „budżetu czasu” dla żywego runu (elapsed / timeout_s). */
function TimeoutBar({ run }: { run: ProcessRun }) {
  const [frac, setFrac] = useState(0);
  useEffect(() => {
    if (!run.started_at || !run.timeout_s) return;
    const start = Date.parse(run.started_at);
    if (Number.isNaN(start)) return;
    const tick = () => setFrac(Math.min(1, (Date.now() - start) / 1000 / run.timeout_s!));
    tick();
    const t = setInterval(tick, 500);
    return () => clearInterval(t);
  }, [run.started_at, run.timeout_s]);
  const urgent = frac > 0.8;
  return (
    <View style={styles.barTrack}>
      <View style={[styles.barFill, { width: `${frac * 100}%`, backgroundColor: urgent ? colors.danger : colors.accent }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    backgroundColor: colors.bgCard,
    borderColor: colors.border,
    borderWidth: 1,
    borderLeftWidth: 3,
    borderRadius: radius.md,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  head: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  cmd: { fontFamily: colors.mono, fontSize: type.body, color: colors.text, flex: 1 },
  metaRow: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", marginTop: 6, gap: 6 },
  meta: { fontFamily: colors.mono, fontSize: type.meta, color: colors.textDim },
  metaSep: { color: colors.textFaint, fontSize: type.meta },
  killBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    backgroundColor: "#3B1220",
    borderColor: colors.danger,
    borderWidth: 1,
    borderRadius: radius.sm,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  killText: { color: colors.danger, fontSize: type.meta, fontWeight: "700" },
  barTrack: { height: 3, backgroundColor: colors.bgInset, borderRadius: 2, marginTop: spacing.sm, overflow: "hidden" },
  barFill: { height: "100%", borderRadius: 2 },
  msg: { fontSize: type.meta, color: colors.accent, marginTop: 6 },
  tail: { fontFamily: colors.mono, fontSize: type.meta, color: "#9FB3CC", marginTop: 6 },
});

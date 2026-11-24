import React, { useCallback, useEffect, useState } from "react";
import { RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { RunRow } from "../components/RunRow";
import { EmptyState, ErrorState, Skeleton } from "../components/States";
import { colors, elevation, radius, spacing, type } from "../theme";
import type { ProcessRun, WorkerHealth } from "../types";

type Decision = { id: string; title: string; role: string; substep: string | null };

/** Ekran 1: Centrum Operacyjne — żywe procesy, liczniki, zacięcia (Puls Fabryki). */
export function CommandCenterScreen() {
  const [runs, setRuns] = useState<ProcessRun[] | null>(null);
  const [recent, setRecent] = useState<ProcessRun[]>([]);
  const [workers, setWorkers] = useState<WorkerHealth[]>([]);
  const [stalled, setStalled] = useState<string[]>([]);
  const [awaiting, setAwaiting] = useState<Decision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  const load = useCallback(async () => {
    try {
      const [live, rec, wh, sp] = await Promise.all([
        api.liveRuns(),
        api.runs({ limit: 20 }),
        api.workerHealth(),
        api.sponsor(),
      ]);
      setRuns(live.runs);
      setRecent(rec.runs);
      setWorkers(wh.workers);
      setStalled(wh.stalled);
      setAwaiting((sp.awaiting_owner ?? []) as Decision[]);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 2000);
    const c = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(t);
      clearInterval(c);
    };
  }, [load]);

  const failed = recent.filter((r) => r.status === "FAILED" || r.status === "TIMEOUT" || r.status === "KILLED");

  return (
    <ScrollView
      style={styles.screen}
      refreshControl={<RefreshControl refreshing={false} onRefresh={load} tintColor={colors.accent} />}
    >
      <View style={styles.hRow}>
        <Icon.radar size={20} color={colors.accent} />
        <Text style={styles.h1}>Centrum Operacyjne</Text>
      </View>

      {error && <ErrorState message={`Backend nie odpowiada: ${error}`} onRetry={load} />}

      <View style={styles.counters}>
        <Counter icon="bolt" label="żywe procesy" value={runs?.length ?? 0} color="#38BDF8" />
        <Counter icon="clock" label="aktywne zadania" value={workers.length} color="#818CF8" />
        <Counter icon="alert" label="zacięcia (stall)" value={stalled.length} color={colors.danger} />
        <Counter icon="inbox" label="decyzje Sponsora" value={awaiting.length} color={colors.warn} />
        <Counter icon="kill" label="nieudane (ostatnie)" value={failed.length} color="#FB923C" />
      </View>

      <Text style={styles.h2}>Żywe procesy</Text>
      {runs === null && !error && (
        <View style={{ gap: 8 }}>
          <Skeleton height={52} /><Skeleton height={52} /><Skeleton height={52} />
        </View>
      )}
      {runs !== null && runs.length === 0 && (
        <EmptyState icon="radar" title="Fabryka stoi" hint="Brak aktywnych procesów — kolejka czeka na zadania." />
      )}
      {runs?.map((r) => (
        <View key={r.run_id}>
          <LiveTimer startedAt={r.started_at} now={now} stalled={stalled.includes(r.task_id ?? "")} />
          <RunRow run={r} onKilled={load} />
        </View>
      ))}

      <Text style={styles.h2}>Ostatnie runy</Text>
      {recent.slice(0, 10).map((r) => (
        <RunRow key={r.run_id} run={r} onKilled={load} />
      ))}
    </ScrollView>
  );
}

function Counter({ icon, label, value, color }: { icon: keyof typeof Icon; label: string; value: number; color: string }) {
  const I = Icon[icon];
  return (
    <View style={[styles.counter, elevation.card]}>
      <I size={15} color={color} />
      <Text style={[styles.counterValue, { color }]}>{value}</Text>
      <Text style={styles.counterLabel}>{label}</Text>
    </View>
  );
}

function LiveTimer({ startedAt, now, stalled }: { startedAt: string | null; now: number; stalled: boolean }) {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return null;
  const s = Math.max(0, Math.floor((now - start) / 1000));
  return (
    <View style={styles.timerRow}>
      <Icon.clock size={11} color={stalled ? colors.danger : "#38BDF8"} />
      <Text style={[styles.timer, stalled && { color: colors.danger }]}>
        {String(Math.floor(s / 60)).padStart(2, "0")}:{String(s % 60).padStart(2, "0")}
        {stalled ? " · ZACIĘCIE" : ""}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: spacing.lg },
  hRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm, marginBottom: spacing.lg },
  h1: { color: colors.text, fontSize: type.display, fontWeight: "800" },
  h2: { color: colors.text, fontSize: type.title, fontWeight: "700", marginVertical: spacing.md },
  counters: { flexDirection: "row", gap: spacing.md, flexWrap: "wrap", marginBottom: spacing.lg },
  counter: {
    backgroundColor: colors.bgCard,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: radius.md,
    padding: spacing.md,
    minWidth: 118,
    gap: 2,
  },
  counterValue: { fontSize: type.big, fontWeight: "800", fontFamily: colors.mono },
  counterLabel: { color: colors.textDim, fontSize: type.meta },
  timerRow: { flexDirection: "row", alignItems: "center", gap: 5, marginBottom: 2 },
  timer: { color: "#38BDF8", fontFamily: colors.mono, fontSize: type.meta },
});

import React, { useCallback, useEffect, useState } from "react";
import { RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { RunRow } from "../components/RunRow";
import { StatusPill } from "../components/StatusPill";
import { colors, spacing } from "../theme";
import type { ProcessRun, WorkerHealth } from "../types";

type Decision = { id: string; title: string; role: string; substep: string | null };

/** Ekran 1: Centrum Operacyjne — żywe procesy, kolejka, blokery (Puls Fabryki). */
export function CommandCenterScreen() {
  const [runs, setRuns] = useState<ProcessRun[]>([]);
  const [recent, setRecent] = useState<ProcessRun[]>([]);
  const [workers, setWorkers] = useState<WorkerHealth[]>([]);
  const [stalled, setStalled] = useState<string[]>([]);
  const [awaiting, setAwaiting] = useState<Decision[]>([]);
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
    } catch {
      /* backend offline — zostawiamy ostatni stan */
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
      <Text style={styles.h1}>🛰️ Centrum Operacyjne</Text>

      <View style={styles.counters}>
        <Counter label="żywe procesy" value={runs.length} color={colors.accent} />
        <Counter label="w kolejce zadań" value={workers.length} color="#818CF8" />
        <Counter label="zacięcia (stall)" value={stalled.length} color="#F87171" />
        <Counter label="decyzje Sponsora" value={awaiting.length} color="#F59E0B" />
        <Counter label="nieudane (ostatnie)" value={failed.length} color="#FB923C" />
      </View>

      <Text style={styles.h2}>⚡ Żywe procesy (sponsor-shell)</Text>
      {runs.length === 0 && <Text style={styles.dim}>Brak aktywnych procesów — fabryka stoi lub czeka.</Text>}
      {runs.map((r) => (
        <View key={r.run_id}>
          <LiveTimer startedAt={r.started_at} now={now} />
          <RunRow run={r} onKilled={load} />
        </View>
      ))}

      <Text style={styles.h2}>🧵 Ostatnie runy</Text>
      {recent.slice(0, 10).map((r) => (
        <RunRow key={r.run_id} run={r} onKilled={load} />
      ))}
    </ScrollView>
  );
}

function Counter({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <View style={styles.counter}>
      <Text style={[styles.counterValue, { color }]}>{value}</Text>
      <Text style={styles.counterLabel}>{label}</Text>
    </View>
  );
}

function LiveTimer({ startedAt, now }: { startedAt: string | null; now: number }) {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return null;
  const s = Math.max(0, Math.floor((now - start) / 1000));
  return (
    <Text style={styles.timer}>
      ⏱ {String(Math.floor(s / 60)).padStart(2, "0")}:{String(s % 60).padStart(2, "0")}
    </Text>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: spacing.lg },
  h1: { color: colors.text, fontSize: 22, fontWeight: "800", marginBottom: spacing.lg },
  h2: { color: colors.text, fontSize: 15, fontWeight: "700", marginVertical: spacing.md },
  dim: { color: colors.textDim, fontSize: 13 },
  counters: { flexDirection: "row", gap: spacing.md, flexWrap: "wrap", marginBottom: spacing.lg },
  counter: {
    backgroundColor: colors.bgCard,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 10,
    padding: spacing.md,
    minWidth: 110,
  },
  counterValue: { fontSize: 24, fontWeight: "800", fontFamily: colors.mono },
  counterLabel: { color: colors.textDim, fontSize: 11, marginTop: 2 },
  timer: { color: "#38BDF8", fontFamily: colors.mono, fontSize: 11, marginBottom: 2 },
});

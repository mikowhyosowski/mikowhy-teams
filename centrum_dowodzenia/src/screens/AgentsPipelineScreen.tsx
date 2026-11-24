import React, { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { RunRow } from "../components/RunRow";
import { StatusPill } from "../components/StatusPill";
import { EmptyState, ErrorState, Skeleton } from "../components/States";
import { colors, elevation, radius, spacing, type } from "../theme";
import type { AgentPipeline, ProcessRun } from "../types";

/** Ekran 2: Agenci & Execution Pipeline — karta agenta → pionowy pipeline zadań i runów. */
export function AgentsPipelineScreen() {
  const [roles, setRoles] = useState<string[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [pipe, setPipe] = useState<AgentPipeline | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadRoles = useCallback(async () => {
    try {
      const all = await api.runs({ limit: 200 });
      const uniq = [...new Set(all.runs.map((r: ProcessRun) => r.agent_role).filter(Boolean))] as string[];
      setRoles(uniq.sort());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const loadPipe = useCallback(async (role: string) => {
    try {
      setPipe(await api.agentPipeline(role));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    loadRoles();
  }, [loadRoles]);

  useEffect(() => {
    if (selected) {
      loadPipe(selected);
      const t = setInterval(() => loadPipe(selected), 3000);
      return () => clearInterval(t);
    }
  }, [selected, loadPipe]);

  return (
    <ScrollView style={styles.screen}>
      <View style={styles.hRow}>
        <Icon.bot size={20} color={colors.accent} />
        <Text style={styles.h1}>Agenci &amp; Execution Pipeline</Text>
      </View>

      {error && <ErrorState message={`API: ${error}`} onRetry={loadRoles} />}

      <View style={styles.roleBar}>
        {roles === null && !error && <Skeleton height={32} width={280} />}
        {roles?.map((r) => (
          <Pressable
            key={r}
            style={[styles.roleChip, selected === r && styles.roleChipActive]}
            onPress={() => setSelected(r)}
            accessibilityRole="button"
          >
            <View style={[styles.roleDot, { backgroundColor: colors.accent }]} />
            <Text style={[styles.roleText, selected === r && { color: colors.accent }]}>{r}</Text>
          </Pressable>
        ))}
        {roles !== null && roles.length === 0 && !error && (
          <EmptyState
            icon="bot"
            title="Brak zarejestrowanych agentów"
            hint="Uruchom komendę przez sponsor-shell: python3 -m engine.sponsor_shell --task T-1 --agent rola -- echo hi"
          />
        )}
      </View>

      {pipe && (
        <View style={{ marginTop: spacing.lg }}>
          <Text style={styles.h2}>
            Pipeline: <Text style={{ color: colors.accent }}>{pipe.role}</Text>
            <Text style={styles.dim}>  ({pipe.runs_total} runów)</Text>
          </Text>
          {pipe.tasks.map((t) => (
            <View key={t.id} style={[styles.taskCard, elevation.card]}>
              <View style={styles.taskHead}>
                <StatusPill status={t.status} />
                <Text style={styles.taskTitle}>{t.id}</Text>
                <Text style={styles.taskTitleDim} numberOfLines={1}>{t.title}</Text>
                {t.substep && <Text style={styles.substep}>krok {t.substep}</Text>}
              </View>
              <View style={styles.timeline}>
                {(t.runs ?? []).map((r) => (
                  <View key={r.run_id} style={styles.timelineItem}>
                    <View style={styles.timelineRail} />
                    <View style={{ flex: 1 }}>
                      <RunRow run={r} onKilled={() => loadPipe(pipe.role)} />
                    </View>
                  </View>
                ))}
              </View>
              {(t.runs ?? []).length === 0 && <Text style={styles.dim}>brak runów procesów dla tego zadania</Text>}
            </View>
          ))}
          {pipe.tasks.length === 0 && pipe.runs_unassigned.length > 0 && (
            <View style={[styles.taskCard, elevation.card]}>
              <Text style={styles.dim}>Runy bez przypisanego zadania:</Text>
              {pipe.runs_unassigned.map((r) => (
                <RunRow key={r.run_id} run={r} onKilled={() => loadPipe(pipe.role)} />
              ))}
            </View>
          )}
          {pipe.tasks.length === 0 && pipe.runs_unassigned.length === 0 && (
            <EmptyState icon="bot" title="Ten agent nie wykonał jeszcze żadnego zadania" />
          )}
        </View>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: spacing.lg },
  hRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  h1: { color: colors.text, fontSize: type.display, fontWeight: "800" },
  h2: { color: colors.text, fontSize: type.title, fontWeight: "700", marginBottom: spacing.md },
  dim: { color: colors.textDim, fontSize: type.meta },
  roleBar: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm, marginTop: spacing.md },
  roleChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 999,
    paddingHorizontal: 12,
    paddingVertical: 6,
    backgroundColor: colors.bgCard,
  },
  roleChipActive: { borderColor: colors.accent, backgroundColor: colors.accentDim },
  roleDot: { width: 6, height: 6, borderRadius: 3 },
  roleText: { color: colors.text, fontSize: type.body, fontWeight: "600" },
  taskCard: {
    backgroundColor: colors.bgCardAlt,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: radius.lg,
    padding: spacing.md,
    marginBottom: spacing.md,
  },
  taskHead: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: spacing.sm },
  taskTitle: { color: colors.text, fontSize: type.body, fontWeight: "700", fontFamily: colors.mono },
  taskTitleDim: { color: colors.textDim, fontSize: type.body, flex: 1 },
  substep: { fontFamily: colors.mono, fontSize: type.meta, color: colors.textDim },
  timeline: { marginTop: 2 },
  timelineItem: { flexDirection: "row" },
  timelineRail: { width: 2, backgroundColor: colors.borderStrong, marginRight: spacing.md, borderRadius: 1 },
});

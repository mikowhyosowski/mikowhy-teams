import React, { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { RunRow } from "../components/RunRow";
import { StatusPill } from "../components/StatusPill";
import { colors, spacing } from "../theme";
import type { AgentPipeline, ProcessRun } from "../types";

/** Ekran 2: Agenci & Execution Pipeline — karta agenta → pionowy pipeline zadań i runów. */
export function AgentsPipelineScreen() {
  const [roles, setRoles] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [pipe, setPipe] = useState<AgentPipeline | null>(null);

  const loadRoles = useCallback(async () => {
    try {
      const all = await api.runs({ limit: 200 });
      const uniq = [...new Set(all.runs.map((r: ProcessRun) => r.agent_role).filter(Boolean))] as string[];
      setRoles(uniq.sort());
    } catch {
      /* offline */
    }
  }, []);

  const loadPipe = useCallback(async (role: string) => {
    try {
      setPipe(await api.agentPipeline(role));
    } catch {
      setPipe(null);
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
      <Text style={styles.h1}>🤖 Agenci & Execution Pipeline</Text>
      <View style={styles.roleBar}>
        {roles.map((r) => (
          <Pressable
            key={r}
            style={[styles.roleChip, selected === r && { borderColor: colors.accent, backgroundColor: "#0E2A33" }]}
            onPress={() => setSelected(r)}
          >
            <Text style={[styles.roleText, selected === r && { color: colors.accent }]}>{r}</Text>
          </Pressable>
        ))}
        {roles.length === 0 && <Text style={styles.dim}>Brak zarejestrowanych runów — uruchom komendę przez sponsor-shell.</Text>}
      </View>

      {pipe && (
        <View style={{ marginTop: spacing.lg }}>
          <Text style={styles.h2}>
            Pipeline: <Text style={{ color: colors.accent }}>{pipe.role}</Text> ({pipe.runs_total} runów)
          </Text>
          {pipe.tasks.map((t) => (
            <View key={t.id} style={styles.taskCard}>
              <View style={styles.taskHead}>
                <StatusPill status={t.status} />
                <Text style={styles.taskTitle}>{t.id} — {t.title}</Text>
                {t.substep && <Text style={styles.substep}>krok: {t.substep}</Text>}
              </View>
              {(t.runs ?? []).map((r) => (
                <RunRow key={r.run_id} run={r} onKilled={() => loadPipe(pipe.role)} />
              ))}
              {(t.runs ?? []).length === 0 && <Text style={styles.dim}>brak runów procesów dla tego zadania</Text>}
            </View>
          ))}
          {pipe.tasks.length === 0 && pipe.runs_unassigned.length > 0 && (
            <View style={styles.taskCard}>
              <Text style={styles.dim}>Runy bez przypisanego zadania:</Text>
              {pipe.runs_unassigned.map((r) => (
                <RunRow key={r.run_id} run={r} onKilled={() => loadPipe(pipe.role)} />
              ))}
            </View>
          )}
          {pipe.tasks.length === 0 && pipe.runs_unassigned.length === 0 && (
            <Text style={styles.dim}>Ten agent nie wykonał jeszcze żadnego zadania.</Text>
          )}
        </View>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: spacing.lg },
  h1: { color: colors.text, fontSize: 22, fontWeight: "800" },
  h2: { color: colors.text, fontSize: 15, fontWeight: "700", marginBottom: spacing.md },
  dim: { color: colors.textDim, fontSize: 13 },
  roleBar: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm, marginTop: spacing.md },
  roleChip: {
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 999,
    paddingHorizontal: 12,
    paddingVertical: 6,
    backgroundColor: colors.bgCard,
  },
  roleText: { color: colors.text, fontSize: 13, fontWeight: "600" },
  taskCard: {
    backgroundColor: colors.bgCardAlt,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 12,
    padding: spacing.md,
    marginBottom: spacing.md,
  },
  taskHead: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: spacing.sm },
  taskTitle: { color: colors.text, fontSize: 13, fontWeight: "600", flex: 1 },
  substep: { fontFamily: colors.mono, fontSize: 11, color: colors.textDim },
});

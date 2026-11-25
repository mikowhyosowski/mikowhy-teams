import React, { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { EmptyState, ErrorState, Skeleton } from "../components/States";
import { colors, elevation, radius, spacing, type } from "../theme";
import type { ActionableTask } from "../types";

type Btn = { action: string; label: string; kind: "ok" | "danger" };

/**
 * Przyciski zmieniające status — nazwa i akcja zależą od tego, na co czeka zadanie:
 *  AWAITING_OWNER → „Ready” (approve → READY) / „Odrzuć” (decline → BLOCKED)
 *  READY_TO_MERGE → „Merguj” (merge → MERGED przez orchestrator)
 *  BLOCKED        → „Ready full back” (retry → READY, pełny cykl od zera)
 * Serwer dodatkowo waliduje allowed_from (available_actions z /api/sponsor).
 */
const STATUS_BUTTONS: Record<string, Btn[]> = {
  AWAITING_OWNER: [
    { action: "approve", label: "Ready", kind: "ok" },
    { action: "decline", label: "Odrzuć", kind: "danger" },
  ],
  ESCALATED: [
    { action: "approve", label: "Ready", kind: "ok" },
    { action: "decline", label: "Odrzuć", kind: "danger" },
  ],
  READY_TO_MERGE: [{ action: "merge", label: "Merguj", kind: "ok" }],
  BLOCKED: [{ action: "retry", label: "Ready full back", kind: "ok" }],
};

/** Ekran 3: Decyzje Sponsora — karty zadań czekających na akcję, z przyciskiem per status. */
export function ApprovalsScreen() {
  const [items, setItems] = useState<ActionableTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const sp = await api.sponsor();
      setItems((sp.actionable ?? sp.decisions ?? []) as ActionableTask[]);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const act = async (id: string, action: string, label: string) => {
    setMsg(null);
    try {
      await api.flowAction(id, action, action === "decline" ? { reason: "Odrzucone z Centrum Dowodzenia" } : {});
      setMsg(`${id}: ${label}`);
      load();
    } catch (e) {
      setMsg(`${id}: ${String(e)}`);
    }
  };

  return (
    <ScrollView style={styles.screen}>
      <View style={styles.hRow}>
        <Icon.inbox size={20} color={colors.warn} />
        <Text style={styles.h1}>Decyzje Sponsora</Text>
      </View>

      {error && <ErrorState message={`API: ${error}`} onRetry={load} />}
      {msg && <Text style={styles.msg}>{msg}</Text>}
      {items === null && !error && <Skeleton height={90} />}
      {items !== null && items.length === 0 && !error && (
        <EmptyState icon="check" title="Skrzynka decyzyjna pusta" hint="Brak zadań czekających na akcję Sponsora." />
      )}

      {items?.map((d) => {
        const all = STATUS_BUTTONS[d.status] ?? [];
        const allowed = d.available_actions;
        const buttons = allowed && allowed.length ? all.filter((b) => allowed.includes(b.action)) : all;
        return (
          <View key={d.id} style={[styles.card, elevation.card]}>
            <View style={styles.head}>
              <StatusPill status={d.status} />
              <Text style={styles.id}>{d.id}</Text>
            </View>
            <Text style={styles.title}>{d.title}</Text>
            <Text style={styles.meta}>rola: {d.role}{d.priority ? ` · priorytet: ${d.priority}` : ""}</Text>
            {buttons.length > 0 && (
              <View style={styles.actions}>
                {buttons.map((b) => (
                  <Pressable
                    key={b.action}
                    style={[styles.btn, b.kind === "ok" ? styles.ok : styles.no]}
                    onPress={() => act(d.id, b.action, b.label)}
                    accessibilityRole="button"
                  >
                    <Icon.check size={14} color={b.kind === "ok" ? colors.ok : colors.danger} />
                    <Text style={[styles.btnText, { color: b.kind === "ok" ? colors.ok : colors.danger }]}>
                      {b.label}
                    </Text>
                  </Pressable>
                ))}
              </View>
            )}
          </View>
        );
      })}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: spacing.lg },
  hRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm, marginBottom: spacing.lg },
  h1: { color: colors.text, fontSize: type.display, fontWeight: "800" },
  msg: { color: colors.accent, fontSize: type.body, marginBottom: spacing.sm },
  card: {
    backgroundColor: colors.bgCard,
    borderColor: colors.warn + "55",
    borderWidth: 1,
    borderRadius: radius.lg,
    padding: spacing.lg,
    marginBottom: spacing.md,
    maxWidth: 640,
  },
  head: { flexDirection: "row", alignItems: "center", gap: 10 },
  id: { color: colors.text, fontWeight: "700", fontSize: type.body, fontFamily: colors.mono },
  title: { color: colors.text, fontSize: type.body, marginTop: 6 },
  meta: { color: colors.textDim, fontFamily: colors.mono, fontSize: type.meta, marginTop: 4 },
  actions: { flexDirection: "row", gap: spacing.sm, marginTop: spacing.md },
  btn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderRadius: radius.sm,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderWidth: 1,
  },
  ok: { backgroundColor: "#052E22", borderColor: colors.ok },
  no: { backgroundColor: "#3B1220", borderColor: colors.danger },
  btnText: { fontWeight: "700", fontSize: type.body },
});

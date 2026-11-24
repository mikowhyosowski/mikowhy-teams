import React, { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { EmptyState, ErrorState, Skeleton } from "../components/States";
import { colors, elevation, radius, spacing, type } from "../theme";

type Decision = { id: string; title: string; role: string; substep: string | null };

/** Ekran 3: Decyzje Sponsora — karty AWAITING_OWNER z szybkimi akcjami. */
export function ApprovalsScreen() {
  const [items, setItems] = useState<Decision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const sp = await api.sponsor();
      setItems((sp.awaiting_owner ?? []) as Decision[]);
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

  const act = async (id: string, action: "approve" | "decline") => {
    setMsg(null);
    try {
      await api.taskAction(id, action, action === "decline" ? { reason: "Odrzucone z Centrum Dowodzenia" } : {});
      setMsg(`${id}: ${action === "approve" ? "zatwierdzone" : "odrzucone"}`);
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
        <EmptyState icon="check" title="Skrzynka decyzyjna pusta" hint="Brak zadań AWAITING_OWNER — fabryka sama sobie radzi." />
      )}

      {items?.map((d) => (
        <View key={d.id} style={[styles.card, elevation.card]}>
          <View style={styles.head}>
            <StatusPill status="AWAITING_OWNER" />
            <Text style={styles.id}>{d.id}</Text>
          </View>
          <Text style={styles.title}>{d.title}</Text>
          <Text style={styles.meta}>rola: {d.role}{d.substep ? ` · krok: ${d.substep}` : ""}</Text>
          <View style={styles.actions}>
            <Pressable style={[styles.btn, styles.ok]} onPress={() => act(d.id, "approve")} accessibilityRole="button">
              <Icon.check size={14} color={colors.ok} />
              <Text style={[styles.btnText, { color: colors.ok }]}>Zatwierdź</Text>
            </Pressable>
            <Pressable style={[styles.btn, styles.no]} onPress={() => act(d.id, "decline")} accessibilityRole="button">
              <Icon.x size={14} color={colors.danger} />
              <Text style={[styles.btnText, { color: colors.danger }]}>Odrzuć</Text>
            </Pressable>
          </View>
        </View>
      ))}
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

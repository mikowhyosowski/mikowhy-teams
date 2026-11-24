import React, { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { StatusPill } from "../components/StatusPill";
import { colors, spacing } from "../theme";
import type { SponsorView } from "../types";

type Decision = { id: string; title: string; role: string; substep: string | null };

/** Ekran 3: Decyzje Sponsora — czyste karty AWAITING_OWNER z szybkimi akcjami. */
export function ApprovalsScreen() {
  const [items, setItems] = useState<Decision[]>([]);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const sp = await api.sponsor();
      setItems((sp.awaiting_owner ?? []) as Decision[]);
    } catch {
      /* offline */
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
      await api.taskAction(id, action, action === "decline" ? { reason: "odrzucone z Centrum Dowodzenia" } : {});
      setMsg(`✅ ${id}: ${action}`);
      load();
    } catch (e) {
      setMsg(`⚠ ${id}: ${String(e)}`);
    }
  };

  return (
    <ScrollView style={styles.screen}>
      <Text style={styles.h1}>🧾 Decyzje Sponsora</Text>
      {msg && <Text style={styles.msg}>{msg}</Text>}
      {items.length === 0 && <Text style={styles.dim}>Brak oczekujących decyzji — AWAITING_OWNER pusto.</Text>}
      {items.map((d) => (
        <View key={d.id} style={styles.card}>
          <View style={styles.head}>
            <StatusPill status="AWAITING_OWNER" />
            <Text style={styles.title}>{d.id}</Text>
          </View>
          <Text style={styles.sub}>{d.title}</Text>
          <Text style={styles.meta}>
            rola: {d.role} {d.substep ? `· krok: ${d.substep}` : ""}
          </Text>
          <View style={styles.actions}>
            <Pressable style={[styles.btn, styles.ok]} onPress={() => act(d.id, "approve")}>
              <Text style={styles.btnTextOk}>✅ Zatwierdź</Text>
            </Pressable>
            <Pressable style={[styles.btn, styles.no]} onPress={() => act(d.id, "decline")}>
              <Text style={styles.btnTextNo}>❌ Odrzuć</Text>
            </Pressable>
          </View>
        </View>
      ))}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: spacing.lg },
  h1: { color: colors.text, fontSize: 22, fontWeight: "800", marginBottom: spacing.lg },
  dim: { color: colors.textDim, fontSize: 13 },
  msg: { color: colors.accent, fontSize: 13, marginBottom: spacing.sm },
  card: {
    backgroundColor: colors.bgCard,
    borderColor: "#F59E0B55",
    borderWidth: 1,
    borderRadius: 12,
    padding: spacing.lg,
    marginBottom: spacing.md,
    maxWidth: 640,
  },
  head: { flexDirection: "row", alignItems: "center", gap: 10 },
  title: { color: colors.text, fontWeight: "700", fontSize: 14 },
  sub: { color: colors.text, fontSize: 13, marginTop: 6 },
  meta: { color: colors.textDim, fontFamily: colors.mono, fontSize: 11, marginTop: 4 },
  actions: { flexDirection: "row", gap: spacing.sm, marginTop: spacing.md },
  btn: { borderRadius: 8, paddingHorizontal: 14, paddingVertical: 8 },
  ok: { backgroundColor: "#052E22", borderColor: "#34D399", borderWidth: 1 },
  no: { backgroundColor: "#3B1220", borderColor: "#F87171", borderWidth: 1 },
  btnTextOk: { color: "#34D399", fontWeight: "700", fontSize: 13 },
  btnTextNo: { color: "#F87171", fontWeight: "700", fontSize: 13 },
});

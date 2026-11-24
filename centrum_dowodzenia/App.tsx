import React, { useState } from "react";
import { Pressable, SafeAreaView, StyleSheet, Text, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { colors, spacing } from "./src/theme";
import { CommandCenterScreen } from "./src/screens/CommandCenterScreen";
import { AgentsPipelineScreen } from "./src/screens/AgentsPipelineScreen";
import { ApprovalsScreen } from "./src/screens/ApprovalsScreen";

type Tab = "command" | "agents" | "approvals";

const TABS: { key: Tab; label: string }[] = [
  { key: "command", label: "🛰️ Centrum Operacyjne" },
  { key: "agents", label: "🤖 Agenci & Pipeline" },
  { key: "approvals", label: "🧾 Decyzje Sponsora" },
];

/**
 * Centrum Dowodzenia — dashboard fabryki (Expo + React Native Web).
 * Dziś: błyskawiczny web pod API silnika. Jutro: natywna apka iOS/Android
 * z tego samego kodu (bez przepisywania komponentów).
 */
export default function App() {
  const [tab, setTab] = useState<Tab>("command");

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar style="light" />
      <View style={styles.header}>
        <Text style={styles.brand}>🏭 CENTRUM DOWODZENIA</Text>
        <View style={styles.tabs}>
          {TABS.map((t) => (
            <Pressable
              key={t.key}
              style={[styles.tab, tab === t.key && styles.tabActive]}
              onPress={() => setTab(t.key)}
            >
              <Text style={[styles.tabText, tab === t.key && styles.tabTextActive]}>{t.label}</Text>
            </Pressable>
          ))}
        </View>
      </View>
      <View style={styles.body}>
        {tab === "command" && <CommandCenterScreen />}
        {tab === "agents" && <AgentsPipelineScreen />}
        {tab === "approvals" && <ApprovalsScreen />}
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderBottomColor: colors.border,
    borderBottomWidth: 1,
    backgroundColor: colors.bgCard,
    flexWrap: "wrap",
    gap: spacing.sm,
  },
  brand: { color: colors.accent, fontWeight: "800", fontSize: 16, letterSpacing: 1 },
  tabs: { flexDirection: "row", gap: spacing.sm },
  tab: { paddingHorizontal: 12, paddingVertical: 7, borderRadius: 8, borderWidth: 1, borderColor: colors.border },
  tabActive: { borderColor: colors.accent, backgroundColor: "#0E2A33" },
  tabText: { color: colors.textDim, fontSize: 13, fontWeight: "600" },
  tabTextActive: { color: colors.accent },
  body: { flex: 1 },
});

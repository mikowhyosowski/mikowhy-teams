import React, { useEffect, useState } from "react";
import { Pressable, SafeAreaView, StyleSheet, Text, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { registerRootComponent } from "expo";
import { colors, elevation, radius, spacing, type } from "./src/theme";
import { Icon } from "./src/components/Icon";
import { CommandCenterScreen } from "./src/screens/CommandCenterScreen";
import { AgentsPipelineScreen } from "./src/screens/AgentsPipelineScreen";
import { ApprovalsScreen } from "./src/screens/ApprovalsScreen";

type Tab = "command" | "agents" | "approvals";

const TABS: { key: Tab; label: string; icon: keyof typeof Icon }[] = [
  { key: "command", label: "Centrum Operacyjne", icon: "radar" },
  { key: "agents", label: "Agenci & Pipeline", icon: "bot" },
  { key: "approvals", label: "Decyzje Sponsora", icon: "inbox" },
];

/**
 * Centrum Dowodzenia — dashboard fabryki (Expo + React Native Web).
 * Dziś: webowy dashboard pod API silnika. Jutro: natywna apka iOS/Android
 * z tego samego kodu. Nagłówek: znak fabryki, segmented tabs, stan połączenia + zegar UTC.
 */
export default function App() {
  const [tab, setTab] = useState<Tab>("command");
  const [online, setOnline] = useState<boolean | null>(null);
  const [clock, setClock] = useState("");

  useEffect(() => {
    const tick = () => setClock(new Date().toISOString().slice(11, 19) + "Z");
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    // lekki health-check — pigułka połączenia w nagłówku
    const check = async () => {
      try {
        await fetch(`${(process.env.EXPO_PUBLIC_API_URL ?? "").replace(/\/$/, "")}/api/pipeline/runs?limit=1`);
        setOnline(true);
      } catch {
        setOnline(false);
      }
    };
    check();
    const t = setInterval(check, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar style="light" />
      <View style={[styles.header, elevation.card]}>
        <View style={styles.brand}>
          <View style={styles.brandMark}>
            <Icon.radar size={17} color={colors.accent} />
          </View>
          <Text style={styles.brandText}>CENTRUM DOWODZENIA</Text>
        </View>

        <View style={styles.tabs}>
          {TABS.map((t) => {
            const I = Icon[t.icon];
            const active = tab === t.key;
            return (
              <Pressable
                key={t.key}
                style={[styles.tab, active && styles.tabActive]}
                onPress={() => setTab(t.key)}
                accessibilityRole="tab"
              >
                <I size={14} color={active ? colors.accent : colors.textDim} />
                <Text style={[styles.tabText, active && styles.tabTextActive]}>{t.label}</Text>
              </Pressable>
            );
          })}
        </View>

        <View style={styles.status}>
          <View style={styles.connPill}>
            {online === null ? (
              <Icon.refresh size={12} color={colors.textDim} />
            ) : online ? (
              <Icon.wifi size={12} color={colors.ok} />
            ) : (
              <Icon.wifiOff size={12} color={colors.danger} />
            )}
            <Text style={[styles.connText, { color: online === false ? colors.danger : colors.ok }]}>
              {online === null ? "łączę…" : online ? "API online" : "API offline"}
            </Text>
          </View>
          <Text style={styles.clock}>{clock}</Text>
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
    paddingVertical: spacing.sm,
    borderBottomColor: colors.border,
    borderBottomWidth: 1,
    backgroundColor: colors.bgCard,
    flexWrap: "wrap",
    gap: spacing.sm,
  },
  brand: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  brandMark: {
    width: 30,
    height: 30,
    borderRadius: radius.sm,
    backgroundColor: colors.accentDim,
    borderWidth: 1,
    borderColor: colors.accent + "55",
    alignItems: "center",
    justifyContent: "center",
  },
  brandText: { color: colors.accent, fontWeight: "800", fontSize: 14, letterSpacing: 1.4 },
  tabs: { flexDirection: "row", gap: 4, backgroundColor: colors.bgInset, borderRadius: radius.md, padding: 3, borderWidth: 1, borderColor: colors.border },
  tab: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: radius.sm,
  },
  tabActive: { backgroundColor: colors.bgCard, borderWidth: 1, borderColor: colors.borderStrong },
  tabText: { color: colors.textDim, fontSize: type.body, fontWeight: "600" },
  tabTextActive: { color: colors.accent },
  status: { flexDirection: "row", alignItems: "center", gap: spacing.md },
  connPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 9,
    paddingVertical: 3,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.bgInset,
  },
  connText: { fontSize: type.meta, fontWeight: "700" },
  clock: { color: colors.textDim, fontFamily: colors.mono, fontSize: type.meta },
  body: { flex: 1 },
});

registerRootComponent(App);

import React, { useEffect, useState } from "react";
import { ActivityIndicator, Platform, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { colors } from "../theme";

/** Rozwijany podgląd logu runu (stdout+stderr, monospaced, ANSI-safe plain text). */
export function LogViewer({ runId }: { runId: string }) {
  const [log, setLog] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLog(null);
    api.runLog(runId).then(
      (t) => alive && setLog(t),
      (e) => alive && setErr(String(e)),
    );
    return () => {
      alive = false;
    };
  }, [runId]);

  return (
    <View style={styles.wrap}>
      {log === null && !err && <ActivityIndicator color={colors.accent} />}
      {err && <Text style={styles.err}>⚠ {err}</Text>}
      {log !== null && (
        <ScrollView style={styles.scroll} horizontal={Platform.OS === "web"}>
          <Text style={styles.text}>{log || "(log pusty)"}</Text>
        </ScrollView>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    marginTop: 8,
    backgroundColor: "#05070B",
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 8,
    padding: 10,
  },
  scroll: { maxHeight: 320 },
  text: { fontFamily: colors.mono, fontSize: 11.5, color: "#C9D6E8", lineHeight: 16 },
  err: { color: "#F87171", fontSize: 12 },
});

import React from "react";
import { StyleSheet, Text, View } from "react-native";
import { colors, statusColorOf } from "../theme";

/** Semantyczna pigułka statusu; RUNNING pulsuje (web: animacja CSS). */
export function StatusPill({ status }: { status: string }) {
  const c = statusColorOf(status);
  return (
    <View style={[styles.pill, { borderColor: c + "55", backgroundColor: c + "1A" }]}>
      {status === "RUNNING" && <View style={[styles.dot, { backgroundColor: c }]} />}
      <Text style={[styles.text, { color: c }]}>{status}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  pill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    alignSelf: "flex-start",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
    borderWidth: 1,
  },
  dot: { width: 7, height: 7, borderRadius: 4 },
  text: { fontSize: 11, fontWeight: "700", letterSpacing: 0.6 },
});

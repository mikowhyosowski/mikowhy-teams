import React, { useEffect, useRef } from "react";
import { Animated, StyleSheet, Text, View } from "react-native";
import { colors, motion, radius, type } from "../theme";
import { isLive, statusColorOf } from "../theme";

/** Pigułka statusu; status „żywy” (RUNNING/QUEUED) ma pulsującą kropkę (Animated loop). */
export function StatusPill({ status }: { status: string }) {
  const c = statusColorOf(status);
  const pulse = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (!isLive(status)) return;
    const a = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 1, duration: motion.slow, useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 0, duration: motion.slow, useNativeDriver: true }),
      ]),
    );
    a.start();
    return () => a.stop();
  }, [status, pulse]);

  const scale = pulse.interpolate({ inputRange: [0, 1], outputRange: [1, 1.6] });
  const opacity = pulse.interpolate({ inputRange: [0, 1], outputRange: [0.9, 0.25] });

  return (
    <View style={[styles.pill, { borderColor: c + "55", backgroundColor: c + "14" }]}>
      {isLive(status) ? (
        <Animated.View style={[styles.dot, { backgroundColor: c, transform: [{ scale }], opacity }]} />
      ) : (
        <View style={[styles.dot, { backgroundColor: c }]} />
      )}
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
    paddingHorizontal: 9,
    paddingVertical: 3,
    borderRadius: radius.pill,
    borderWidth: 1,
  },
  dot: { width: 7, height: 7, borderRadius: 4 },
  text: { fontSize: type.meta, fontWeight: "700", letterSpacing: 0.7 },
});

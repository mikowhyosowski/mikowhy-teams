import React, { useEffect, useRef } from "react";
import { Animated, Pressable, StyleSheet, Text, View } from "react-native";
import { colors, motion, radius, spacing, type } from "../theme";
import { Icon } from "./Icon";

/** Szkielet ładowania — pulsujące bloki zamiast kręcącego się spinnera. */
export function Skeleton({ width = "100%", height = 14, style }: { width?: number | string; height?: number; style?: object }) {
  const pulse = useRef(new Animated.Value(0.35)).current;
  useEffect(() => {
    const a = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 1, duration: motion.slow, useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 0.35, duration: motion.slow, useNativeDriver: true }),
      ]),
    );
    a.start();
    return () => a.stop();
  }, [pulse]);
  return (
    <Animated.View
      style={[styles.skel, { width, height, opacity: pulse }, style]}
    />
  );
}

/** Stan błędu API jako pierwsza klasa UI — z akcją retry. */
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <View style={styles.box}>
      <Icon.alert size={20} color={colors.danger} />
      <Text style={styles.errText}>{message}</Text>
      {onRetry && (
        <Pressable style={styles.retryBtn} onPress={onRetry}>
          <Icon.refresh size={13} color={colors.accent} />
          <Text style={styles.retryText}>Spróbuj ponownie</Text>
        </Pressable>
      )}
    </View>
  );
}

/** Pusty stan — ikona + konkretna informacja + opcjonalna akcja (bez „brak danych”). */
export function EmptyState({
  icon, title, hint, action, onAction,
}: {
  icon: keyof typeof Icon;
  title: string;
  hint?: string;
  action?: string;
  onAction?: () => void;
}) {
  const I = Icon[icon];
  return (
    <View style={styles.box}>
      <I size={26} color={colors.textFaint} />
      <Text style={styles.emptyTitle}>{title}</Text>
      {hint && <Text style={styles.hint}>{hint}</Text>}
      {action && onAction && (
        <Pressable style={styles.retryBtn} onPress={onAction}>
          <Text style={styles.retryText}>{action}</Text>
        </Pressable>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  skel: { backgroundColor: colors.bgCard, borderRadius: radius.sm, borderWidth: 1, borderColor: colors.border },
  box: {
    alignItems: "center",
    gap: spacing.sm,
    padding: spacing.xl,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: colors.border,
    backgroundColor: colors.bgCardAlt,
  },
  errText: { color: colors.danger, fontSize: type.body, textAlign: "center" },
  emptyTitle: { color: colors.textDim, fontSize: type.body, fontWeight: "600" },
  hint: { color: colors.textFaint, fontSize: type.meta, textAlign: "center" },
  retryBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    marginTop: spacing.xs,
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.accent + "66",
    backgroundColor: colors.accentDim,
  },
  retryText: { color: colors.accent, fontSize: type.meta, fontWeight: "700" },
});

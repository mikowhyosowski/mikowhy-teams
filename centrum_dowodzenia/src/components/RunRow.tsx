import React, { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { api } from "../api";
import { colors, statusColorOf } from "../theme";
import type { ProcessRun } from "../types";
import { LogViewer } from "./LogViewer";
import { StatusPill } from "./StatusPill";

function fmtDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Pojedynczy run procesu w pipeline'u — status, PID/PGID, czas, akcja kill, log. */
export function RunRow({ run, onKilled }: { run: ProcessRun; onKilled?: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const live = run.status === "RUNNING" || run.status === "QUEUED";

  const kill = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.killRun(run.run_id);
      setMsg(r.ok ? `⛔ zabito (${r.status})` : `⚠ ${JSON.stringify(r)}`);
      onKilled?.();
    } catch (e) {
      setMsg(`⚠ ${String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.row}>
      <Pressable style={styles.head} onPress={() => setOpen((o) => !o)}>
        <StatusPill status={run.status} />
        <Text style={styles.cmd} numberOfLines={1}>
          {open ? "▾" : "▸"} ${run.command}
        </Text>
        <Text style={styles.meta}>
          pid {run.pid ?? "—"} · pgid {run.pgid ?? "—"} · {fmtDuration(run.duration_ms)}
          {run.started_at ? ` · ${run.started_at.replace("T", " ").slice(0, 19)}Z` : ""}
        </Text>
        {live && (
          <Pressable
            style={[styles.killBtn, busy && { opacity: 0.5 }]}
            disabled={busy}
            onPress={kill}
          >
            <Text style={styles.killText}>{busy ? "…" : "⛔ Ubierz"}</Text>
          </Pressable>
        )}
      </Pressable>
      {run.substep && <Text style={styles.substep}>krok: {run.substep}</Text>}
      {msg && <Text style={styles.msg}>{msg}</Text>}
      {open && (
        <View>
          {run.stdout_tail ? (
            <Text style={styles.tail}>stdout: {run.stdout_tail.slice(-300)}</Text>
          ) : null}
          {run.stderr_tail ? (
            <Text style={[styles.tail, { color: "#F87171" }]}>
              stderr: {run.stderr_tail.slice(-300)}
            </Text>
          ) : null}
          <LogViewer runId={run.run_id} />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    borderColor: colors.border,
    borderWidth: 1,
    borderLeftWidth: 3,
    borderLeftColor: statusColorOf("RUNNING"),
    borderRadius: 8,
    padding: 10,
    marginBottom: 8,
    backgroundColor: colors.bgCard,
  },
  head: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap" },
  cmd: { fontFamily: colors.mono, fontSize: 12, color: colors.text, flex: 1 },
  meta: { fontFamily: colors.mono, fontSize: 11, color: colors.textDim },
  killBtn: {
    backgroundColor: "#3B1220",
    borderColor: "#F87171",
    borderWidth: 1,
    borderRadius: 6,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  killText: { color: "#F87171", fontSize: 12, fontWeight: "700" },
  substep: { fontFamily: colors.mono, fontSize: 11, color: colors.textDim, marginTop: 4 },
  msg: { fontSize: 12, color: colors.accent, marginTop: 4 },
  tail: { fontFamily: colors.mono, fontSize: 11, color: "#9FB3CC", marginTop: 6 },
});

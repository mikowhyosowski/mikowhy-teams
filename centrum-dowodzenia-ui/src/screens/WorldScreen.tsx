/**
 * WorldScreen — pełnoekranowa podstrona „Świat Fabryki (Live World)”.
 *
 * - Scena React Three Fiber z przełącznikiem dwóch światów (Chronos Foundry ↔ Diorama).
 * - Płynne presety kamery (lerp do celu) + OrbitControls (swoboda).
 * - Klikalne obiekty 3D (raycast) → panel HUD glassmorphism.
 * - Demo controls sterujące symulatorem silnika (mockEngine zgodny z flow.md).
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls, Html } from "@react-three/drei";
import * as THREE from "three";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { colors, radius, spacing, type } from "../theme";
import { MockEngine, type EngineSnapshot, type TaskDTO } from "../world/mockEngine";
import { ChronosWorld } from "../world/ChronosWorld";
import { DioramaWorld } from "../world/DioramaWorld";
import { ZONES, statusZone, slotPos, type WorldVariant } from "../world/worldLayout";

type Preset = "overview" | "reactor" | "tower" | "follow";

const CAM_PRESETS: Record<Preset, { pos: [number, number, number]; target: [number, number, number] }> = {
  overview: { pos: [3, 15, 22], target: [3, 0, 0] },
  reactor: { pos: [2.5, 3.2, 7.5], target: [ZONES.reactor.pos[0], 1.2, ZONES.reactor.pos[2]] },
  tower: { pos: [2.5, 4.2, 14], target: [ZONES.tower.pos[0], 3, ZONES.tower.pos[2]] },
  follow: { pos: [-3, 4.5, 9], target: [ZONES.workshop.pos[0], 1, ZONES.workshop.pos[2]] },
};

function CameraRig({ preset, onArrive }: { preset: Preset; onArrive: () => void }) {
  const { camera } = useThree();
  const target = useRef(new THREE.Vector3(...CAM_PRESETS.overview.target));
  const goal = useRef(new THREE.Vector3(...CAM_PRESETS[preset].pos));
  const done = useRef(0);

  useEffect(() => {
    goal.current.set(...CAM_PRESETS[preset].pos);
    target.current.set(...CAM_PRESETS[preset].target);
    done.current = 0;
  }, [preset]);

  useFrame((_, dt) => {
    const before = camera.position.distanceTo(goal.current);
    camera.position.lerp(goal.current, Math.min(1, dt * 3.2));
    camera.lookAt(target.current);
    const after = camera.position.distanceTo(goal.current);
    if (before < 0.06 && after < 0.06 && !done.current) {
      done.current = 1;
      onArrive();
    }
  });
  return null;
}

function useEngine() {
  const engine = useMemo(() => new MockEngine(), []);
  const [snap, setSnap] = useState<EngineSnapshot>(() => engine.snapshot());
  const [flash, setFlash] = useState(0);
  useEffect(() => {
    let last = performance.now();
    const iv = setInterval(() => {
      const now = performance.now();
      engine.tick(now - last);
      last = now;
      const s = engine.snapshot();
      setSnap(s);
      const merged = s.events.filter((e) => e.kind === "merge").length;
      if (merged) setFlash(1);
    }, 600);
    return () => clearInterval(iv);
  }, [engine]);
  useEffect(() => {
    if (flash <= 0) return;
    const t = setTimeout(() => setFlash((f) => Math.max(0, f - 0.1)), 60);
    return () => clearTimeout(t);
  }, [flash]);
  return { engine, snap, setSnap, flash };
}

export function WorldScreen() {
  const { engine, snap, setSnap, flash } = useEngine();
  const [world, setWorld] = useState<WorldVariant>("chronos");
  const [preset, setPreset] = useState<Preset>("overview");
  const [selected, setSelected] = useState<TaskDTO | null>(null);

  const onSelect = useCallback((id: string | null) => {
    if (!id) return setSelected(null);
    const t = snap.tasks.find((x) => x.id === id) ?? null;
    setSelected(t);
  }, [snap.tasks]);

  // aktualizuj zaznaczone zadanie, żeby HUD pokazywał świeży stan
  useEffect(() => {
    if (!selected) return;
    const fresh = snap.tasks.find((t) => t.id === selected.id);
    if (fresh && fresh !== selected) setSelected(fresh);
    if (!fresh) setSelected(null);
  }, [snap, selected]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const t of snap.tasks) c[t.status] = (c[t.status] ?? 0) + 1;
    return c;
  }, [snap.tasks]);

  return (
    <View style={styles.root}>
      <Canvas
        shadows={world === "diorama"}
        dpr={[1, 1.6]}
        camera={{ position: CAM_PRESETS.overview.pos, fov: 46, near: 0.1, far: 200 }}
        onPointerMissed={() => setSelected(null)}
        gl={{ antialias: true, powerPreference: "high-performance" }}
      >
        <CameraRig preset={preset} onArrive={() => {}} />
        {world === "chronos" ? (
          <ChronosWorld snapshot={snap} flash={flash} />
        ) : (
          <DioramaWorld snapshot={snap} flash={flash} />
        )}
        {/* warstwa klikalna: niewidzialne kule w miejscach zadań (raycast) */}
        <ClickableTasks tasks={snap.tasks} onSelect={onSelect} />
        <OrbitControls enablePan enableZoom enableRotate minDistance={6} maxDistance={48} target={[3, 1, 0]} />
      </Canvas>

      {/* --- HUD: góra --- */}
      <View style={styles.topBar} pointerEvents="box-none">
        <View style={styles.brand}>
          <Text style={styles.brandText}>🌍 ŚWIAT FABRYKI · LIVE</Text>
          <Text style={styles.brandSub}>
            {world === "chronos" ? "Chronos Foundry" : "Miasteczko Rzemieślnicze"} · {snap.tasks.length} obiektów
          </Text>
        </View>

        <View style={styles.switcher}>
          {(["chronos", "diorama"] as WorldVariant[]).map((w) => (
            <Pressable key={w} onPress={() => setWorld(w)} style={[styles.switchBtn, world === w && styles.switchBtnActive]}>
              <Text style={[styles.switchText, world === w && styles.switchTextActive]}>
                {w === "chronos" ? "A · Odlewnia" : "B · Diorama"}
              </Text>
            </Pressable>
          ))}
        </View>
      </View>

      {/* --- HUD: presety kamery (lewa) --- */}
      <View style={styles.presets}>
        {([
          ["overview", "◱ Overview"],
          ["reactor", "◉ Reaktor LLM"],
          ["tower", "♛ Wieża Decyzji"],
          ["follow", "➤ Śledź agenta"],
        ] as [Preset, string][]).map(([p, label]) => (
          <Pressable key={p} onPress={() => setPreset(p)} style={[styles.presetBtn, preset === p && styles.presetActive]}>
            <Text style={[styles.presetText, preset === p && styles.presetTextActive]}>{label}</Text>
          </Pressable>
        ))}
      </View>

      {/* --- HUD: legenda statusów (prawa) --- */}
      <View style={styles.legend}>
        {[
          ["READY", "kolejka", "#22D3EE"],
          ["RUNNING", "worktree", "#38BDF8"],
          ["IN_REVIEW", "sędzia", "#A78BFA"],
          ["READY_TO_MERGE", "tor", "#818CF8"],
          ["MERGED", "skarbiec", "#34D399"],
          ["COOLING", "chłodzenie", "#60A5FA"],
          ["AWAITING_OWNER", "Ty", "#F59E0B"],
        ].map(([st, what, c]) => (
          <View key={st} style={styles.legendRow}>
            <View style={[styles.dot, { backgroundColor: c }]} />
            <Text style={styles.legendText}>{st} · {what} · {counts[st] ?? 0}</Text>
          </View>
        ))}
      </View>

      {/* --- HUD: Reaktor LLM + Decyzje --- */}
      <View style={styles.llmCard}>
        <Text style={styles.llmTitle}>⟢ Reaktor Wyroczni</Text>
        <Text style={styles.llmLine}>
          {snap.llm.running ? `GENERUJE · ${snap.llm.running.model}` : "bezczynny · brama wolna"}
        </Text>
        <Text style={styles.llmMeta}>kolejka: {snap.llm.queue.length} · flock: {snap.llm.lock ? "ZAJĘTY" : "wolny"}</Text>
      </View>

      {/* --- Panel zaznaczonego zadania --- */}
      {selected && (
        <View style={styles.detail}>
          <View style={styles.detailHead}>
            <Text style={styles.detailId}>{selected.id}</Text>
            <Pressable onPress={() => setSelected(null)}><Text style={styles.detailClose}>✕</Text></Pressable>
          </View>
          <Text style={styles.detailRow}>status: <Text style={styles.mono}>{selected.status}</Text></Text>
          <Text style={styles.detailRow}>etap: <Text style={styles.mono}>{selected.substep}</Text> ({Math.round(selected.progress * 100)}%)</Text>
          <Text style={styles.detailRow}>rola: <Text style={styles.mono}>{selected.role}</Text></Text>
          <Text style={styles.detailRow}>priorytet: <Text style={styles.mono}>{selected.priority}</Text></Text>
          <Text style={styles.detailRow}>worktree: <Text style={styles.mono}>{selected.worktree ? "izolowany boks" : "—"}</Text></Text>
          <Text style={styles.detailRow}>scope: <Text style={styles.mono}>{selected.scope_paths.join(", ")}</Text></Text>
          <Text style={styles.detailAge}>w statusie: {Math.round(selected.age_s)}s</Text>
          <View style={styles.detailActions}>
            <Pressable style={styles.detailBtn} onPress={() => { engine.demo("escalate"); }}>
              <Text style={styles.detailBtnText}>Eskaluj</Text>
            </Pressable>
            <Pressable style={styles.detailBtnOk} onPress={() => { engine.demo("merge"); }}>
              <Text style={styles.detailBtnText}>Merguj</Text>
            </Pressable>
          </View>
        </View>
      )}

      {/* --- Demo controls (dół) --- */}
      <ScrollView horizontal style={styles.demoBar} contentContainerStyle={styles.demoInner}>
        {([
          ["spawn_p0", "+ Nowe zadanie P0"],
          ["llm", "⚡ Zapytanie do LLM"],
          ["qg_fail", "🚨 Błąd QG → COOLING"],
          ["escalate", "👑 Eskaluj do Sponsora"],
          ["merge", "🚀 Merge do Master"],
        ] as [Parameters<MockEngine["demo"]>[0], string][]).map(([a, label]) => (
          <Pressable key={a} onPress={() => { engine.demo(a); setSnap(engine.snapshot()); }} style={styles.demoBtn}>
            <Text style={styles.demoText}>{label}</Text>
          </Pressable>
        ))}
      </ScrollView>
    </View>
  );
}

/** Niewidzialne hity raycastów nad obiektami zadań. */
function ClickableTasks({ tasks, onSelect }: { tasks: TaskDTO[]; onSelect: (id: string | null) => void }) {
  return (
    <group>
      {tasks.map((t) => {
        const zone = t.status;
        const idx = tasks.filter((x) => x.status === zone).indexOf(t);
        const list = tasks.filter((x) => x.status === zone);
        const z = statusZone(t.status);
        const p = slotPos(z, Math.max(0, idx), Math.max(4, list.length));
        return (
          <mesh
            key={t.id}
            position={[p[0], 0.55, p[2]]}
            visible={false}
            userData={{ taskId: t.id }}
            onPointerDown={(e) => {
              e.stopPropagation();
              onSelect(t.id);
            }}
          >
            <sphereGeometry args={[0.5, 8, 8]} />
            <meshBasicMaterial transparent opacity={0} />
          </mesh>
        );
      })}
    </group>
  );
}

export default WorldScreen;

const glass = "rgba(11,16,24,0.72)";

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#070A12" },
  topBar: {
    position: "absolute", top: 0, left: 0, right: 0,
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    padding: spacing.lg,
  },
  brand: { backgroundColor: glass, borderRadius: radius.md, paddingHorizontal: 12, paddingVertical: 8, borderWidth: 1, borderColor: colors.border },
  brandText: { color: colors.accent, fontWeight: "800", fontSize: type.body, letterSpacing: 1.2 },
  brandSub: { color: colors.textDim, fontSize: type.meta, marginTop: 2 },
  switcher: { flexDirection: "row", backgroundColor: glass, borderRadius: radius.pill, padding: 4, borderWidth: 1, borderColor: colors.border, gap: 4 },
  switchBtn: { paddingHorizontal: 14, paddingVertical: 7, borderRadius: radius.pill },
  switchBtnActive: { backgroundColor: colors.accentDim, borderWidth: 1, borderColor: colors.accent + "77" },
  switchText: { color: colors.textDim, fontSize: type.meta, fontWeight: "700" },
  switchTextActive: { color: colors.accent },
  presets: { position: "absolute", left: spacing.lg, top: 96, gap: 6 },
  presetBtn: { backgroundColor: glass, borderRadius: radius.sm, paddingHorizontal: 10, paddingVertical: 6, borderWidth: 1, borderColor: colors.border },
  presetActive: { borderColor: colors.accent, backgroundColor: colors.accentDim },
  presetText: { color: colors.textDim, fontSize: type.meta, fontWeight: "600" },
  presetTextActive: { color: colors.accent },
  legend: { position: "absolute", right: spacing.lg, top: 96, backgroundColor: glass, borderRadius: radius.md, padding: 10, borderWidth: 1, borderColor: colors.border, gap: 4 },
  legendRow: { flexDirection: "row", alignItems: "center", gap: 6 },
  dot: { width: 7, height: 7, borderRadius: 4 },
  legendText: { color: colors.textDim, fontSize: 10, fontFamily: colors.mono },
  llmCard: {
    position: "absolute", right: spacing.lg, bottom: 96,
    backgroundColor: glass, borderRadius: radius.md, padding: 12,
    borderWidth: 1, borderColor: "#A855F755", maxWidth: 240,
  },
  llmTitle: { color: "#C4A2FF", fontWeight: "800", fontSize: type.body },
  llmLine: { color: colors.text, fontSize: type.meta, marginTop: 4 },
  llmMeta: { color: colors.textFaint, fontSize: 10, fontFamily: colors.mono, marginTop: 2 },
  detail: {
    position: "absolute", left: spacing.lg, bottom: 96, width: 320,
    backgroundColor: "rgba(9,13,20,0.88)", borderRadius: radius.lg, padding: 14,
    borderWidth: 1, borderColor: colors.accent + "55", gap: 3,
  },
  detailHead: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  detailId: { color: colors.accent, fontFamily: colors.mono, fontWeight: "800", fontSize: type.body },
  detailClose: { color: colors.textDim, fontSize: 14 },
  detailRow: { color: colors.textDim, fontSize: type.meta },
  detailAge: { color: colors.textFaint, fontSize: 10, marginTop: 4, fontFamily: colors.mono },
  mono: { color: colors.text, fontFamily: colors.mono },
  detailActions: { flexDirection: "row", gap: 8, marginTop: 10 },
  detailBtn: { flex: 1, paddingVertical: 7, borderRadius: radius.sm, borderWidth: 1, borderColor: colors.warn, backgroundColor: "#2A1F05", alignItems: "center" },
  detailBtnOk: { flex: 1, paddingVertical: 7, borderRadius: radius.sm, borderWidth: 1, borderColor: colors.ok, backgroundColor: "#052E22", alignItems: "center" },
  detailBtnText: { color: colors.text, fontWeight: "700", fontSize: type.meta },
  demoBar: {
    position: "absolute", left: 0, right: 0, bottom: 0, maxHeight: 76,
    backgroundColor: "rgba(6,9,14,0.85)", borderTopWidth: 1, borderTopColor: colors.border,
  },
  demoInner: { flexDirection: "row", alignItems: "center", gap: 8, padding: 12 },
  demoBtn: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: radius.sm, borderWidth: 1, borderColor: colors.borderStrong, backgroundColor: colors.bgCard },
  demoText: { color: colors.text, fontSize: type.meta, fontWeight: "600" },
});

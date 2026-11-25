/**
 * ChronosWorld — Propozycja A: „Orbitalna Odlewnia / Chronos Foundry”.
 *
 * Retro-futurystyczna kinetyczna maszyneria (Teenage Engineering / Monument Valley):
 *  - zadania = świetliste rdzenie energii jadące po szynach magnetycznych,
 *  - stacje = śluzy QG i warsztaty odlewnicze,
 *  - Reaktor Wyroczni = potężny walec plazmowy z JEDNĄ bramą (flock .agents/llm.lock),
 *  - COOLING = kriostaza (błękitna mgła), AWAITING_OWNER = złota winda Sponsora.
 * Czyste geometrie, zero tekstur — <60 draw calli, 60 FPS na iGPU.
 */
import React, { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import type { EngineSnapshot, TaskDTO } from "./mockEngine";
import { ZONES, statusZone, slotPos } from "./worldLayout";

const COLORS = {
  bg: "#070A12",
  metal: "#26303F",
  metalLight: "#3B4859",
  accent: "#22D3EE",
  plasma: "#A855F7",
  ok: "#34D399",
  warn: "#F59E0B",
  danger: "#F87171",
  cold: "#60A5FA",
};

const PRIO_COLOR: Record<string, string> = { P0: "#F87171", P1: "#F59E0B", P2: "#22D3EE", P3: "#94A3B8" };

function Rail({ from, to }: { from: [number, number, number]; to: [number, number, number] }) {
  const mid: [number, number, number] = [(from[0] + to[0]) / 2, 0.08, (from[2] + to[2]) / 2];
  const len = Math.hypot(to[0] - from[0], to[2] - from[2]);
  const angle = Math.atan2(to[2] - from[2], to[0] - from[0]);
  return (
    <mesh position={mid} rotation={[0, -angle, 0]}>
      <boxGeometry args={[len, 0.06, 0.08]} />
      <meshStandardMaterial color={COLORS.metalLight} metalness={0.9} roughness={0.35} emissive={COLORS.accent} emissiveIntensity={0.08} />
    </mesh>
  );
}

function Platform({ pos, size, label, tint = COLORS.metal }: { pos: [number, number, number]; size: [number, number, number]; label: string; tint?: string }) {
  return (
    <group position={pos}>
      <mesh position={[0, 0.02, 0]} receiveShadow>
        <boxGeometry args={[size[0], size[1], size[2]]} />
        <meshStandardMaterial color={tint} metalness={0.7} roughness={0.5} />
      </mesh>
      {/* obwódka strefy */}
      <mesh position={[0, 0.12, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[Math.min(size[0], size[2]) * 0.45, Math.min(size[0], size[2]) * 0.47, 40]} />
        <meshBasicMaterial color={COLORS.accent} transparent opacity={0.35} side={THREE.DoubleSide} />
      </mesh>
    </group>
  );
}

/** Rdzeń energii = zadanie. Świetlista kula z pierścieniem priorytetu. */
function EnergyCore({ task, position }: { task: TaskDTO; position: [number, number, number] }) {
  const ref = useRef<THREE.Group>(null);
  const ring = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (ref.current) {
      ref.current.position.lerp(new THREE.Vector3(...position), 0.06);
      ref.current.position.y = 0.5 + Math.sin(clock.elapsedTime * 2 + position[0]) * 0.05;
    }
    if (ring.current) ring.current.rotation.y = clock.elapsedTime * 0.9;
  });
  const size = task.priority === "P0" ? 0.26 : task.priority === "P1" ? 0.22 : task.priority === "P2" ? 0.18 : 0.15;
  const color = PRIO_COLOR[task.priority] ?? COLORS.accent;
  const dim = task.status === "MERGED" ? 0.35 : 1;
  return (
    <group ref={ref} position={position} userData={{ taskId: task.id }}>
      <mesh>
        <icosahedronGeometry args={[size, 1]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={1.6 * dim} toneMapped={false} />
      </mesh>
      <mesh ref={ring} rotation={[Math.PI / 2.6, 0, 0]}>
        <torusGeometry args={[size * 1.9, 0.012, 6, 28]} />
        <meshBasicMaterial color={color} transparent opacity={0.75 * dim} />
      </mesh>
      <pointLight color={color} intensity={0.5 * dim} distance={2.2} />
    </group>
  );
}

/** Reaktor Wyroczni — pojedyncza brama, plazma świeci gdy trwa generowanie. */
function OracleReactor({ snapshot }: { snapshot: EngineSnapshot }) {
  const plasma = useRef<THREE.Mesh>(null);
  const beam = useRef<THREE.Mesh>(null);
  const busy = !!snapshot.llm.running;
  useFrame(({ clock }) => {
    if (plasma.current) {
      const s = 1 + Math.sin(clock.elapsedTime * (busy ? 9 : 2)) * (busy ? 0.06 : 0.02);
      plasma.current.scale.setScalar(s);
      const mat = plasma.current.material as THREE.MeshStandardMaterial;
      mat.emissiveIntensity = busy ? 2.4 : 0.5;
    }
    if (beam.current) {
      const m = beam.current.material as THREE.MeshBasicMaterial;
      m.opacity = busy ? 0.55 : 0.08;
    }
  });
  const z = ZONES.reactor.pos;
  return (
    <group position={[z[0], 0, z[2]]}>
      <mesh position={[0, 0.25, 0]}>
        <cylinderGeometry args={[1.35, 1.55, 0.5, 8]} />
        <meshStandardMaterial color={COLORS.metal} metalness={0.95} roughness={0.28} />
      </mesh>
      <mesh ref={plasma} position={[0, 1.15, 0]}>
        <cylinderGeometry args={[0.62, 0.62, 1.7, 16, 1, true]} />
        <meshStandardMaterial color={COLORS.plasma} emissive={COLORS.plasma} emissiveIntensity={0.6}
          transparent opacity={0.85} metalness={0.2} roughness={0.2} side={THREE.DoubleSide} toneMapped={false} />
      </mesh>
      <mesh ref={beam} position={[0, 2.1, 0]}>
        <coneGeometry args={[0.5, 1.1, 16, 1, true]} />
        <meshBasicMaterial color={COLORS.plasma} transparent opacity={0.1} side={THREE.DoubleSide} />
      </mesh>
      <mesh position={[0, 0.45, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[1.5, 1.62, 48]} />
        <meshBasicMaterial color={busy ? COLORS.plasma : COLORS.accent} transparent opacity={0.6} side={THREE.DoubleSide} />
      </mesh>
      {/* kolejka: kuleczki czekające na bramę */}
      {snapshot.llm.queue.slice(0, 5).map((j, i) => (
        <mesh key={j.id} position={[-2.2 + i * 0.42, 0.45, 1.7]}>
          <sphereGeometry args={[0.08, 10, 10]} />
          <meshStandardMaterial color={COLORS.plasma} emissive={COLORS.plasma} emissiveIntensity={1.1} toneMapped={false} />
        </mesh>
      ))}
    </group>
  );
}

/** Wieża Sponsora — złota winda, pulsuje gdy jest decyzja do podjęcia. */
function SponsorTower({ pending }: { pending: number }) {
  const halo = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (halo.current) {
      const m = halo.current.material as THREE.MeshBasicMaterial;
      m.opacity = pending > 0 ? 0.25 + Math.abs(Math.sin(clock.elapsedTime * 1.6)) * 0.45 : 0.08;
    }
  });
  const z = ZONES.tower.pos;
  return (
    <group position={[z[0], 0, z[2]]}>
      <mesh position={[0, 1.6, 0]}>
        <cylinderGeometry args={[0.35, 0.5, 3.2, 6]} />
        <meshStandardMaterial color={COLORS.metal} metalness={0.95} roughness={0.3} />
      </mesh>
      <mesh position={[0, 3.3, 0]}>
        <octahedronGeometry args={[0.55, 0]} />
        <meshStandardMaterial color={COLORS.warn} emissive={COLORS.warn} emissiveIntensity={pending > 0 ? 1.6 : 0.35} toneMapped={false} />
      </mesh>
      <mesh ref={halo} position={[0, 3.3, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[0.8, 1.0, 32]} />
        <meshBasicMaterial color={COLORS.warn} transparent opacity={0.1} side={THREE.DoubleSide} />
      </mesh>
      {pending > 0 && <pointLight position={[0, 3.3, 0]} color={COLORS.warn} intensity={1.2} distance={7} />}
    </group>
  );
}

/** Kriostaza COOLING — błękitna mgła i odliczanie. */
function CryoChamber({ count }: { count: number }) {
  const z = ZONES.cooling.pos;
  return (
    <group position={[z[0], 0, z[2]]}>
      <mesh position={[0, 0.55, 0]}>
        <boxGeometry args={[4.2, 1.1, 2.2]} />
        <meshStandardMaterial color={COLORS.cold} transparent opacity={0.16} metalness={0.4} roughness={0.2} />
      </mesh>
      <mesh position={[0, 1.2, 0]}>
        <sphereGeometry args={[0.35, 12, 12]} />
        <meshStandardMaterial color={COLORS.cold} emissive={COLORS.cold} emissiveIntensity={count > 0 ? 1.1 : 0.2} transparent opacity={0.5} toneMapped={false} />
      </mesh>
    </group>
  );
}

/** Brama Integracji — rozbłysk przy merge. */
function IntegrationGate({ flash }: { flash: number }) {
  const g = useRef<THREE.Mesh>(null);
  useFrame(() => {
    if (!g.current) return;
    const m = g.current.material as THREE.MeshBasicMaterial;
    m.opacity = flash > 0 ? Math.min(0.9, flash) : 0.12;
  });
  const z = ZONES.merge.pos;
  return (
    <group position={[z[0], 0, z[2]]}>
      <mesh position={[0, 1.4, 0]}>
        <torusGeometry args={[1.0, 0.09, 8, 32]} />
        <meshStandardMaterial color={COLORS.ok} emissive={COLORS.ok} emissiveIntensity={0.8} metalness={0.9} roughness={0.2} />
      </mesh>
      <mesh ref={g} position={[0, 1.4, 0]}>
        <circleGeometry args={[0.98, 32]} />
        <meshBasicMaterial color={COLORS.ok} transparent opacity={0.12} side={THREE.DoubleSide} />
      </mesh>
    </group>
  );
}

export function ChronosWorld({ snapshot, flash }: { snapshot: EngineSnapshot; flash: number }) {
  const groups = useMemo(() => {
    const byZone: Record<string, TaskDTO[]> = {};
    for (const t of snapshot.tasks) {
      const z = statusZone(t.status);
      (byZone[z] ||= []).push(t);
    }
    return byZone;
  }, [snapshot.tasks]);

  const pendingOwner = snapshot.tasks.filter((t) => t.status === "AWAITING_OWNER").length;
  const cooling = snapshot.tasks.filter((t) => t.status === "COOLING" || t.status === "BLOCKED").length;

  const railPairs: Array<[[number, number, number], [number, number, number]]> = [
    [ZONES.intake.pos, ZONES.workshop.pos],
    [ZONES.workshop.pos, ZONES.reactor.pos],
    [ZONES.reactor.pos, ZONES.review.pos],
    [ZONES.review.pos, ZONES.merge.pos],
    [ZONES.merge.pos, ZONES.vault.pos],
    [ZONES.workshop.pos, ZONES.cooling.pos],
  ];

  return (
    <group>
      <color attach="background" args={[COLORS.bg]} />
      <fog attach="fog" args={[COLORS.bg, 26, 62]} />
      <ambientLight intensity={0.32} />
      <directionalLight position={[8, 14, 6]} intensity={0.85} color="#BFD7FF" />
      <pointLight position={[0, 8, 0]} intensity={0.55} color={COLORS.accent} />

      {/* podłoga-siatka */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[3, -0.02, 0]}>
        <planeGeometry args={[52, 40]} />
        <meshStandardMaterial color="#0A0F18" metalness={0.6} roughness={0.75} />
      </mesh>
      <gridHelper args={[52, 34, "#16202E", "#101722"]} position={[3, 0.005, 0]} />

      {Object.entries(ZONES).map(([id, z]) => (
        <Platform key={id} pos={z.pos} size={z.size} label={z.label} />
      ))}

      {railPairs.map(([a, b], i) => <Rail key={i} from={a} to={b} />)}

      <OracleReactor snapshot={snapshot} />
      <SponsorTower pending={pendingOwner} />
      <CryoChamber count={cooling} />
      <IntegrationGate flash={flash} />
      {/* skarbiec MERGED */}
      <group position={ZONES.vault.pos}>
        <mesh position={[0, 0.4, 0]}>
          <boxGeometry args={[2.2, 0.8, 3]} />
          <meshStandardMaterial color={COLORS.metal} metalness={0.9} roughness={0.4} emissive={COLORS.ok} emissiveIntensity={0.05} />
        </mesh>
      </group>

      {Object.entries(groups).map(([zone, list]) =>
        list.map((t, i) => {
          const p = slotPos(zone as keyof typeof ZONES, i, Math.max(4, list.length));
          return <EnergyCore key={t.id} task={t} position={p} />;
        }),
      )}
    </group>
  );
}

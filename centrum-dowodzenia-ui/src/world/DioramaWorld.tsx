/**
 * DioramaWorld — Propozycja B: „Żywe Miasteczko Rzemieślnicze / Alchemiczny Port”.
 *
 * Ciepła, stylizowana diorama low-poly (Pixar / Wes Anderson):
 *  - agenci to fizyczne postacie w warsztatach (git worktree),
 *  - zadania to paczki/zwoje podróżujące między budynkami,
 *  - COOLING = sauna / komora odpoczynku z parą,
 *  - AWAITING_OWNER = gabinet na szczycie wieży-latarni ze złotym światłem.
 */
import React, { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import type { EngineSnapshot, TaskDTO } from "./mockEngine";
import { ZONES, statusZone, slotPos } from "./worldLayout";

const C = {
  sky: "#F3E3CC",
  ground: "#C9B79A",
  wood: "#8B5E3C",
  woodDark: "#5C3A22",
  roof: "#B5503C",
  ok: "#7BAE6A",
  amber: "#E8B04B",
  blue: "#7FB2D9",
  cream: "#F7EFE2",
  ink: "#3E2C22",
};

const PRIO_PARCEL: Record<string, string> = { P0: "#D96B4A", P1: "#E8B04B", P2: "#7FB2D9", P3: "#A9B79F" };

function House({ pos, w = 2.2, h = 1.6, d = 2.0, roof = C.roof }: {
  pos: [number, number, number]; w?: number; h?: number; d?: number; roof?: string;
}) {
  return (
    <group position={pos}>
      <mesh position={[0, h / 2, 0]} castShadow>
        <boxGeometry args={[w, h, d]} />
        <meshStandardMaterial color={C.cream} roughness={0.9} flatShading />
      </mesh>
      {/* beama konstrukcyjna */}
      {[-1, 1].map((s) => (
        <mesh key={s} position={[(s * w) / 2, h / 2, 0]}>
          <boxGeometry args={[0.08, h, d * 1.01]} />
          <meshStandardMaterial color={C.wood} roughness={0.85} flatShading />
        </mesh>
      ))}
      {/* dach */}
      <mesh position={[0, h + 0.28, 0]} rotation={[0, 0, Math.PI / 4]} castShadow>
        <boxGeometry args={[w * 0.82, w * 0.82, d * 1.05]} />
        <meshStandardMaterial color={roof} roughness={0.8} flatShading />
      </mesh>
    </group>
  );
}

function Agent({ pos, color, busy }: { pos: [number, number, number]; color: string; busy: boolean }) {
  const ref = useRef<THREE.Group>(null);
  useFrame(({ clock }) => {
    if (ref.current) ref.current.position.y = 0.02 + (busy ? Math.abs(Math.sin(clock.elapsedTime * 3)) * 0.08 : 0);
  });
  return (
    <group ref={ref} position={pos}>
      <mesh position={[0, 0.32, 0]}>
        <capsuleGeometry args={[0.13, 0.22, 4, 8]} />
        <meshStandardMaterial color={color} roughness={0.75} flatShading />
      </mesh>
      <mesh position={[0, 0.58, 0]}>
        <sphereGeometry args={[0.12, 10, 8]} />
        <meshStandardMaterial color={C.cream} roughness={0.7} flatShading />
      </mesh>
    </group>
  );
}

/** Paczka = zadanie: drewniana skrzynka/zawój z pieczęcią priorytetu. */
function Parcel({ task, position }: { task: TaskDTO; position: [number, number, number] }) {
  const ref = useRef<THREE.Group>(null);
  useFrame(({ clock }) => {
    if (ref.current) {
      ref.current.position.lerp(new THREE.Vector3(...position), 0.06);
      ref.current.rotation.y += 0.006;
    }
  });
  const s = task.priority === "P0" ? 0.42 : task.priority === "P1" ? 0.36 : task.priority === "P2" ? 0.3 : 0.26;
  const tint = PRIO_PARCEL[task.priority] ?? C.amber;
  return (
    <group ref={ref} position={position} userData={{ taskId: task.id }}>
      <mesh castShadow>
        <boxGeometry args={[s, s * 0.8, s]} />
        <meshStandardMaterial color={C.wood} roughness={0.9} flatShading />
      </mesh>
      <mesh position={[0, s * 0.4, 0]}>
        <boxGeometry args={[s * 0.72, 0.06, s * 0.72]} />
        <meshStandardMaterial color={tint} emissive={tint} emissiveIntensity={0.35} roughness={0.6} />
      </mesh>
    </group>
  );
}

/** Wieża-latarnia Sponsora. */
function Lighthouse({ pending }: { pending: number }) {
  const glow = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (!glow.current) return;
    const m = glow.current.material as THREE.MeshStandardMaterial;
    m.emissiveIntensity = pending > 0 ? 1.4 + Math.abs(Math.sin(clock.elapsedTime * 2)) * 1.4 : 0.25;
  });
  const z = ZONES.tower.pos;
  return (
    <group position={[z[0], 0, z[2]]}>
      <mesh position={[0, 1.5, 0]}>
        <cylinderGeometry args={[0.55, 0.8, 3, 8]} />
        <meshStandardMaterial color={C.cream} roughness={0.9} flatShading />
      </mesh>
      <mesh ref={glow} position={[0, 3.2, 0]}>
        <sphereGeometry args={[0.42, 12, 10]} />
        <meshStandardMaterial color={C.amber} emissive={C.amber} emissiveIntensity={0.3} toneMapped={false} flatShading />
      </mesh>
      {pending > 0 && <pointLight position={[0, 3.2, 0]} color={C.amber} intensity={2} distance={12} />}
    </group>
  );
}

/** Sauna COOLING z unoszącą się parą. */
function Sauna({ count }: { count: number }) {
  const steam = useRef<THREE.Group>(null);
  useFrame(({ clock }) => {
    if (!steam.current) return;
    steam.current.children.forEach((c, i) => {
      c.position.y = 1.1 + ((clock.elapsedTime * 0.5 + i * 0.4) % 1.4);
      const mesh = c as THREE.Mesh;
      (mesh.material as THREE.MeshBasicMaterial).opacity = Math.max(0, 0.35 - ((clock.elapsedTime * 0.5 + i * 0.4) % 1.4) * 0.25);
    });
  });
  const z = ZONES.cooling.pos;
  return (
    <group position={[z[0], 0, z[2]]}>
      <House pos={[0, 0, 0]} w={3.4} h={1.1} d={1.8} roof={C.blue} />
      <group ref={steam} position={[0, 1.1, 0]}>
        {Array.from({ length: count > 0 ? Math.min(4, count + 1) : 1 }).map((_, i) => (
          <mesh key={i} position={[i * 0.5 - 0.7, 1.2, 0]}>
            <sphereGeometry args={[0.13, 8, 6]} />
            <meshBasicMaterial color="#FFFFFF" transparent opacity={0.25} />
          </mesh>
        ))}
      </group>
    </group>
  );
}

export function DioramaWorld({ snapshot, flash }: { snapshot: EngineSnapshot; flash: number }) {
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
  const running = groups["workshop"] ?? [];
  const light = useRef<THREE.DirectionalLight>(null);
  useFrame(({ clock }) => {
    if (light.current) light.current.intensity = 1.15 + Math.sin(clock.elapsedTime * 0.4) * 0.08;
  });

  return (
    <group>
      <color attach="background" args={[C.sky]} />
      <fog attach="fog" args={[C.sky, 30, 68]} />
      <ambientLight intensity={0.75} color="#FFF4E2" />
      <directionalLight ref={light} position={[9, 15, 7]} intensity={1.2} color="#FFE9C7" castShadow />
      <hemisphereLight args={[C.cream, C.ground, 0.55]} />

      {/* trawnik-diorama */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[3, -0.02, 0]} receiveShadow>
        <planeGeometry args={[56, 44]} />
        <meshStandardMaterial color={C.ground} roughness={1} flatShading />
      </mesh>

      {/* bruk w centrum */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[1, 0.01, 0]}>
        <circleGeometry args={[6.5, 48]} />
        <meshStandardMaterial color="#D8C6A8" roughness={1} />
      </mesh>

      {/* Strefa wejściowa — magazyn z rampą */}
      <group position={ZONES.intake.pos}>
        <House pos={[0, 0, -2.4]} w={3.6} h={1.5} d={1.8} />
        <mesh position={[0, 0.12, 0.9]} rotation={[-0.12, 0, 0]}>
          <boxGeometry args={[3.2, 0.12, 2.2]} />
          <meshStandardMaterial color={C.wood} roughness={0.9} flatShading />
        </mesh>
      </group>

      {/* Warsztaty — trzy budynki z agentami */}
      {[0, 1, 2].map((i) => (
        <group key={i} position={[ZONES.workshop.pos[0] + (i - 1) * 2.6, 0, ZONES.workshop.pos[2] + (i % 2) * 2.2]}>
          <House pos={[0, 0, -0.9]} w={1.9} h={1.4} d={1.6} roof={i % 2 ? C.roof : "#8E7C5B"} />
          <Agent
            pos={[0.1, 0, 0.6]}
            color={["#7FB2D9", "#C97B63", "#7BAE6A"][i]}
            busy={running.length > i}
          />
        </group>
      ))}

      {/* Reaktor Wyroczni = alchemiczny kocioł / studnia mądrości */}
      <group position={[ZONES.reactor.pos[0], 0, ZONES.reactor.pos[2]]}>
        <mesh position={[0, 0.45, 0]} castShadow>
          <cylinderGeometry args={[1.0, 0.8, 0.9, 10]} />
          <meshStandardMaterial color={C.woodDark} roughness={0.85} flatShading />
        </mesh>
        <mesh position={[0, 1.0, 0]}>
          <sphereGeometry args={[0.55, 14, 12]} />
          <meshStandardMaterial color="#8E6BC9" emissive="#8E6BC9" emissiveIntensity={snapshot.llm.running ? 1.4 : 0.35} transparent opacity={0.85} toneMapped={false} flatShading />
        </mesh>
        {snapshot.llm.running && <pointLight position={[0, 1.2, 0]} color="#B794F6" intensity={1.6} distance={7} />}
      </group>

      {/* Code Review — altana z sędzią */}
      <group position={[ZONES.review.pos[0], 0, ZONES.review.pos[2]]}>
        <House pos={[0, 0, -0.8]} w={2.4} h={1.2} d={1.6} roof="#6E8B74" />
        <Agent pos={[0, 0, 0.9]} color="#5E5E6E" busy />
      </group>

      {/* Brama integracji — łuk z flagami */}
      <group position={[ZONES.merge.pos[0], 0, ZONES.merge.pos[2]]}>
        {[-1, 1].map((s) => (
          <mesh key={s} position={[s * 0.9, 1.0, 0]}>
            <cylinderGeometry args={[0.09, 0.1, 2.0, 6]} />
            <meshStandardMaterial color={C.woodDark} roughness={0.9} flatShading />
          </mesh>
        ))}
        <mesh position={[0, 2.05, 0]}>
          <boxGeometry args={[2.0, 0.22, 0.22]} />
          <meshStandardMaterial color={flash > 0 ? C.ok : C.wood} emissive={flash > 0 ? C.ok : "#000"} emissiveIntensity={flash * 1.5} roughness={0.8} flatShading />
        </mesh>
      </group>

      {/* Skarbiec MERGED — piętrzące się skrzynie */}
      <group position={[ZONES.vault.pos[0], 0, ZONES.vault.pos[2]]}>
        {[[0, 0.22, 0], [0.55, 0.22, 0.1], [0.25, 0.66, 0.05]].map((p, i) => (
          <mesh key={i} position={p as [number, number, number]} castShadow>
            <boxGeometry args={[0.5, 0.42, 0.5]} />
            <meshStandardMaterial color={i === 2 ? C.amber : C.wood} roughness={0.9} flatShading />
          </mesh>
        ))}
      </group>

      <Lighthouse pending={pendingOwner} />
      <Sauna count={cooling} />

      {Object.entries(groups).map(([zone, list]) =>
        list.map((t, i) => {
          const p = slotPos(zone as keyof typeof ZONES, i, Math.max(4, list.length));
          return <Parcel key={t.id} task={t} position={[p[0], 0.55, p[2]]} />;
        }),
      )}
    </group>
  );
}

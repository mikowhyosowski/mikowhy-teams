/**
 * worldLayout — wspólna mapa logiczna obu światów.
 * Każdy status zadania ma przypisaną strefę w przestrzeni; oba światy (A:chronos, B:diorama)
 * korzystają z tego samego kontraktu pozycji, więc przełącznik zmienia tylko oprawę wizualną.
 */
import type { Status } from "./mockEngine";

export type WorldVariant = "chronos" | "diorama";

export interface Zone {
  id: string;
  label: string;
  pos: [number, number, number];
  size: [number, number, number];
}

/** Strefy świata — wspólne dla A i B. */
export const ZONES = {
  intake: { id: "intake", label: "Strefa Wejścia (READY / P0–P3)", pos: [-9, 0, 0], size: [4, 0.4, 6] },
  workshop: { id: "workshop", label: "Warsztaty (git worktree / RUNNING)", pos: [-3, 0, 0], size: [6, 0.4, 8] },
  reactor: { id: "reactor", label: "Reaktor Wyroczni (llm_queue + flock)", pos: [2.5, 0, 0], size: [3, 0.4, 3] },
  review: { id: "review", label: "Strefa Code Review (IN_REVIEW)", pos: [7, 0, -2], size: [3.5, 0.4, 4] },
  merge: { id: "merge", label: "Brama Integracji (_integration → master)", pos: [12, 0, 0], size: [3, 0.4, 5] },
  tower: { id: "tower", label: "Wieża Sponsora (AWAITING_OWNER)", pos: [2.5, 0, 7], size: [3, 0.4, 3] },
  cooling: { id: "cooling", label: "Komora Chłodzenia (COOLING)", pos: [-3, 0, -8], size: [5, 0.4, 3] },
  vault: { id: "vault", label: "Skarbiec (MERGED)", pos: [15, 0, -6], size: [3, 0.4, 4] },
} as const satisfies Record<string, Zone>;

/** Slot w obrębie strefy dla n-tego obiektu. */
export function slotPos(zoneId: keyof typeof ZONES, i: number, total = 6): [number, number, number] {
  const z = ZONES[zoneId];
  const cols = Math.ceil(Math.sqrt(total));
  const c = i % cols;
  const r = Math.floor(i / cols);
  const stepX = z.size[0] / (cols + 1);
  const stepZ = z.size[2] / (cols + 1);
  return [z.pos[0] - z.size[0] / 2 + stepX * (c + 1), 0.3, z.pos[2] - z.size[2] / 2 + stepZ * (r + 1)];
}

/** Docelowa pozycja obiektu wg statusu. */
export function statusZone(status: Status): keyof typeof ZONES {
  switch (status) {
    case "READY": return "intake";
    case "RUNNING":
    case "CHANGES_REQUESTED": return "workshop";
    case "IN_REVIEW": return "review";
    case "READY_TO_MERGE": return "merge";
    case "MERGED": return "vault";
    case "AWAITING_OWNER": return "tower";
    case "COOLING":
    case "BLOCKED": return "cooling";
    default: return "intake";
  }
}

export const STATUS_RANK: Record<Status, number> = {
  READY: 0, RUNNING: 1, CHANGES_REQUESTED: 1, IN_REVIEW: 2, READY_TO_MERGE: 3, MERGED: 4,
  COOLING: -1, BLOCKED: -1, AWAITING_OWNER: -2,
};

/**
 * Design System „Centrum Dowodzenia” — ciemny, wysokokontrastowy tryb dowodzenia.
 * Jeden plik = jedno źródło prawdy: kolory, skala typografii, spacing, radius,
 * elevation (cienie), tokeny ruchu i semantyczne kolory statusów.
 */
import { Platform } from "react-native";

export const colors = {
  bg: "#0B0E14",          // głębia — tło aplikacji
  bgCard: "#121826",      // karta bazowa
  bgCardAlt: "#0F1420",   // karta przyciemniona
  bgInset: "#05070B",     // inset (logi, terminal)
  border: "#1E2A3D",
  borderStrong: "#2A3A55",
  text: "#E6EDF7",
  textDim: "#8B98AC",
  textFaint: "#5B6B82",
  accent: "#22D3EE",      // cyjan — akcent dowodzenia
  accentDim: "#0E2A33",
  danger: "#F87171",
  warn: "#F59E0B",
  ok: "#34D399",
  mono: "JetBrains Mono, Menlo, Consolas, monospace",
  sans: Platform.select({ web: "Inter, system-ui, -apple-system, sans-serif", default: "System" }) as string,
};

/** Skala typografii — tylko te rozmiary, żadnych „losowych” 13px/17px. */
export const type = {
  display: 22,   // H1 ekranu
  title: 15,     // H2 sekcji
  body: 13,
  meta: 11,      // mono meta
  big: 24,       // licznik (mono)
} as const;

export const spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };
export const radius = { sm: 6, md: 10, lg: 14, pill: 999 };

/** Elevation — cień jako sygnał hierarchii (web: box-shadow, natywne: shadow*). */
export const elevation = {
  card: Platform.select({
    web: { boxShadow: "0 1px 2px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.25)" },
    default: { shadowColor: "#000", shadowOpacity: 0.35, shadowRadius: 12, shadowOffset: { width: 0, height: 4 }, elevation: 4 },
  }) as object,
  pop: Platform.select({
    web: { boxShadow: "0 8px 32px rgba(0,0,0,.5)" },
    default: { shadowColor: "#000", shadowOpacity: 0.5, shadowRadius: 20, shadowOffset: { width: 0, height: 8 }, elevation: 8 },
  }) as object,
};

/** Tokeny ruchu — jedna krzywa easingu i czasów w całej apce. */
export const motion = {
  fast: 140,
  base: 220,
  slow: 380,
  easeOut: "cubic-bezier(0.22, 1, 0.36, 1)",
} as const;

/** Semantyczne kolory statusów procesów (sponsor-shell) i zadań. */
export const statusColor: Record<string, string> = {
  QUEUED: "#8B98AC",
  RUNNING: "#38BDF8",
  SUCCEEDED: "#34D399",
  FAILED: "#F87171",
  TIMEOUT: "#FB923C",
  KILLED: "#A78BFA",
  READY: "#8B98AC",
  IN_REVIEW: "#818CF8",
  CHANGES_REQUESTED: "#FBBF24",
  READY_TO_MERGE: "#34D399",
  MERGED: "#475569",
  AWAITING_OWNER: "#F59E0B",
  BLOCKED: "#F87171",
};

export function statusColorOf(status: string | null | undefined): string {
  return statusColor[status ?? ""] ?? colors.textDim;
}

/** Czy status oznacza „żywy” proces (pulsowanie, akcje kill). */
export function isLive(status: string | null | undefined): boolean {
  return status === "RUNNING" || status === "QUEUED";
}

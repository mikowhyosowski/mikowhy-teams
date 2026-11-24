/**
 * Design System „Centrum Dowodzenia” — ciemny, wysokokontrastowy tryb dowodzenia.
 * Jeden plik = jedno źródło prawdy dla kolorów, typografii i statusów.
 */
import { Platform } from "react-native";

export const colors = {
  bg: "#0B0E14",          // głębia — tło aplikacji
  bgCard: "#121826",      // karta
  bgCardAlt: "#0F1420",   // karta podniesiona / hover
  border: "#1E2A3D",
  text: "#E6EDF7",
  textDim: "#8B98AC",
  accent: "#22D3EE",      // cyjan — akcent dowodzenia
  mono: "JetBrains Mono, Menlo, Consolas, monospace",
  sans: Platform.select({ web: "Inter, system-ui, sans-serif", default: "System" }) as string,
};

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

export const spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 };
export const radius = { sm: 6, md: 10, lg: 14 };

export function statusColorOf(status: string | null | undefined): string {
  return statusColor[status ?? ""] ?? colors.textDim;
}

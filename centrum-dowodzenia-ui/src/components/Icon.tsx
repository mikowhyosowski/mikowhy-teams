import React from "react";
import Svg, { Circle, Path, Rect } from "react-native-svg";

/**
 * Zestaw ikon (stroke 1.7, currentColor) — spójny język wizualny zamiast emoji.
 * react-native-svg renderuje identycznie na web i natywnie (iOS/Android).
 */
type P = { size?: number; color?: string };

function I({ size = 16, color = "#E6EDF7", children }: P & { children: React.ReactNode }) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke={color} strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      {children}
    </Svg>
  );
}

export const Icon = {
  radar: (p: P) => (
    <I {...p}><Circle cx={12} cy={12} r={9} /><Circle cx={12} cy={12} r={4.5} /><Path d="M12 12 18.5 5.5" /><Circle cx={12} cy={12} r={1} fill={p.color ?? "#E6EDF7"} stroke="none" /></I>
  ),
  bot: (p: P) => (
    <I {...p}><Rect x={5} y={8} width={14} height={11} rx={2.5} /><Path d="M12 8V4.5" /><Circle cx={12} cy={3.5} r={1} /><Circle cx={9.5} cy={13} r={1} fill={p.color ?? "#E6EDF7"} stroke="none" /><Circle cx={14.5} cy={13} r={1} fill={p.color ?? "#E6EDF7"} stroke="none" /><Path d="M9.5 16.5h5" /></I>
  ),
  inbox: (p: P) => (
    <I {...p}><Path d="M4 13h4l2 3h4l2-3h4" /><Path d="M5 6h14l2 7v5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18v-5z" /></I>
  ),
  kill: (p: P) => (
    <I {...p}><Circle cx={12} cy={12} r={8.5} /><Path d="M9 9l6 6M15 9l-6 6" /></I>
  ),
  check: (p: P) => (
    <I {...p}><Path d="M5 12.5l4.5 4.5L19 7.5" /></I>
  ),
  x: (p: P) => (
    <I {...p}><Path d="M6 6l12 12M18 6L6 18" /></I>
  ),
  alert: (p: P) => (
    <I {...p}><Path d="M12 4 21 20H3z" /><Path d="M12 10v4.5" /><Circle cx={12} cy={17} r={0.6} fill={p.color ?? "#E6EDF7"} stroke="none" /></I>
  ),
  bolt: (p: P) => (
    <I {...p}><Path d="M13 3 5 13.5h5.5L10 21l8-10.5h-5.5z" /></I>
  ),
  terminal: (p: P) => (
    <I {...p}><Rect x={3} y={4.5} width={18} height={15} rx={2.5} /><Path d="m7 9.5 3 3-3 3" /><Path d="M12.5 15.5H17" /></I>
  ),
  clock: (p: P) => (
    <I {...p}><Circle cx={12} cy={12} r={8.5} /><Path d="M12 7.5V12l3 2" /></I>
  ),
  refresh: (p: P) => (
    <I {...p}><Path d="M20 12a8 8 0 1 1-2.34-5.66" /><Path d="M20 4v4h-4" /></I>
  ),
  chevron: (p: P) => (
    <I {...p}><Path d="m9 6 6 6-6 6" /></I>
  ),
  wifi: (p: P) => (
    <I {...p}><Path d="M4 10a12 12 0 0 1 16 0" /><Path d="M7 13.5a7.5 7.5 0 0 1 10 0" /><Path d="M10 17a3 3 0 0 1 4 0" /><Circle cx={12} cy={19.5} r={0.8} fill={p.color ?? "#E6EDF7"} stroke="none" /></I>
  ),
  wifiOff: (p: P) => (
    <I {...p}><Path d="M4 10a12 12 0 0 1 5-3.5M15 6.8A12 12 0 0 1 20 10" /><Path d="m7 13.5 2-2M15 13.5l-2-2M10 17a3 3 0 0 1 1.5-1.3" /><Path d="M3 3l18 18" /></I>
  ),
};

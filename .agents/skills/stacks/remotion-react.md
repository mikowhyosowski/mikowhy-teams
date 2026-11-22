# STACK: remotion-react

## Konwencje
- Remotion + React + TypeScript strict; kompozycje w `src/`, `npx remotion render` do MP4.
- `package.json` i lockfile — tylko Faza 2.5 (zmiana #4).
- Animacje: `spring()` / interpolacje z `useCurrentFrame()`; brak twardych `setTimeout` w renderze.
- Teksty: fonty z `staticFile()`, kontrast WCAG AA; motion zobowiązuje do testów wizualnych (Playwright) w QG-3.

// Expo/Metro podstawia process.env w runtime (EXPO_PUBLIC_*); tu tylko deklaracja dla TS.
declare const process: { env: Record<string, string | undefined> };

const { getDefaultConfig } = require("expo/metro-config");
const path = require("path");
const fs = require("fs");

/** @type {import('expo/metro-config').MetroConfig} */
const config = getDefaultConfig(__dirname);

// `zustand/esm/middleware.mjs` używa `import.meta`, którego Metro nie potrafi obsłużyć
// w trybie non-module. Mapujemy wszystkie wejścia zustanda na wariant CJS z node_modules.
const ZUSTAND_ROOT = path.join(__dirname, "node_modules", "zustand");

function zustandCjs(sub) {
  const base = sub ? path.join(ZUSTAND_ROOT, sub) : ZUSTAND_ROOT;
  for (const cand of [`${base}.js`, path.join(base, "index.js")]) {
    if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return cand;
  }
  return null;
}

config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (moduleName === "zustand" || moduleName.startsWith("zustand/")) {
    const sub = moduleName === "zustand" ? "" : moduleName.slice("zustand/".length);
    const target = zustandCjs(sub);
    if (target) return { filePath: target, type: "sourceFile" };
  }
  return context.resolveRequest(context, moduleName, platform);
};

module.exports = config;

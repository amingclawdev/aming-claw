import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_URL || "http://localhost:40000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: false,
      proxy: {
        // SSE-friendly: timeout 0 + no buffering on the dev proxy so the
        // /events/stream endpoint stays open and flushes events live.
        "/api": {
          target: backend,
          changeOrigin: true,
          ws: true,
          // 0 = no socket timeout — required for long-lived SSE connections,
          // otherwise Vite drops the upstream after the default 120s idle.
          timeout: 0,
          proxyTimeout: 0,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});

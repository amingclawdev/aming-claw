import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dashboardDir = path.dirname(fileURLToPath(import.meta.url));
const defaultWorkspaceRoot = path.resolve(dashboardDir, "../..").replace(/\\/g, "/");

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.VITE_BACKEND_URL || "http://localhost:40000";
  const workspaceRoot = (env.VITE_WORKSPACE_ROOT || defaultWorkspaceRoot).replace(/\\/g, "/");
  const defaultWorkspaceRootPlugin = {
    name: "dashboard-default-workspace-root",
    enforce: "pre" as const,
    transform(code: string, id: string) {
      if (!id.replace(/\\/g, "/").endsWith("/src/lib/editor.ts")) return null;
      const nextCode = code.replace(
        'typeof __DEFAULT_WORKSPACE_ROOT__ === "string" ? __DEFAULT_WORKSPACE_ROOT__ : ""',
        JSON.stringify(workspaceRoot),
      );
      return nextCode === code ? null : { code: nextCode, map: null };
    },
  };
  return {
    plugins: [defaultWorkspaceRootPlugin, react()],
    define: {
      __DEFAULT_WORKSPACE_ROOT__: JSON.stringify(workspaceRoot),
    },
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

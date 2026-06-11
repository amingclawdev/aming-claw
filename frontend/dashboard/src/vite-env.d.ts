/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BACKEND_URL?: string;
  readonly VITE_PROJECT_ID?: string;
  readonly VITE_DIRECT_API?: string;
  readonly VITE_WORKSPACE_ROOT?: string;
  readonly VITE_EDITOR_SCHEME?: string;
}

declare const __DEFAULT_WORKSPACE_ROOT__: string;
/** Build-time git short HEAD. "dev" in Vite serve mode (banner disabled). */
declare const __DASHBOARD_BUILD__: string;

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

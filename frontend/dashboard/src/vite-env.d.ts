/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BACKEND_URL?: string;
  readonly VITE_PROJECT_ID?: string;
  readonly VITE_DIRECT_API?: string;
  readonly VITE_WORKSPACE_ROOT?: string;
  readonly VITE_EDITOR_SCHEME?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

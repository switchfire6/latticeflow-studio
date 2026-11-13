/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend origin, e.g. "http://127.0.0.1:8000". Unset = same origin via the Vite proxy. */
  readonly VITE_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

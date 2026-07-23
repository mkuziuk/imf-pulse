/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly BASE_URL: string;
  readonly VITE_ROUTER_MODE?: "browser" | "hash";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "virtual:imf-pulse-content" {
  export const currentModules: Record<string, string>;
  export const pulseModules: Record<string, string>;
  export const releaseModules: Record<string, string>;
  export const curatedModules: Record<string, string>;
}

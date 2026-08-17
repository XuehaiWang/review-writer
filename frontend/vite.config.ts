import { defineConfig, type Plugin } from "vitest/config";
import react from "@vitejs/plugin-react";

import { resolveProxyRequestOrigin } from "./src/dev/proxyOrigin";

const developmentApiTarget = process.env.VITE_DEV_API_TARGET || "http://127.0.0.1:8770";
const developmentPublicOrigin = process.env.VITE_DEV_PUBLIC_ORIGIN || "";

function developmentProxy() {
  return {
    target: developmentApiTarget,
    changeOrigin: true,
  };
}

function canonicalDevelopmentOrigin(): Plugin {
  return {
    name: "review-writer-canonical-development-origin",
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        if (request.url?.startsWith("/api/")) {
          const forwardedOrigin = resolveProxyRequestOrigin({
            browserOrigin: request.headers.origin,
            requestHost: request.headers.host,
            apiTarget: developmentApiTarget,
            configuredPublicOrigin: developmentPublicOrigin,
          });
          if (forwardedOrigin) request.headers.origin = forwardedOrigin;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [canonicalDevelopmentOrigin(), react()],
  base: "/assets/react/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": developmentProxy(),
      "/assets/ketcher": developmentProxy(),
      "/assets/dashboard": developmentProxy(),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});

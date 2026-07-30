import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/static/dist/",
  build: {
    outDir: "../static/dist",
    emptyOutDir: true,
    manifest: true,
    // Never inline fonts. Vite inlines assets under ~4 kB as data: URIs, and a
    // small KaTeX face was being emitted that way, which the application's own
    // `font-src 'self'` Content Security Policy then blocked at runtime. Keeping
    // fonts as files preserves the strict policy instead of widening it.
    assetsInlineLimit: (filePath: string) =>
      /\.(woff2?|ttf|otf|eot)$/i.test(filePath) ? false : undefined,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:9010",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});

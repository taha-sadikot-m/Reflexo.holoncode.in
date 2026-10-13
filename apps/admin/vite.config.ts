import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// One web app at the root: the public site (/), the owner app (/app) and the ops console (/admin).
export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/admin/api": { target: "http://localhost:8080", changeOrigin: false },
      "/api": { target: "http://localhost:8080", changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});

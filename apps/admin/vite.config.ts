import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Served under /admin (Caddy in the demo deployment, or the API process locally from dist/).
export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/admin/api": { target: "http://localhost:8080", changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});

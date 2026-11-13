import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the API runs separately (compass-studio-serve --allow-origin
// http://127.0.0.1:5173). Build: served by the API server itself (--web dist).
// Pages: index.html (the application), bench.html (spike benches).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: true } },
  },
  build: {
    target: "es2022",
    sourcemap: true,
    rolldownOptions: { input: { main: "index.html", bench: "bench.html" } },
  },
});

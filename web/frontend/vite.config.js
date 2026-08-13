import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    cssMinify: false,
  },
  server: {
    proxy: {
      // ws: the run stream is a WebSocket under /api, and vite only proxies
      // the upgrade request when told to.
      "/api": { target: "http://localhost:8100", ws: true },
    },
  },
});

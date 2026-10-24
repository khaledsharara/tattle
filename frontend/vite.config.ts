import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // `npm run dev` against a Tattle running on this machine (the demo, for example).
    proxy: { "/api": "http://localhost:8090" },
  },
});

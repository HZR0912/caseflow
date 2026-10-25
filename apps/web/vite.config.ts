import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  base: "/approval-assets/",
  build: { assetsDir: "" },
  server: { proxy: { "/v1": "http://127.0.0.1:8000" } },
});


import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: {
    // 构建产物直接进 console（http 接口）的静态目录，单端口由 console 提供
    outDir: "../src/kernel/console/frontend/dist",
    emptyOutDir: true,
  },
});


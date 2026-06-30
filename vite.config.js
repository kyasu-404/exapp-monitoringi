import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'ex_app',
    emptyOutDir: false,
    rollupOptions: {
      input: 'ex_app/src/main.js',
      output: {
        entryFileNames: 'js/monitoringi-main.js',
        chunkFileNames: 'js/[name].js',
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith('.css')) {
            return 'css/monitoringi.css'
          }
          return 'assets/[name][extname]'
        },
      },
    },
  },
})


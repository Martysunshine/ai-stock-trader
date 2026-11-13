import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite runs inside the console server (server/index.ts, middleware mode): page, API and Desk share
// one address, http://127.0.0.1:4317, so there is no dev port, no proxy and no CORS.
export default defineConfig({
  plugins: [react()],
  // Pre-bundle dependencies that are only reached through lazy routes, so the first visit to a
  // page does not trigger an "outdated optimize dep" reload.
  optimizeDeps: {
    include: [
      'three',
      '@react-three/fiber',
      'three/examples/jsm/environments/RoomEnvironment.js',
      'three/examples/jsm/loaders/GLTFLoader.js',
      'three/examples/jsm/libs/meshopt_decoder.module.js',
      'lightweight-charts',
      'd3-sankey',
      'd3-scale',
      'd3-shape',
      'marked',
      '@tanstack/react-virtual',
      'decimal.js',
    ],
  },
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 1200,
  },
});

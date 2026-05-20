import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [react()],
    base: './', // Important for Electron
    server: {
        host: true, // aceptar conexiones desde la LAN (p. ej. móvil en la misma Wi‑Fi)
        // Vite 5.4+ comprueba el Host; sin esto, acceder por IP a veces devuelve 403 y la app no carga.
        allowedHosts: true,
        port: 5173,
        watch: {
            // Evitar ENOSPC en Linux: ignora árboles enormes que no son código fuente del frontend.
            ignored: [
                '**/venv/**',
                '**/.venv/**',
                '**/node_modules/**',
                '**/.git/**',
                '**/dist/**',
                '**/build/**',
                '**/__pycache__/**',
                '**/backend/__pycache__/**',
                '**/backend/projects/**',
                '**/backend/*.stl',
                '**/backend/*.ndjson',
                '**/backend/debug-*.log',
                '**/.cursor/**',
                '**/terminals/**',
            ],
        },
    },
})

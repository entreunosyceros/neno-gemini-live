const { app, BrowserWindow, ipcMain, session } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

/**
 * Resuelve el intérprete de Python a usar para arrancar el backend.
 * Prioriza el venv del proyecto (donde están instalados socketio, fastapi, etc.)
 * sobre el python del sistema, evitando ModuleNotFoundError silenciosos.
 */
function resolvePythonExecutable() {
    const projectRoot = path.join(__dirname, '..');
    const candidates = process.platform === 'win32'
        ? [
            path.join(projectRoot, 'venv', 'Scripts', 'python.exe'),
            path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
        ]
        : [
            path.join(projectRoot, 'venv', 'bin', 'python'),
            path.join(projectRoot, '.venv', 'bin', 'python'),
        ];
    for (const c of candidates) {
        try {
            if (fs.existsSync(c)) return c;
        } catch (_) { /* noop */ }
    }
    return process.platform === 'win32' ? 'python.exe' : 'python3';
}

// Windows-only: ANGLE D3D11 + Vulkan flags avoid GPU stalls there; on Linux they can break WebGL.
if (process.platform === 'win32') {
    app.commandLine.appendSwitch('use-angle', 'd3d11');
    app.commandLine.appendSwitch('enable-features', 'Vulkan');
    app.commandLine.appendSwitch('ignore-gpu-blocklist');
}

// Linux: chrome-sandbox suele fallar sin root (p. ej. /var/www, Docker, usuario normal).
// Alternativa segura en tu máquina: sudo chown root:root node_modules/electron/dist/chrome-sandbox && sudo chmod 4755 ...
if (process.platform === 'linux') {
    if (process.env.NENO_ELECTRON_SANDBOX !== '1') {
        app.commandLine.appendSwitch('no-sandbox');
    }
}

let mainWindow;
let pythonProcess;

const sessionsWithMediaPermissions = new WeakSet();

/**
 * Chromium/Electron piden permisos con distintos nombres según versión (`media`,
 * `audioCapture`, `videoCapture`, etc.). Si el check devuelve false, ni siquiera
 * se llega a `getUserMedia` y `enumerateDevices` puede quedar vacío.
 */
function attachRendererMediaPermissions(ses) {
    if (!ses || sessionsWithMediaPermissions.has(ses)) return;
    sessionsWithMediaPermissions.add(ses);

    ses.setPermissionRequestHandler((webContents, permission, callback, details) => {
        console.log('[NENO Electron] permission-request:', permission, details ? JSON.stringify(details) : '');
        callback(true);
    });

    ses.setPermissionCheckHandler((_webContents, permission, requestingOrigin, _details) => {
        console.log('[NENO Electron] permission-check:', permission, requestingOrigin || '');
        return true;
    });
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1920,
        height: 1080,
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false, // For simple IPC/Socket.IO usage
            sandbox: false,
        },
        backgroundColor: '#000000',
        frame: false, // Frameless for custom UI
        titleBarStyle: 'hidden',
        show: false, // Don't show until ready
    });

    // In dev, load Vite server. In prod, load index.html
    const isDev = process.env.NODE_ENV !== 'production';

    const loadFrontend = (retries = 3) => {
        const url = isDev ? 'http://localhost:5173' : null;
        const loadPromise = isDev
            ? mainWindow.loadURL(url)
            : mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));

        loadPromise
            .then(() => {
                console.log('Frontend loaded successfully!');
                windowWasShown = true;
                mainWindow.show();
                // Anteriormente se abrían las DevTools automáticamente en dev.
                // Se ha movido a un toggle desde la ventana de Settings (window-toggle-devtools).
            })
            .catch((err) => {
                console.error(`Failed to load frontend: ${err.message}`);
                if (retries > 0) {
                    console.log(`Retrying in 1 second... (${retries} retries left)`);
                    setTimeout(() => loadFrontend(retries - 1), 1000);
                } else {
                    console.error('Failed to load frontend after all retries. Keeping window open.');
                    windowWasShown = true;
                    mainWindow.show(); // Show anyway so user sees something
                }
            });
    };

    loadFrontend();

    attachRendererMediaPermissions(mainWindow.webContents.session);

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

function startPythonBackend() {
    const scriptPath = path.join(__dirname, '../backend/server.py');
    const pythonBin = resolvePythonExecutable();
    console.log(`Starting Python backend: ${scriptPath}`);
    console.log(`Using Python interpreter: ${pythonBin}`);

    pythonProcess = spawn(pythonBin, ['-u', scriptPath], {
        cwd: path.join(__dirname, '../backend'),
        env: {
            ...process.env,
            ALSA_LOG_LEVEL: process.env.ALSA_LOG_LEVEL || '0',
            PYTHONUNBUFFERED: '1',
        },
    });

    pythonProcess.stdout.on('data', (data) => {
        console.log(`[Python]: ${data}`);
    });

    pythonProcess.stderr.on('data', (data) => {
        const text = data.toString();
        const tidied = text.trimEnd();

        // Uvicorn y librerías escriben INFO/WARNING en stderr; no son errores de la app.
        const isRoutineStderr =
            /^\s*INFO:\s+/m.test(text) ||
            /^\s*WARNING:\s+(?!.*DeprecationWarning)/m.test(text) ||
            /Started server process|Waiting for application startup|Application startup complete|Uvicorn running/i.test(text) ||
            /connection open|"(?:GET|POST) \/socket\.io\//i.test(text) ||
            /^I0000 |^W0000 \d\d:/m.test(text) ||
            /Successfully initialized EGL|^GL version:|Created TensorFlow Lite|Feedback manager requires/i.test(text) ||
            /^WARNING: All log messages before absl/i.test(text);

        // PortAudio/ALSA (enumeración / conflicto de dispositivo); suele ir junto a mensajes de micrófono.
        const isAudioDriverNoise =
            /ALSA lib /i.test(text) ||
            /pa_linux_alsa/i.test(text) ||
            /Expression '\w+' failed in '.*alsa/i.test(text) ||
            /JackShm|jack server is not running|Cannot connect to server socket/i.test(text);

        // Mesa/Electron en algunos entornos (permiso sobre drivers GBM).
        const isGpuLoaderNoise =
            /MESA-LOADER:|failed to open dri:|dri_gbm\.so/i.test(text);

        if (isRoutineStderr || isAudioDriverNoise || isGpuLoaderNoise) {
            console.log(`[Python]: ${tidied}`);
        } else {
            console.error(`[Python Error]: ${tidied}`);
        }
    });
}

app.whenReady().then(() => {
    attachRendererMediaPermissions(session.defaultSession);

    ipcMain.on('window-minimize', () => {
        if (mainWindow) mainWindow.minimize();
    });

    ipcMain.on('window-maximize', () => {
        if (mainWindow) {
            if (mainWindow.isMaximized()) {
                mainWindow.unmaximize();
            } else {
                mainWindow.maximize();
            }
        }
    });

    ipcMain.on('window-toggle-devtools', () => {
        if (!mainWindow) return;
        const wc = mainWindow.webContents;
        if (wc.isDevToolsOpened()) {
            wc.closeDevTools();
        } else {
            // `mode: 'detach'` evita robar espacio a la UI principal.
            wc.openDevTools({ mode: 'detach' });
        }
    });

    ipcMain.on('window-close', () => {
        if (mainWindow) mainWindow.close();
    });

    checkBackendPort(8000).then((isTaken) => {
        if (isTaken) {
            console.log('Port 8000 is taken. Assuming backend is already running manually.');
            waitForBackend().then(createWindow);
        } else {
            startPythonBackend();
            // Give it a moment to start, then wait for health check
            setTimeout(() => {
                waitForBackend().then(createWindow);
            }, 1000);
        }
    });

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

function checkBackendPort(port) {
    return new Promise((resolve) => {
        const net = require('net');
        const server = net.createServer();
        server.once('error', (err) => {
            if (err.code === 'EADDRINUSE') {
                resolve(true);
            } else {
                resolve(false);
            }
        });
        server.once('listening', () => {
            server.close();
            resolve(false);
        });
        server.listen(port);
    });
}

function waitForBackend() {
    return new Promise((resolve) => {
        const check = () => {
            const http = require('http');
            http.get('http://127.0.0.1:8000/status', (res) => {
                if (res.statusCode === 200) {
                    console.log('Backend is ready!');
                    resolve();
                } else {
                    console.log('Backend not ready, retrying...');
                    setTimeout(check, 1000);
                }
            }).on('error', (err) => {
                console.log('Waiting for backend...');
                setTimeout(check, 1000);
            });
        };
        check();
    });
}

let windowWasShown = false;

app.on('window-all-closed', () => {
    // Only quit if the window was actually shown at least once
    // This prevents quitting during startup if window creation fails
    if (process.platform !== 'darwin' && windowWasShown) {
        app.quit();
    } else if (!windowWasShown) {
        console.log('Window was never shown - keeping app alive to allow retries');
    }
});

app.on('will-quit', () => {
    console.log('App closing... Killing Python backend.');
    if (pythonProcess) {
        if (process.platform === 'win32') {
            // Windows: Force kill the process tree synchronously
            try {
                const { execSync } = require('child_process');
                execSync(`taskkill /pid ${pythonProcess.pid} /f /t`);
            } catch (e) {
                console.error('Failed to kill python process:', e.message);
            }
        } else {
            // Unix: SIGKILL
            pythonProcess.kill('SIGKILL');
        }
        pythonProcess = null;
    }
});

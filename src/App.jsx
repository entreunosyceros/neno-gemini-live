import React, { useEffect, useState, useRef, useMemo, useCallback } from 'react';
import io from 'socket.io-client';

/**
 * Convierte una lista de bytes PCM 16-bit signed little-endian
 * (como la que emite el backend al frontend) en un array de
 * `bins` valores 0..255 que represente la amplitud absoluta media
 * por bin (envolvente útil para visualización).
 */
function pcmBytesToEnvelope(byteList, bins = 32) {
    if (!byteList || byteList.length < 2) return new Array(bins).fill(0);
    const samples = Math.floor(byteList.length / 2);
    if (samples === 0) return new Array(bins).fill(0);
    const out = new Array(bins).fill(0);
    const samplesPerBin = Math.max(1, Math.floor(samples / bins));
    for (let b = 0; b < bins; b++) {
        let sum = 0;
        let n = 0;
        const start = b * samplesPerBin;
        const end = Math.min(samples, start + samplesPerBin);
        for (let i = start; i < end; i++) {
            const lo = byteList[i * 2];
            const hi = byteList[i * 2 + 1];
            // Reconstrucción del sample 16-bit LE con signo.
            let s = (hi << 8) | lo;
            if (s & 0x8000) s -= 0x10000;
            sum += Math.abs(s);
            n++;
        }
        const avg = n > 0 ? sum / n : 0; // 0..32768
        // algo más de ganancia para que la voz del modelo se note en la barra
        out[b] = Math.max(0, Math.min(255, Math.round((avg / 32768) * 255 * 3.2)));
    }
    return out;
}

/** Límite de mensajes en memoria para que el chat no degrade al crecer (transcripción + sistema). */
const MAX_CHAT_MESSAGES = 400;

/**
 * El backend PyAudio ignora deviceId del navegador; necesita la etiqueta legible para
 * emparejar con ALSA/Pulse. Si selectedMicId no existe (p. ej. localStorage obsoleto),
 * usa el primer mic con etiqueta o el primero de la lista.
 */
function pickAudioInputForBackend(micDevices, selectedMicId) {
    if (!micDevices?.length) return { device: null, name: null };
    let d = micDevices.find((x) => x.deviceId === selectedMicId);
    if (!d) {
        d = micDevices.find((x) => x.label && String(x.label).trim());
    }
    if (!d) d = micDevices[0];
    const raw = d?.label != null ? String(d.label).trim() : '';
    const name = raw.length > 0 ? raw : null;
    return { device: d, name };
}

/** Si no hay selección guardada, prioriza entradas USB / interfaces externas en la lista del navegador. */
function preferredMicDeviceId(audioInputs) {
    if (!audioInputs?.length) return '';
    const re = /usb|yeti|rode|shure|focusrite|presonus|hyperx|elgato|snowball|audioengine|samson|behringer|audient|uac|headset|external mic|iec958|pcm2902|quadcast/i;
    const hit = audioInputs.find((d) => d.label && re.test(d.label));
    return (hit || audioInputs[0]).deviceId;
}

import Visualizer from './components/Visualizer';
import { AVATAR_FRAME_URLS, preloadAvatarFrames } from './avatarFrames';
import TopAudioBar from './components/TopAudioBar';
import BrowserWindow from './components/BrowserWindow';
import ChatModule from './components/ChatModule';
import ToolsModule from './components/ToolsModule';
import { Mic, MicOff, Settings, X, Minus, Power, Video, VideoOff, Clock } from 'lucide-react';
import ConfirmationPopup from './components/ConfirmationPopup';
import AuthLock from './components/AuthLock';
import KasaWindow from './components/KasaWindow';
import SettingsWindow from './components/SettingsWindow';


// WebSocket primero (menos latencia inicial que long-polling); polling como respaldo.
const _socketTransports = ['websocket', 'polling'];
let socket;
if (import.meta.env.VITE_SOCKET_SAME_ORIGIN === 'true') {
    // Apache (u otro proxy) sirve la SPA y hace proxy de /socket.io al backend.
    socket = io({ path: '/socket.io', transports: _socketTransports });
} else if (import.meta.env.VITE_SOCKET_URL) {
    socket = io(import.meta.env.VITE_SOCKET_URL, { transports: _socketTransports });
} else {
    const _loc = typeof window !== 'undefined' ? window.location : null;
    let _url = 'http://127.0.0.1:8000';
    if (_loc && _loc.protocol !== 'file:' && _loc.hostname) {
        _url = `${_loc.protocol}//${_loc.hostname}:8000`;
    }
    socket = io(_url, { transports: _socketTransports });
}

let ipcRenderer = null;
try {
    if (typeof window !== 'undefined' && typeof window.require === 'function') {
        ipcRenderer = window.require('electron').ipcRenderer;
    }
} catch (_) {
    /* Navegador móvil / sin Electron */
}

function App() {
    const [status, setStatus] = useState('Disconnected');
    const [socketConnected, setSocketConnected] = useState(socket.connected); // Track socket connection reactively
    // Auth State
    const [isAuthenticated, setIsAuthenticated] = useState(() => {
        // Optimistically assume authenticated if face auth is NOT enabled
        return localStorage.getItem('face_auth_enabled') !== 'true';
    });

    // Initialize from LocalStorage to prevent flash of UI
    const [isLockScreenVisible, setIsLockScreenVisible] = useState(() => {
        const saved = localStorage.getItem('face_auth_enabled');
        // If saved is 'true', we MUST start locked.
        // If 'false' or null (default off), we start unlocked.
        return saved === 'true';
    });

    // Local state for tracking settings, also init from local storage
    const [faceAuthEnabled, setFaceAuthEnabled] = useState(() => {
        return localStorage.getItem('face_auth_enabled') === 'true';
    });


    const [isConnected, setIsConnected] = useState(true); // Power state DEFAULT ON
    const [isMuted, setIsMuted] = useState(true); // Mic starts muted; user clicks the mic button to start talking
    const [isVideoOn, setIsVideoOn] = useState(false); // Video state
    const [messages, setMessages] = useState([]);
    const [inputValue, setInputValue] = useState('');
    const [browserData, setBrowserData] = useState({ image: null, logs: [] });
    const [confirmationRequest, setConfirmationRequest] = useState(null); // { id, tool, args }
    const [kasaDevices, setKasaDevices] = useState([]);
    const [showKasaWindow, setShowKasaWindow] = useState(false);
    const [showBrowserWindow, setShowBrowserWindow] = useState(false);

    const [currentTime, setCurrentTime] = useState(new Date()); // Live clock


    // RESTORED STATE
    const [aiAudioData, setAiAudioData] = useState(new Array(64).fill(0));
    const [micAudioData, setMicAudioData] = useState(new Array(32).fill(0));
    const [fps, setFps] = useState(0);
    const [avatarFrameIndex, setAvatarFrameIndex] = useState(0);
    const [aiSpeaking, setAiSpeaking] = useState(false);

    // Device states - microphones, speakers, webcams
    const [micDevices, setMicDevices] = useState([]);
    const [speakerDevices, setSpeakerDevices] = useState([]);
    const [webcamDevices, setWebcamDevices] = useState([]);

    // Selected device IDs - restored from localStorage
    const [selectedMicId, setSelectedMicId] = useState(() => localStorage.getItem('selectedMicId') || '');
    const [selectedSpeakerId, setSelectedSpeakerId] = useState(() => localStorage.getItem('selectedSpeakerId') || '');
    const [selectedWebcamId, setSelectedWebcamId] = useState(() => localStorage.getItem('selectedWebcamId') || '');
    const [showSettings, setShowSettings] = useState(false);
    const [currentProject, setCurrentProject] = useState('default');

    // Modular Mode State
    const [isModularMode, setIsModularMode] = useState(false);
    const [elementPositions, setElementPositions] = useState({
        video: { x: 40, y: 80 }, // Initial positions (approximate)
        visualizer: { x: window.innerWidth / 2, y: window.innerHeight / 2 - 150 },
        chat: { x: window.innerWidth / 2, y: window.innerHeight / 2 + 100 },
        browser: { x: window.innerWidth / 2 - 300, y: window.innerHeight / 2 },
        kasa: { x: window.innerWidth / 2 + 350, y: window.innerHeight / 2 - 100 },
        tools: { x: window.innerWidth / 2, y: window.innerHeight - 100 } // Fixed bottom OFFSET
    });

    const [elementSizes, setElementSizes] = useState({
        visualizer: { w: 550, h: 350 },
        chat: { w: 550, h: 220 },
        tools: { w: 500, h: 80 }, // Approx
        browser: { w: 550, h: 380 },
        video: { w: 320, h: 180 },
        kasa: { w: 300, h: 380 }, // Approx
    });
    const [activeDragElement, setActiveDragElement] = useState(null);

    // Z-Index Stacking Order (last element = highest z-index)
    const [zIndexOrder, setZIndexOrder] = useState([
        'visualizer', 'chat', 'tools', 'video', 'browser', 'kasa'
    ]);

    // Web Audio Context for Mic Visualization
    const audioContextRef = useRef(null);
    const analyserRef = useRef(null);
    const sourceRef = useRef(null);
    const animationFrameRef = useRef(null);
    /** Stream del visualizador del mic; hay que parar las pistas para no bloquear PyAudio/ALSA. */
    const micVisualizerStreamRef = useRef(null);
    /** Limita actualizaciones del nivel de voz del modelo para no saturar React durante la reproducción. */
    const lastAiAudioUiRef = useRef(0);
    const lastAiAudioAtRef = useRef(0);
    const aiAudioDataRef = useRef(aiAudioData);
    const prevAiSpeakingRef = useRef(false);
    const lastAvatarFrameAdvanceRef = useRef(0);
    const selectedMicIdRef = useRef(selectedMicId);
    useEffect(() => {
        selectedMicIdRef.current = selectedMicId;
    }, [selectedMicId]);

    // Video Refs
    const videoRef = useRef(null);
    const canvasRef = useRef(null);
    const transmissionCanvasRef = useRef(null); // Dedicated canvas for resizing payload
    const videoIntervalRef = useRef(null);
    const lastFrameTimeRef = useRef(0);
    const frameCountRef = useRef(0);
    // Ref to track video state for the loop (avoids closure staleness)
    const isVideoOnRef = useRef(false);
    const isModularModeRef = useRef(false);
    const elementPositionsRef = useRef(elementPositions);
    const activeDragElementRef = useRef(null);
    const lastActiveDragElementRef = useRef(null);

    // Mouse Drag Refs
    const dragOffsetRef = useRef({ x: 0, y: 0 });
    const isDraggingRef = useRef(false);

    // Update refs when state changes
    useEffect(() => {
        isModularModeRef.current = isModularMode;
        elementPositionsRef.current = elementPositions;
    }, [isModularMode, elementPositions]);

    // Live Clock Update
    useEffect(() => {
        const timer = setInterval(() => {
            setCurrentTime(new Date());
        }, 1000);
        return () => clearInterval(timer);
    }, []);

    // Aplica el tema persistido en localStorage al cargar (antes de que llegue el evento del backend),
    // para evitar el flash de color por defecto.
    useEffect(() => {
        const saved = localStorage.getItem('theme');
        if (saved) {
            document.documentElement.dataset.theme = saved;
        }
    }, []);

    // Centering Logic (Startup & Resize)
    useEffect(() => {
        const centerElements = () => {
            const width = window.innerWidth;
            const height = window.innerHeight;

            // Calculate available vertical space
            // Tools is fixed at bottom ~100px space
            const toolsY = height - 100;
            // ToolsModule uses translate(-50%, -50%). So its Center Y.
            // Let's reserve bottom 140px for tools to be safe and float it nicely.
            const toolsCenterY = height - 100;

            const gap = 20;

            // Chat: Anchor is Top-Center (translate(-50%, 0)).
            // We want Chat Bottom to be above Tools Top.
            // Tools Top = toolsCenterY - (ToolsHeight/2) approx 40 = height - 140;
            const chatBottomLimit = height - 140;

            // Dynamic Height Calculation to fit screen
            // Standard Heights
            let vizH = 400;
            let chatH = 250;
            const topBarHeight = 60;

            // Total needed: TopBar + Viz + Gap + Chat + Gap + Tools (140 reserved)
            const totalNeeded = topBarHeight + vizH + gap + chatH + gap + 140;

            if (height < totalNeeded) {
                // Scale down
                const available = height - topBarHeight - 140 - (gap * 2);
                // Allocate 60% to Viz, 40% to Chat
                vizH = available * 0.6;
                chatH = available * 0.4;
            }

            // Positions
            // Visualizer (Center Anchored)
            // Top of Viz = TopBarHeight. Center = TopBarHeight + VizH/2
            const vizY = topBarHeight + (vizH / 2); // Removed buffer

            // Chat (Top Anchored)
            // Top of Chat = TopBarHeight + VizH + Gap
            const chatY = topBarHeight + vizH + gap;

            setElementSizes(prev => ({
                ...prev,
                visualizer: { w: Math.min(600, width * 0.8), h: vizH },
                chat: { w: Math.min(600, width * 0.9), h: chatH }
            }));

            setElementPositions(prev => ({
                ...prev,
                visualizer: {
                    x: width / 2,
                    y: vizY
                },
                chat: {
                    x: width / 2,
                    y: chatY
                },
                tools: {
                    x: width / 2,
                    y: toolsCenterY
                }
            }));
        };

        // Center on mount
        centerElements();

        // Center on resize
        window.addEventListener('resize', centerElements);
        return () => window.removeEventListener('resize', centerElements);
    }, []);

    // Utility: Clamp position to viewport so component stays fully visible
    const clampToViewport = (pos, size) => {
        const margin = 10;
        const topBarHeight = 60;
        const width = window.innerWidth;
        const height = window.innerHeight;

        return {
            x: Math.max(size.w / 2 + margin, Math.min(width - size.w / 2 - margin, pos.x)),
            y: Math.max(size.h / 2 + margin + topBarHeight, Math.min(height - size.h / 2 - margin, pos.y))
        };
    };

    // Utility: Get z-index for an element based on stacking order
    const getZIndex = (id) => {
        const baseZ = 30; // Above background elements
        const index = zIndexOrder.indexOf(id);
        return baseZ + (index >= 0 ? index : 0);
    };

    // Utility: Bring element to front (highest z-index)
    const bringToFront = (id) => {
        setZIndexOrder(prev => {
            const filtered = prev.filter(el => el !== id);
            return [...filtered, id]; // Move to end = highest z-index
        });
    };

    // Ref to track if model has been auto-connected (prevents duplicate connections)
    const hasAutoConnectedRef = useRef(false);

    // Auto-Connect Model on Start (Only after Auth and devices loaded)
    // En Electron/Chromium las etiquetas del mic suelen llegar vacías en el primer enumerate;
    // sin etiqueta el backend cae en "pulse" (mic por defecto del sistema) y parece que el USB no funciona.
    useEffect(() => {
        if (!isConnected || !isAuthenticated || !socketConnected || micDevices.length === 0 || hasAutoConnectedRef.current) {
            return undefined;
        }

        const hasLabel = micDevices.some((d) => d.label && String(d.label).trim());
        let cancelled = false;

        const runConnect = () => {
            if (cancelled || hasAutoConnectedRef.current) return;
            hasAutoConnectedRef.current = true;
            socket.emit('discover_kasa');
            setTimeout(async () => {
                if (cancelled) return;
                let inputs = micDevices;
                try {
                    const raw = await navigator.mediaDevices.enumerateDevices();
                    inputs = raw.filter((x) => x.kind === 'audioinput');
                    if (inputs.length) setMicDevices(inputs);
                } catch (_) { /* mantener lista anterior */ }
                const micId = selectedMicIdRef.current;
                const { name } = pickAudioInputForBackend(inputs, micId);
                console.log('[Mic] auto start_audio → device_name=', name, 'selectedMicId=', micId);
                setStatus('Connecting...');
                socket.emit('start_audio', {
                    device_index: null,
                    device_name: name,
                    muted: isMuted,
                });
            }, 700);
        };

        if (hasLabel) {
            runConnect();
            return undefined;
        }

        const tRetry = window.setTimeout(async () => {
            if (cancelled) return;
            try {
                const raw = await navigator.mediaDevices.enumerateDevices();
                const inputs = raw.filter((x) => x.kind === 'audioinput');
                if (inputs.length) setMicDevices(inputs);
            } catch (_) { /* noop */ }
        }, 400);

        const tFail = window.setTimeout(() => {
            if (cancelled || hasAutoConnectedRef.current) return;
            console.warn('[Mic] Sin etiquetas de mic tras espera; conectando con fuente por defecto del sistema.');
            runConnect();
        }, 2800);

        return () => {
            cancelled = true;
            clearTimeout(tRetry);
            clearTimeout(tFail);
        };
    }, [isConnected, isAuthenticated, socketConnected, micDevices, selectedMicId, isMuted]);

    const restartBackendAudioWithMic = useCallback(async (deviceId) => {
        if (!socket?.connected) return;
        let inputs = micDevices;
        try {
            const raw = await navigator.mediaDevices.enumerateDevices();
            inputs = raw.filter((x) => x.kind === 'audioinput');
            if (inputs.length) setMicDevices(inputs);
        } catch (_) { /* */ }
        const { name } = pickAudioInputForBackend(inputs, deviceId);
        console.log('[Mic] reinicio por cambio en Ajustes →', name);
        // El backend serializa start/stop con un mutex y derriba el AudioLoop previo
        // de forma síncrona antes de crear el nuevo, así que basta con `start_audio`.
        socket.emit('start_audio', {
            device_index: null,
            device_name: name,
            muted: isMuted,
        });
    }, [micDevices, isMuted]);

    const handleMicSelectionChange = useCallback(
        (deviceId) => {
            setSelectedMicId(deviceId);
            if (isConnected) void restartBackendAudioWithMic(deviceId);
        },
        [isConnected, restartBackendAudioWithMic]
    );

    useEffect(() => {
        // Socket IO Setup
        socket.on('connect', () => {
            setStatus('Connected');
            setSocketConnected(true);
            socket.emit('get_settings');
        });
        socket.on('disconnect', () => {
            setStatus('Disconnected');
            setSocketConnected(false);
        });
        socket.on('status', (data) => {
            addMessage('System', data.msg);
            // Update status bar based on backend messages
            if (data.msg === 'N.E.N.O Started') {
                setStatus('Model Connected');
            } else if (data.msg === 'N.E.N.O Stopped') {
                setStatus('Connected');
            }
        });
        socket.on('audio_data', (data) => {
            // El backend envía PCM 16-bit signed LE (24 kHz mono) como lista de bytes.
            const now = performance.now();
            lastAiAudioAtRef.current = now;
            if (now - lastAiAudioUiRef.current < 20) return;
            lastAiAudioUiRef.current = now;
            setAiAudioData(pcmBytesToEnvelope(data.data, 32));
        });
        socket.on('auth_status', (data) => {
            console.log("Auth Status:", data);
            setIsAuthenticated(data.authenticated);
            if (data.authenticated) {
                // Quitar el bloqueo en cuanto el servidor confirma (evita overlay fantasma / pantalla negra en móvil).
                setIsLockScreenVisible(false);
            } else {
                setIsLockScreenVisible(true);
            }
        });

        socket.on('settings', (settings) => {
            console.log("[Settings] Received:", settings);
            if (!settings) return;
            if (typeof settings.face_auth_enabled !== 'undefined') {
                setFaceAuthEnabled(settings.face_auth_enabled);
                localStorage.setItem('face_auth_enabled', settings.face_auth_enabled);
                if (!settings.face_auth_enabled) {
                    setIsLockScreenVisible(false);
                }
            }
            if (typeof settings.theme === 'string') {
                document.documentElement.dataset.theme = settings.theme;
                localStorage.setItem('theme', settings.theme);
            }
            if (typeof settings.voice_name === 'string') {
                localStorage.setItem('voice_name', settings.voice_name);
            }
            if (typeof settings.response_language === 'string') {
                localStorage.setItem('response_language', settings.response_language);
            }
        });
        socket.on('error', (data) => {
            console.error("Socket Error:", data);
            addMessage('System', `Error: ${data.msg}`);
        });
        // El backend pide abrir una URL externa (p. ej. `mailto:`). En Electron
        // hay que usar `shell.openExternal`: si se asigna `location.href` al
        // mailto, el webview deja de cargar la app. En navegador, un `<a>`
        // programático evita descargar la SPA y en Android dispara el intent.
        socket.on('open_external_url', (data) => {
            const url = data?.url;
            if (!url || typeof url !== 'string') return;
            console.log('[OPEN_URL] Opening external URL:', url);
            try {
                if (typeof window !== 'undefined' && typeof window.require === 'function') {
                    try {
                        const { shell } = window.require('electron');
                        if (shell?.openExternal) {
                            void shell.openExternal(url);
                            return;
                        }
                    } catch (_) {
                        /* no es el proceso renderer de Electron o require falló */
                    }
                }
                if (url.startsWith('mailto:')) {
                    const a = document.createElement('a');
                    a.href = url;
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    return;
                }
                const w = window.open(url, '_blank', 'noopener,noreferrer');
                if (!w) window.location.href = url;
            } catch (err) {
                console.error('[OPEN_URL] Failed to open URL:', err);
                addMessage('System', 'No he podido abrir el enlace en el dispositivo.');
            }
        });
        socket.on('browser_frame', (data) => {
            setBrowserData(prev => ({
                image: data.image,
                logs: [...prev.logs, data.log].filter(l => l).slice(-50) // Keep last 50 logs
            }));
            setShowBrowserWindow(true);
            // Auto-show browser window if hidden, clamped to viewport
            if (!elementPositions.browser) {
                const size = { w: 550, h: 380 };
                const clamped = clampToViewport({ x: window.innerWidth / 2 - 200, y: window.innerHeight / 2 }, size);
                setElementPositions(prev => ({
                    ...prev,
                    browser: clamped
                }));
            }
        });

        // Transcripción en streaming: unir fragmentos y pintar como mucho una vez por frame
        // para no bloquear el hilo principal ni copiar arrays enormes en cada carácter.
        let transcriptionPending = null;
        let transcriptionRaf = null;

        const flushTranscriptionToState = () => {
            if (!transcriptionPending || !transcriptionPending.text) return;
            const pending = transcriptionPending;
            transcriptionPending = null;
            setMessages((prev) => {
                const base = prev.length > MAX_CHAT_MESSAGES ? prev.slice(-MAX_CHAT_MESSAGES) : prev;
                const lastMsg = base[base.length - 1];
                if (lastMsg && lastMsg.sender === pending.sender) {
                    return [
                        ...base.slice(0, -1),
                        { ...lastMsg, text: lastMsg.text + pending.text },
                    ];
                }
                return [
                    ...base,
                    {
                        sender: pending.sender,
                        text: pending.text,
                        time: new Date().toLocaleTimeString(),
                    },
                ];
            });
        };

        socket.on('transcription', (data) => {
            if (!data?.text) return;
            if (transcriptionPending && transcriptionPending.sender !== data.sender) {
                if (transcriptionRaf != null) {
                    cancelAnimationFrame(transcriptionRaf);
                    transcriptionRaf = null;
                }
                flushTranscriptionToState();
            }
            if (!transcriptionPending) {
                transcriptionPending = { sender: data.sender, text: data.text };
            } else {
                transcriptionPending.text += data.text;
            }
            if (transcriptionRaf == null) {
                transcriptionRaf = requestAnimationFrame(() => {
                    transcriptionRaf = null;
                    flushTranscriptionToState();
                });
            }
        });

        // Handle tool confirmation requests
        socket.on('tool_confirmation_request', (data) => {
            console.log("Received Confirmation Request:", data);
            setConfirmationRequest(data);
        });

        // Kasa Devices
        socket.on('kasa_devices', (devices) => {
            console.log("Kasa Devices:", devices);
            setKasaDevices(devices);
        });

        socket.on('kasa_update', (data) => {
            setKasaDevices(prev => prev.map(d => {
                if (d.ip === data.ip) {
                    // Update only fields that are not null/undefined
                    return {
                        ...d,
                        is_on: data.is_on !== null ? data.is_on : d.is_on,
                        brightness: data.brightness !== null ? data.brightness : d.brightness
                    };
                }
                return d;
            }));
        });

        socket.on('project_update', (data) => {
            console.log("Project Update:", data.project);
            setCurrentProject(data.project);
            addMessage('System', `Switched to project: ${data.project}`);
        });

        const refreshDevices = async () => {
            try {
                if (!navigator.mediaDevices) {
                    console.warn('[Devices] navigator.mediaDevices no disponible (HTTP en LAN no es contexto seguro en muchos móviles).');
                    return;
                }
                const devs = await navigator.mediaDevices.enumerateDevices();
                const audioInputs = devs.filter(d => d.kind === 'audioinput');
                const audioOutputs = devs.filter(d => d.kind === 'audiooutput');
                const videoInputs = devs.filter(d => d.kind === 'videoinput');

                setMicDevices(audioInputs);
                setSpeakerDevices(audioOutputs);
                setWebcamDevices(videoInputs);

                const savedMicId = localStorage.getItem('selectedMicId');
                if (savedMicId && audioInputs.some(d => d.deviceId === savedMicId)) {
                    setSelectedMicId(savedMicId);
                } else if (audioInputs.length > 0) {
                    setSelectedMicId((prev) => prev || preferredMicDeviceId(audioInputs));
                }

                const savedSpeakerId = localStorage.getItem('selectedSpeakerId');
                if (savedSpeakerId && audioOutputs.some(d => d.deviceId === savedSpeakerId)) {
                    setSelectedSpeakerId(savedSpeakerId);
                } else if (audioOutputs.length > 0) {
                    setSelectedSpeakerId(prev => prev || audioOutputs[0].deviceId);
                }

                const savedWebcamId = localStorage.getItem('selectedWebcamId');
                if (savedWebcamId && videoInputs.some(d => d.deviceId === savedWebcamId)) {
                    setSelectedWebcamId(savedWebcamId);
                } else if (videoInputs.length > 0) {
                    setSelectedWebcamId(prev => prev || videoInputs[0].deviceId);
                }
            } catch (err) {
                console.error('[Devices] enumerateDevices failed:', err);
            }
        };

        // Chromium oculta los labels (e incluso filtra dispositivos) hasta que se concede permiso de mic.
        // Pedimos permiso una vez, paramos las pistas para no retener ALSA, y enumeramos con labels reales.
        const primeMicPermissionAndEnumerate = async () => {
            if (!navigator.mediaDevices) {
                console.warn('[Devices] Sin mediaDevices: no se puede pedir mic en este origen (usa HTTPS o prueba desde el escritorio).');
                await refreshDevices();
                return;
            }
            try {
                const stream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: false,
                        noiseSuppression: false,
                        autoGainControl: false,
                    },
                });
                stream.getTracks().forEach(t => t.stop());
                console.log('[Devices] Mic permission granted; enumerating with labels');
            } catch (err) {
                console.warn('[Devices] Mic permission denied or unavailable; enumerating without labels:', err);
            } finally {
                await refreshDevices();
            }
        };

        primeMicPermissionAndEnumerate();

        const onDeviceChange = () => {
            console.log('[Devices] devicechange fired; refreshing list');
            refreshDevices();
        };
        try {
            navigator.mediaDevices?.addEventListener('devicechange', onDeviceChange);
        } catch (e) {
            console.warn('[Devices] devicechange no registrado:', e);
        }

        return () => {
            if (transcriptionRaf != null) {
                cancelAnimationFrame(transcriptionRaf);
            }
            transcriptionPending = null;

            socket.off('connect');
            socket.off('disconnect');
            socket.off('status');
            socket.off('audio_data');
            socket.off('auth_status');
            socket.off('settings');
            socket.off('browser_frame');
            socket.off('transcription');
            socket.off('tool_confirmation_request');
            socket.off('kasa_devices');
            socket.off('kasa_update');
            socket.off('project_update');
            socket.off('error');
            socket.off('open_external_url');

            try {
                navigator.mediaDevices?.removeEventListener('devicechange', onDeviceChange);
            } catch (_) { /* noop */ }

            stopMicVisualizer();
            stopVideo();
        };
    }, []);

    // Initial check in case we are already connected (fix race condition)
    useEffect(() => {
        if (socket.connected) {
            setStatus('Connected');
            socket.emit('get_settings');
        }
    }, []);

    // Persist device selections to localStorage when they change
    useEffect(() => {
        if (selectedMicId) {
            localStorage.setItem('selectedMicId', selectedMicId);
            console.log('[Settings] Saved microphone:', selectedMicId);
        }
    }, [selectedMicId]);

    useEffect(() => {
        if (selectedSpeakerId) {
            localStorage.setItem('selectedSpeakerId', selectedSpeakerId);
            console.log('[Settings] Saved speaker:', selectedSpeakerId);
        }
    }, [selectedSpeakerId]);

    useEffect(() => {
        if (selectedWebcamId) {
            localStorage.setItem('selectedWebcamId', selectedWebcamId);
            console.log('[Settings] Saved webcam:', selectedWebcamId);
        }
    }, [selectedWebcamId]);

    // Mantén el visualizador del mic activo siempre que haya un mic seleccionado
    // o, si todavía no hay selección, prueba con el predeterminado del navegador.
    // Antes lo parábamos con `isConnected` para no chocar con PyAudio en ALSA `hw:`,
    // pero el backend captura por `pulse`/`pipewire` y multiplexa, así que pueden
    // coexistir. Se reintenta en `devicechange` por si los permisos cambian.
    useEffect(() => {
        startMicVisualizer(selectedMicId || null);
        const onDevs = () => startMicVisualizer(selectedMicId || null);
        try { navigator.mediaDevices.addEventListener('devicechange', onDevs); } catch (_) {}
        return () => {
            try { navigator.mediaDevices.removeEventListener('devicechange', onDevs); } catch (_) {}
        };
    }, [selectedMicId]);

    const startMicVisualizer = async (deviceId) => {
        stopMicVisualizer();
        try {
            if (!navigator.mediaDevices) {
                console.warn('[mic-visualizer] navigator.mediaDevices no disponible en este origen.');
                return;
            }
            // `deviceId: { exact }` falla en seco si el id ya no existe; sin él pedimos el default.
            const audioConstraints = deviceId
                ? { deviceId: { exact: deviceId } }
                : true;
            const stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
            micVisualizerStreamRef.current = stream;

            audioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
            // Algunos navegadores arrancan el AudioContext en estado 'suspended'.
            if (audioContextRef.current.state === 'suspended') {
                try { await audioContextRef.current.resume(); } catch (_) {}
            }
            analyserRef.current = audioContextRef.current.createAnalyser();
            analyserRef.current.fftSize = 512;
            analyserRef.current.smoothingTimeConstant = 0.65;

            sourceRef.current = audioContextRef.current.createMediaStreamSource(stream);
            sourceRef.current.connect(analyserRef.current);

            let lastUiUpdate = 0;
            const MIC_UI_MIN_MS = 33;

            const updateMicData = () => {
                if (!analyserRef.current) return;
                const n = analyserRef.current.fftSize;
                const dataArray = new Uint8Array(n);
                analyserRef.current.getByteTimeDomainData(dataArray);
                const bins = 32;
                const chunk = Math.max(1, Math.floor(n / bins));
                const out = new Array(bins);
                for (let b = 0; b < bins; b++) {
                    let sumSq = 0;
                    const start = b * chunk;
                    const end = Math.min(n, start + chunk);
                    for (let i = start; i < end; i++) {
                        const v = dataArray[i] - 128;
                        sumSq += v * v;
                    }
                    const rms = Math.sqrt(sumSq / (end - start));
                    out[b] = Math.min(255, Math.round(rms * 4.2));
                }
                const now = performance.now();
                if (now - lastUiUpdate >= MIC_UI_MIN_MS) {
                    lastUiUpdate = now;
                    setMicAudioData(out);
                }
                animationFrameRef.current = requestAnimationFrame(updateMicData);
            };

            updateMicData();
        } catch (err) {
            console.error('[mic-visualizer] getUserMedia failed:', err && err.name, err && err.message);
            // Si falla con `exact` (dispositivo cambiado/desconectado), reintenta con el default.
            if (deviceId && err && (err.name === 'OverconstrainedError' || err.name === 'NotFoundError')) {
                console.warn('[mic-visualizer] reintentando con dispositivo por defecto...');
                startMicVisualizer(null);
            }
        }
    };

    const stopMicVisualizer = () => {
        if (animationFrameRef.current) cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
        if (sourceRef.current) sourceRef.current.disconnect();
        sourceRef.current = null;
        if (audioContextRef.current) audioContextRef.current.close();
        audioContextRef.current = null;
        if (micVisualizerStreamRef.current) {
            micVisualizerStreamRef.current.getTracks().forEach((t) => t.stop());
            micVisualizerStreamRef.current = null;
        }
    };

    const startVideo = async () => {
        try {
            if (!navigator.mediaDevices) {
                addMessage('System', 'Cámara no disponible: abre la app por HTTPS o desde localhost (contexto seguro).');
                return;
            }
            // Request 1080p resolution with selected webcam
            const constraints = {
                video: {
                    width: { ideal: 1920 },
                    height: { ideal: 1080 },
                    aspectRatio: 16 / 9
                }
            };

            // Use selected webcam if available
            if (selectedWebcamId) {
                constraints.video.deviceId = { exact: selectedWebcamId };
            }

            const stream = await navigator.mediaDevices.getUserMedia(constraints);
            if (videoRef.current) {
                videoRef.current.srcObject = stream;
                videoRef.current.play();
            }

            // Initialize the transmission canvas
            if (!transmissionCanvasRef.current) {
                transmissionCanvasRef.current = document.createElement('canvas');
                transmissionCanvasRef.current.width = 640;
                transmissionCanvasRef.current.height = 360;
                console.log("Initialized transmission canvas (640x360)");
            }

            setIsVideoOn(true);
            isVideoOnRef.current = true; // Update ref for loop

            console.log("Starting video loop with webcam:", selectedWebcamId || "default");
            requestAnimationFrame(predictWebcam);

        } catch (err) {
            console.error("Error accessing camera:", err);
            addMessage('System', 'Error accessing camera');
        }
    };

    const predictWebcam = () => {
        // Use ref for checking state to avoid closure staleness
        if (!videoRef.current || !canvasRef.current || !isVideoOnRef.current) {
            return;
        }

        // Check if video has valid dimensions before drawing
        if (videoRef.current.readyState < 2 || videoRef.current.videoWidth === 0 || videoRef.current.videoHeight === 0) {
            requestAnimationFrame(predictWebcam);
            return;
        }

        // 1. Draw Video to Local Display Canvas (Native Resolution)
        const ctx = canvasRef.current.getContext('2d');

        // Ensure canvas matches video dimensions
        if (canvasRef.current.width !== videoRef.current.videoWidth || canvasRef.current.height !== videoRef.current.videoHeight) {
            canvasRef.current.width = videoRef.current.videoWidth;
            canvasRef.current.height = videoRef.current.videoHeight;
        }

        ctx.drawImage(videoRef.current, 0, 0, canvasRef.current.width, canvasRef.current.height);

        // 2. Send Frame to Backend (Throttled & Resized)
        // Only send if connected
        if (isConnected) {
            // Simple throttle: every 5th frame roughly
            if (frameCountRef.current % 5 === 0) {

                // Use dedicated transmission canvas for resizing
                const transCanvas = transmissionCanvasRef.current;
                if (transCanvas) {
                    const transCtx = transCanvas.getContext('2d');
                    // Draw resized image
                    transCtx.drawImage(videoRef.current, 0, 0, transCanvas.width, transCanvas.height);

                    // Convert resized image to blob
                    transCanvas.toBlob((blob) => {
                        if (blob) {
                            socket.emit('video_frame', { image: blob });
                        }
                    }, 'image/jpeg', 0.6); // Slightly higher compression for speed
                }
            }
        }

        // FPS Calculation
        const now = performance.now();
        frameCountRef.current++;
        if (now - lastFrameTimeRef.current >= 1000) {
            setFps(frameCountRef.current);
            frameCountRef.current = 0;
            lastFrameTimeRef.current = now;
        }

        if (isVideoOnRef.current) {
            requestAnimationFrame(predictWebcam);
        }
    };

    const stopVideo = () => {
        if (videoRef.current && videoRef.current.srcObject) {
            videoRef.current.srcObject.getTracks().forEach(track => track.stop());
            videoRef.current.srcObject = null;
        }
        setIsVideoOn(false);
        isVideoOnRef.current = false; // Update ref
        setFps(0);
    };

    const toggleVideo = () => {
        if (isVideoOn) {
            stopVideo();
        } else {
            startVideo();
        }
    };

    const addMessage = (sender, text) => {
        setMessages((prev) => {
            const next = [...prev, { sender, text, time: new Date().toLocaleTimeString() }];
            return next.length > MAX_CHAT_MESSAGES ? next.slice(-MAX_CHAT_MESSAGES) : next;
        });
    };

    const togglePower = () => {
        if (isConnected) {
            socket.emit('stop_audio');
            setIsConnected(false);
            setIsMuted(true);
        } else {
            const { name } = pickAudioInputForBackend(micDevices, selectedMicId);
            socket.emit('start_audio', {
                device_index: null,
                device_name: name,
                muted: true,
            });
            setIsConnected(true);
            setIsMuted(true);
        }
    };

    const toggleMute = () => {
        if (!isConnected) return; // Can't mute if not connected

        if (isMuted) {
            socket.emit('resume_audio');
            setIsMuted(false);
        } else {
            socket.emit('pause_audio');
            setIsMuted(true);
        }
    };

    const handleSend = (e) => {
        if (e.key === 'Enter' && inputValue.trim()) {
            socket.emit('user_input', { text: inputValue });
            addMessage('Yo', inputValue);
            setInputValue('');
        }
    };

    const handleMinimize = () => ipcRenderer?.send('window-minimize');
    const handleMaximize = () => ipcRenderer?.send('window-maximize');

    // Close Application - memory is now actively saved to project, no prompt needed
    const handleCloseRequest = () => {
        // Emit shutdown signal to backend for graceful shutdown
        // Use volatile emit with timeout fallback to ensure window closes even if server is unresponsive
        const closeWindow = () => {
            if (ipcRenderer) ipcRenderer.send('window-close');
            else window.close();
        };

        if (socket.connected) {
            console.log('[APP] Sending shutdown signal to backend...');
            socket.emit('shutdown', {}, (ack) => {
                // This callback may not be called if server uses os._exit
                console.log('[APP] Shutdown acknowledged');
                closeWindow();
            });
            // Fallback: close after 500ms if ack doesn't come back
            setTimeout(closeWindow, 500);
        } else {
            // Socket not connected, just close
            closeWindow();
        }
    };

    const handleFileUpload = (e) => {
        const file = e.target.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = (event) => {
            try {
                const textContent = event.target.result;
                // Just send the text content directly
                if (typeof textContent === 'string' && textContent.length > 0) {
                    socket.emit('upload_memory', { memory: textContent });
                    addMessage('System', 'Uploading memory...');
                } else {
                    addMessage('System', 'Empty or invalid memory file');
                }
            } catch (err) {
                console.error("Error reading file:", err);
                addMessage('System', 'Error reading memory file');
            }
        };
        reader.readAsText(file);
    };

    // handleCancelClose removed - no longer using memory prompt

    const handleConfirmTool = () => {
        if (confirmationRequest) {
            socket.emit('confirm_tool', { id: confirmationRequest.id, confirmed: true });
            setConfirmationRequest(null);
        }
    };

    const handleDenyTool = () => {
        if (confirmationRequest) {
            socket.emit('confirm_tool', { id: confirmationRequest.id, confirmed: false });
            setConfirmationRequest(null);
        }
    };

    // Updated Bounds Checking Logic
    const updateElementPosition = (id, dx, dy) => {
        setElementPositions(prev => {
            const currentPos = prev[id];
            const size = elementSizes[id] || { w: 100, h: 100 }; // Fallback
            let newX = currentPos.x + dx;
            let newY = currentPos.y + dy;

            // Bounds Logic
            // Depends on anchor point.
            // Visualizer, Tools, Browser, Kasa: translate(-50%, -50%) -> Center Anchor
            // Chat: translate(-50%, 0) -> Top-Center Anchor
            // Video: Top-Left Anchor (default div)

            const width = window.innerWidth;
            const height = window.innerHeight;
            const margin = 0; // Strict bounds

            if (id === 'chat') {
                // Anchor: Top-Center (x is center, y is top)
                // X Bounds: size.w/2 <= x <= width - size.w/2
                newX = Math.max(size.w / 2 + margin, Math.min(width - size.w / 2 - margin, newX));
                // Y Bounds: 0 <= y <= height - size.h
                newY = Math.max(margin, Math.min(height - size.h - margin, newY));

            } else if (id === 'video') {
                // Anchor: Top-Left
                newX = Math.max(margin, Math.min(width - size.w - margin, newX));
                newY = Math.max(margin, Math.min(height - size.h - margin, newY));

            } else {
                // Anchor: Center
                newX = Math.max(size.w / 2 + margin, Math.min(width - size.w / 2 - margin, newX));
                newY = Math.max(size.h / 2 + margin, Math.min(height - size.h / 2 - margin, newY));
            }

            return {
                ...prev,
                [id]: {
                    x: newX,
                    y: newY
                }
            };
        });
    };

    // --- MOUSE DRAG HANDLERS ---
    const handleMouseDown = (e, id) => {
        console.log(`[MouseDrag] MouseDown on ${id}`, { target: e.target.tagName });

        // Fixed elements that should never be draggable (even in modular mode)
        const fixedElements = ['visualizer', 'chat', 'video', 'tools'];
        if (fixedElements.includes(id)) {
            console.log(`[MouseDrag] ${id} is a fixed element, not draggable`);
            return;
        }

        // Bring clicked element to front (z-index)
        bringToFront(id);

        // Prevent dragging if interacting with inputs, buttons, or canvas (for 3D controls)
        const tagName = e.target.tagName.toLowerCase();
        if (tagName === 'input' || tagName === 'button' || tagName === 'textarea' || tagName === 'canvas' || e.target.closest('button')) {
            console.log("[MouseDrag] Interaction blocked by interactive element");
            return;
        }

        // Check if clicking on a drag handle section (data-drag-handle attribute)
        const isDragHandle = e.target.closest('[data-drag-handle]');
        if (!isDragHandle && !isModularModeRef.current) {
            // If not clicking a drag handle and modular mode is off, don't drag
            // This allows popup windows to have dedicated drag areas
            console.log("[MouseDrag] Not a drag handle and modular mode off");
            return;
        }

        const elPos = elementPositions[id];
        if (!elPos) return;

        // Calculate offset based on anchor point
        // Most are Center Anchored (x, y is center)
        // Chat is Top-Center Anchored (x is center, y is top)
        // Video is Top-Left Anchored (x is left, y is top)

        // We want: MousePos = ElementPos + Offset
        // So: Offset = MousePos - ElementPos
        dragOffsetRef.current = {
            x: e.clientX - elPos.x,
            y: e.clientY - elPos.y
        };

        setActiveDragElement(id);
        activeDragElementRef.current = id;
        isDraggingRef.current = true;

        window.addEventListener('mousemove', handleMouseDrag);
        window.addEventListener('mouseup', handleMouseUp);
    };

    const handleMouseDrag = (e) => {
        if (!isDraggingRef.current || !activeDragElementRef.current) return;

        const id = activeDragElementRef.current;
        const currentPos = elementPositionsRef.current[id];
        if (!currentPos) return;

        // Target Position = MousePos - Offset
        // But we want delta for updateElementPosition??
        // actually updateElementPosition takes dx, dy.
        // Let's just set the position directly or calculate delta.
        // Since updateElementPosition has bounds logic, let's use it, but we need delta from PREVIOUS position?
        // OR we can refactor updateElementPosition to take absolute.
        // Let's stick to calculating new position and manually updating state with bounds logic inside a setter.

        // Actually, updateElementPosition uses setElementPositions(prev => ...).
        // Let's duplicate bounds logic for mouse drag to be precise or reuse.
        // reusing updateElementPosition requires calculating dx/dy from *current state* which might be lagging in the closure?
        // No, functional update is fine.

        // But for smooth mouse drag, absolute position is better.
        const rawNewX = e.clientX - dragOffsetRef.current.x;
        const rawNewY = e.clientY - dragOffsetRef.current.y;

        setElementPositions(prev => {
            const size = elementSizes[id] || { w: 100, h: 100 }; // Fallback
            let newX = rawNewX;
            let newY = rawNewY;

            const width = window.innerWidth;
            const height = window.innerHeight;
            const margin = 0;

            if (id === 'chat') {
                newX = Math.max(size.w / 2 + margin, Math.min(width - size.w / 2 - margin, newX));
                newY = Math.max(margin, Math.min(height - size.h - margin, newY));
            } else if (id === 'video') {
                newX = Math.max(margin, Math.min(width - size.w - margin, newX));
                newY = Math.max(margin, Math.min(height - size.h - margin, newY));
            } else {
                newX = Math.max(size.w / 2 + margin, Math.min(width - size.w / 2 - margin, newX));
                newY = Math.max(size.h / 2 + margin, Math.min(height - size.h / 2 - margin, newY));
            }

            return {
                ...prev,
                [id]: { x: newX, y: newY }
            };
        });
    };

    const handleMouseUp = () => {
        isDraggingRef.current = false;
        setActiveDragElement(null);
        activeDragElementRef.current = null;
        window.removeEventListener('mousemove', handleMouseDrag);
        window.removeEventListener('mouseup', handleMouseUp);
    };

    // Calculate Average Audio Amplitude for Background Pulse
    const audioAmp = aiAudioData.reduce((a, b) => a + b, 0) / aiAudioData.length / 255;

    useEffect(() => {
        aiAudioDataRef.current = aiAudioData;
    }, [aiAudioData]);

    useEffect(() => {
        preloadAvatarFrames();
    }, []);

    // Voz del modelo: basado en llegada de paquetes audio_data (no en envelope obsoleto).
    useEffect(() => {
        const SILENCE_MS = 480;
        const SPEAK_ON = 0.055;
        const FRAME_MS = 500;
        const n = AVATAR_FRAME_URLS.length;

        const tick = () => {
            const now = performance.now();
            const gap = now - lastAiAudioAtRef.current;
            const packetsActive = gap < SILENCE_MS;

            if (!packetsActive) {
                if (prevAiSpeakingRef.current) {
                    setAvatarFrameIndex(0);
                    setAiAudioData((prev) => prev.map((v) => Math.max(0, v * 0.6)));
                }
                prevAiSpeakingRef.current = false;
                setAiSpeaking(false);
                return;
            }

            const data = aiAudioDataRef.current;
            const peak = data.length ? Math.max(...data) / 255 : 0;
            const avg = data.length
                ? data.reduce((a, b) => a + b, 0) / data.length / 255
                : 0;
            const loud = peak > SPEAK_ON || avg > 0.03;
            const wasSpeaking = prevAiSpeakingRef.current;
            const speaking = loud || (wasSpeaking && packetsActive);

            if (!speaking) {
                prevAiSpeakingRef.current = false;
                setAiSpeaking(false);
                return;
            }

            prevAiSpeakingRef.current = true;
            setAiSpeaking(true);

            if (n > 1 && now - lastAvatarFrameAdvanceRef.current >= FRAME_MS) {
                lastAvatarFrameAdvanceRef.current = now;
                setAvatarFrameIndex((i) => (i + 1) % n);
            } else if (!wasSpeaking) {
                lastAvatarFrameAdvanceRef.current = now;
            }
        };

        const id = setInterval(tick, 45);
        return () => clearInterval(id);
    }, []);

    // Combinación mic + voz del modelo para la barra superior: el bin se queda con la mayor energía.
    const combinedAudioData = useMemo(() => {
        const len = Math.max(micAudioData.length, aiAudioData.length, 32);
        const out = new Array(len).fill(0);
        for (let i = 0; i < len; i++) {
            const m = micAudioData[i] || 0;
            const a = aiAudioData[i % aiAudioData.length] || 0;
            out[i] = Math.max(m, a);
        }
        return out;
    }, [micAudioData, aiAudioData]);

    const toggleKasaWindow = () => {
        if (!showKasaWindow) {
            // Maybe trigger discover instantly?
            if (kasaDevices.length === 0) socket.emit('discover_kasa');
        }
        setShowKasaWindow(!showKasaWindow);
    };

    return (
        <div className="h-screen w-screen bg-black text-cyan-100 font-mono overflow-hidden flex flex-col relative selection:bg-cyan-900 selection:text-white">

            {/* --- PREMIUM UI LAYER --- */}

            {/* --- PREMIUM UI LAYER --- */}

            {/* --- PREMIUM UI LAYER --- */}

            {/* Logic: Show AuthLock if we are NOT authenticated AND (Lock Screen is visible OR Auth is Enabled) 
                Actually, simpler: isLockScreenVisible is the source of truth for visibility.
                We set isLockScreenVisible = true via socket if auth is required.
             */}

            {isLockScreenVisible && (
                <AuthLock
                    socket={socket}
                    onAuthenticated={() => setIsAuthenticated(true)}
                    onAnimationComplete={() => setIsLockScreenVisible(false)}
                />
            )}

            {/* --- PREMIUM UI LAYER --- */}

            {/* Background Grid/Effects - ALIVE BACKGROUND (Fixed: Static opacity) */}
            <div
                className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,_var(--tw-gradient-stops))] from-gray-900 via-black to-black z-0 pointer-events-none"
                style={{ opacity: 0.6 }}
            ></div>
            <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-20 z-0 pointer-events-none mix-blend-overlay"></div>

            {/* Ambient Glow (Fixed: Static) */}
            <div
                className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[800px] h-[800px] bg-cyan-900/10 rounded-full blur-[120px] pointer-events-none"
            />

            {/* Top Bar (Draggable) */}
            <div className="z-50 flex items-center justify-between p-2 border-b border-cyan-500/20 bg-black/40 backdrop-blur-md select-none sticky top-0" style={{ WebkitAppRegion: 'drag' }}>
                <div className="flex items-center gap-4 pl-2">
                    <h1 className="text-xl font-bold tracking-[0.2em] text-cyan-400 drop-shadow-[0_0_10px_rgba(34,211,238,0.5)]">
                        N.E.N.O
                    </h1>
                    <div className="text-[10px] text-cyan-700 border border-cyan-900 px-1 rounded">
                        V2.0.0
                    </div>
                    {/* FPS Counter */}
                    {isVideoOn && (
                        <div className="text-[10px] text-green-500 border border-green-900 px-1 rounded ml-2">
                            FPS: {fps}
                        </div>
                    )}
                    {/* Connected Smart Devices Count */}
                    {kasaDevices.length > 0 && (
                        <div className="flex items-center gap-1.5 text-[10px] text-yellow-400 border border-yellow-500/30 bg-yellow-500/10 px-2 py-0.5 rounded ml-2">
                            <span>💡</span>
                            <span>{kasaDevices.length} Device{kasaDevices.length !== 1 ? 's' : ''}</span>
                        </div>
                    )}
                </div>

                {/* Top Visualizer (Mic + voz del modelo) */}
                <div className="flex-1 flex justify-center mx-4">
                    <TopAudioBar audioData={combinedAudioData} />
                </div>

                <div className="flex items-center gap-2 pr-2" style={{ WebkitAppRegion: 'no-drag' }}>
                    {/* Live Clock */}
                    <div className="flex items-center gap-1.5 text-[11px] text-cyan-300/70 font-mono px-2">
                        <Clock size={12} className="text-cyan-500/50" />
                        <span>{currentTime.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                    </div>
                    <button onClick={handleMinimize} className="p-1 hover:bg-cyan-900/50 rounded text-cyan-500 transition-colors">
                        <Minus size={18} />
                    </button>
                    <button onClick={handleMaximize} className="p-1 hover:bg-cyan-900/50 rounded text-cyan-500 transition-colors">
                        <div className="w-[14px] h-[14px] border-2 border-current rounded-[2px]" />
                    </button>
                    <button onClick={handleCloseRequest} className="p-1 hover:bg-red-900/50 rounded text-red-500 transition-colors">
                        <X size={18} />
                    </button>
                </div>
            </div>

            {/* Main Content */}
            <div className="flex-1 relative z-10 flex flex-col items-center justify-center">
                {/* Central Visualizer (AI Audio) */}
                <div
                    id="visualizer"
                    className={`absolute flex items-center justify-center transition-all duration-200 
                        backdrop-blur-xl bg-black/30 border border-white/10 shadow-2xl overflow-visible
                        ${isModularMode ? (activeDragElement === 'visualizer' ? 'ring-2 ring-green-500 bg-green-500/10' : 'ring-1 ring-yellow-500/30 bg-yellow-500/5') + ' rounded-2xl pointer-events-auto' : 'rounded-2xl pointer-events-none'}
                    `}
                    style={{
                        left: elementPositions.visualizer.x,
                        top: elementPositions.visualizer.y,
                        transform: 'translate(-50%, -50%)',
                        width: elementSizes.visualizer.w,
                        height: elementSizes.visualizer.h
                    }}
                    onMouseDown={(e) => handleMouseDown(e, 'visualizer')}
                >
                    <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-10 pointer-events-none mix-blend-overlay z-10"></div>
                    <div className="relative z-20">
                        <Visualizer
                            audioData={aiAudioData}
                            intensity={audioAmp}
                            aiSpeaking={aiSpeaking}
                            frameUrls={AVATAR_FRAME_URLS}
                            frameIndex={avatarFrameIndex}
                            width={elementSizes.visualizer.w}
                            height={elementSizes.visualizer.h}
                        />
                    </div>
                    {isModularMode && <div className={`absolute top-2 right-2 text-xs font-bold tracking-widest z-20 ${activeDragElement === 'visualizer' ? 'text-green-500' : 'text-yellow-500/50'}`}>VISUALIZER</div>}
                </div>

                {/* Video Feed Overlay */}
                {/* Floating Project Label */}
                <div className="absolute top-[70px] left-1/2 -translate-x-1/2 text-cyan-500 text-xs font-mono tracking-widest pointer-events-none z-50 bg-black/50 px-2 py-1 rounded backdrop-blur-sm border border-cyan-500/20">
                    PROJECT: {currentProject?.toUpperCase()}
                </div>

                <div
                    id="video"
                    className={`fixed bottom-4 right-4 transition-all duration-200 
                        ${isVideoOn ? 'opacity-100' : 'opacity-0 pointer-events-none'} 
                        backdrop-blur-md bg-black/40 border border-white/10 shadow-xl rounded-xl
                    `}
                    style={{ zIndex: 20 }}
                >
                    <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-5 pointer-events-none mix-blend-overlay"></div>
                    {/* Compact Display Container (1080p Source) */}
                    <div className="relative border border-cyan-500/30 rounded-lg overflow-hidden shadow-[0_0_20px_rgba(6,182,212,0.1)] w-80 aspect-video bg-black/80">
                        {/* Hidden Video Element (Source) */}
                        <video ref={videoRef} autoPlay muted className="absolute inset-0 w-full h-full object-cover opacity-0" />

                        <div className="absolute top-2 left-2 text-[10px] text-cyan-400 bg-black/60 backdrop-blur px-2 py-0.5 rounded border border-cyan-500/20 z-10 font-bold tracking-wider">CAM_01</div>

                        {/* Canvas de previsualización de la cámara */}
                        <canvas
                            ref={canvasRef}
                            className="absolute inset-0 w-full h-full opacity-80"
                        />
                    </div>
                </div>

                {/* Settings Modal - Moved outside Video so it shows independently */}
                {showSettings && (
                    <SettingsWindow
                        socket={socket}
                        micDevices={micDevices}
                        speakerDevices={speakerDevices}
                        webcamDevices={webcamDevices}
                        selectedMicId={selectedMicId}
                        onMicChange={handleMicSelectionChange}
                        selectedSpeakerId={selectedSpeakerId}
                        setSelectedSpeakerId={setSelectedSpeakerId}
                        selectedWebcamId={selectedWebcamId}
                        setSelectedWebcamId={setSelectedWebcamId}
                        handleFileUpload={handleFileUpload}
                        onClose={() => setShowSettings(false)}
                    />
                )}



                {/* Browser Window Overlay */}
                {showBrowserWindow && (
                    <div
                        id="browser"
                        className={`absolute flex flex-col transition-all duration-200 
                        backdrop-blur-xl bg-black/40 border border-white/10 shadow-2xl overflow-hidden rounded-lg
                        ${activeDragElement === 'browser' ? 'ring-2 ring-green-500 bg-green-500/10' : ''}
                    `}
                        style={{
                            left: elementPositions.browser?.x || window.innerWidth / 2 - 200,
                            top: elementPositions.browser?.y || window.innerHeight / 2,
                            transform: 'translate(-50%, -50%)',
                            width: `${elementSizes.browser.w}px`,
                            height: `${elementSizes.browser.h}px`,
                            pointerEvents: 'auto',
                            zIndex: getZIndex('browser')
                        }}
                        onMouseDown={(e) => handleMouseDown(e, 'browser')}
                    >
                        <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-10 pointer-events-none mix-blend-overlay z-10"></div>
                        <div className="relative z-20 w-full h-full">
                            <BrowserWindow
                                imageSrc={browserData.image}
                                logs={browserData.logs}
                                onClose={() => setShowBrowserWindow(false)}
                                socket={socket}
                            />
                        </div>
                    </div>
                )}


                {/* Chat Module */}
                <ChatModule
                    messages={messages}
                    inputValue={inputValue}
                    setInputValue={setInputValue}
                    handleSend={handleSend}
                    isModularMode={isModularMode}
                    activeDragElement={activeDragElement}
                    position={elementPositions.chat}
                    width={elementSizes.chat.w}
                    height={elementSizes.chat.h}
                    onMouseDown={(e) => handleMouseDown(e, 'chat')}
                />

                {/* Footer Controls / Tools Module */}
                <div className="z-20 flex justify-center pb-10 pointer-events-none">
                    <ToolsModule
                        isConnected={isConnected}
                        isMuted={isMuted}
                        isVideoOn={isVideoOn}
                        showSettings={showSettings}
                        onTogglePower={togglePower}
                        onToggleMute={toggleMute}
                        onToggleVideo={toggleVideo}
                        onToggleSettings={() => setShowSettings(!showSettings)}
                        onToggleKasa={toggleKasaWindow}
                        showKasaWindow={showKasaWindow}
                        onToggleBrowser={() => setShowBrowserWindow(!showBrowserWindow)}
                        showBrowserWindow={showBrowserWindow}
                        position={elementPositions.tools}
                        onMouseDown={(e) => handleMouseDown(e, 'tools')}
                    />
                </div>

                {/* Kasa Window */}
                {showKasaWindow && (
                    <KasaWindow
                        socket={socket}
                        position={elementPositions.kasa}
                        activeDragElement={activeDragElement}
                        setActiveDragElement={setActiveDragElement}
                        devices={kasaDevices}
                        onClose={() => setShowKasaWindow(false)}
                        onMouseDown={(e) => handleMouseDown(e, 'kasa')}
                        zIndex={getZIndex('kasa')}
                    />
                )}

                {/* Tool Confirmation Modal */}
                <ConfirmationPopup
                    request={confirmationRequest}
                    onConfirm={handleConfirmTool}
                    onDeny={handleDenyTool}
                />
            </div>
        </div>
    );
}

export default App;

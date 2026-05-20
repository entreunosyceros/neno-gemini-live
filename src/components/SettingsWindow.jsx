import React, { useState, useEffect } from 'react';
import { X, Code2 } from 'lucide-react';

// IPC opcional: solo disponible bajo Electron. En tests/web puro no rompemos.
let ipcRendererSafe = null;
try {
    if (typeof window !== 'undefined' && window.require) {
        ipcRendererSafe = window.require('electron').ipcRenderer;
    }
} catch (_) {
    ipcRendererSafe = null;
}

const TOOLS = [
    { id: 'run_web_agent', label: 'Agente web' },
    { id: 'create_directory', label: 'Crear carpeta' },
    { id: 'write_file', label: 'Escribir archivo' },
    { id: 'read_directory', label: 'Leer directorio' },
    { id: 'read_file', label: 'Leer archivo' },
    { id: 'create_project', label: 'Crear proyecto' },
    { id: 'switch_project', label: 'Cambiar proyecto' },
    { id: 'list_projects', label: 'Listar proyectos' },
    { id: 'list_smart_devices', label: 'Listar dispositivos' },
    { id: 'control_light', label: 'Controlar luz' },
    { id: 'open_email_client', label: 'Abrir cliente de correo' },
    { id: 'open_document', label: 'Abrir documento (app del sistema)' },
];

/** Grupos de extensiones para la política «Abrir documentos» (IA con `open_document`). */
const OPEN_DOCUMENT_TYPE_PRESETS = [
    { id: 'pdf', label: 'PDF', exts: ['.pdf'] },
    { id: 'text', label: 'Texto / Markdown', exts: ['.txt', '.md', '.rst', '.log'] },
    { id: 'office', label: 'Office y ODF', exts: ['.doc', '.docx', '.odt', '.rtf', '.xls', '.xlsx', '.ods', '.ppt', '.pptx', '.odp'] },
    { id: 'images', label: 'Imágenes', exts: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.tif', '.tiff', '.ico'] },
    { id: 'data', label: 'CSV / JSON / XML', exts: ['.csv', '.tsv', '.json', '.xml'] },
    { id: 'web', label: 'HTML', exts: ['.html', '.htm', '.mhtml'] },
    { id: 'code', label: 'Código fuente', exts: ['.py', '.js', '.jsx', '.ts', '.tsx', '.java', '.c', '.cpp', '.h', '.hpp', '.cs', '.go', '.rs', '.php', '.rb', '.swift', '.kt', '.sql', '.sh', '.bash', '.zsh'] },
    { id: 'media', label: 'Audio / vídeo', exts: ['.mp3', '.wav', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.mp4', '.webm', '.mkv', '.avi', '.mov'] },
    { id: 'cad', label: 'CAD / 3D', exts: ['.stl', '.obj', '.step', '.stp', '.iges', '.igs', '.dxf', '.dwg'] },
];

function normalizeExtList(list) {
    if (!Array.isArray(list)) return [];
    const out = new Set();
    list.forEach((x) => {
        if (typeof x !== 'string') return;
        let s = x.trim().toLowerCase();
        if (!s) return;
        if (!s.startsWith('.')) s = `.${s}`;
        out.add(s);
    });
    return Array.from(out).sort();
}

function flattenAllPresetExts() {
    const s = new Set();
    OPEN_DOCUMENT_TYPE_PRESETS.forEach((p) => p.exts.forEach((e) => s.add(e)));
    return Array.from(s).sort();
}

// Voces disponibles en Gemini Live (Native Audio).
// Charon/Fenrir/Puck/Orus = masculinas; Kore/Aoede/Leda/Zephyr = femeninas.
const VOICES = [
    { id: 'Charon', label: 'Charon (masculina, grave)' },
    { id: 'Fenrir', label: 'Fenrir (masculina)' },
    { id: 'Puck', label: 'Puck (masculina, joven)' },
    { id: 'Orus', label: 'Orus (masculina, cálida)' },
    { id: 'Kore', label: 'Kore (femenina)' },
    { id: 'Aoede', label: 'Aoede (femenina)' },
    { id: 'Leda', label: 'Leda (femenina, suave)' },
    { id: 'Zephyr', label: 'Zephyr (femenina, brillante)' },
];

const THEMES = [
    { id: 'cyan', label: 'Cian (por defecto)' },
    { id: 'amber', label: 'Ámbar' },
    { id: 'magenta', label: 'Magenta' },
    { id: 'emerald', label: 'Esmeralda' },
    { id: 'violet', label: 'Violeta' },
];

// Debe coincidir con backend/neno.py AVAILABLE_RESPONSE_LANGUAGES
const RESPONSE_LANGUAGES = [
    { id: 'es_es', label: 'Español (España)' },
    { id: 'es_419', label: 'Español (Latinoamérica)' },
    { id: 'en', label: 'Inglés' },
    { id: 'fr', label: 'Francés' },
    { id: 'de', label: 'Alemán' },
    { id: 'pt', label: 'Portugués' },
    { id: 'it', label: 'Italiano' },
    { id: 'ca', label: 'Catalán' },
    { id: 'gl', label: 'Galego' },
];

const SettingsWindow = ({
    socket,
    micDevices,
    speakerDevices,
    webcamDevices,
    selectedMicId,
    onMicChange,
    selectedSpeakerId,
    setSelectedSpeakerId,
    selectedWebcamId,
    setSelectedWebcamId,
    handleFileUpload,
    onClose
}) => {
    const [permissions, setPermissions] = useState({});
    const [faceAuthEnabled, setFaceAuthEnabled] = useState(false);
    const [voiceName, setVoiceName] = useState(() => localStorage.getItem('voice_name') || 'Charon');
    const [responseLanguage, setResponseLanguage] = useState(() => localStorage.getItem('response_language') || 'es_es');
    const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'cyan');
    const [docLimitExts, setDocLimitExts] = useState(false);
    const [docAllowDirs, setDocAllowDirs] = useState(true);
    const [docAllowedExts, setDocAllowedExts] = useState([]);

    useEffect(() => {
        socket.emit('get_settings');

        const handleSettings = (settings) => {
            console.log('Received settings:', settings);
            if (!settings) return;
            if (settings.tool_permissions) setPermissions(settings.tool_permissions);
            if (typeof settings.face_auth_enabled !== 'undefined') {
                setFaceAuthEnabled(settings.face_auth_enabled);
                localStorage.setItem('face_auth_enabled', settings.face_auth_enabled);
            }
            if (typeof settings.voice_name === 'string') {
                setVoiceName(settings.voice_name);
                localStorage.setItem('voice_name', settings.voice_name);
            }
            if (typeof settings.response_language === 'string') {
                setResponseLanguage(settings.response_language);
                localStorage.setItem('response_language', settings.response_language);
            }
            if (typeof settings.theme === 'string') {
                setTheme(settings.theme);
                localStorage.setItem('theme', settings.theme);
            }
            if (typeof settings.open_document_limit_extensions === 'boolean') {
                setDocLimitExts(settings.open_document_limit_extensions);
            }
            if (typeof settings.open_document_allow_directories === 'boolean') {
                setDocAllowDirs(settings.open_document_allow_directories);
            }
            if (Array.isArray(settings.open_document_allowed_extensions)) {
                setDocAllowedExts(normalizeExtList(settings.open_document_allowed_extensions));
            }
        };

        socket.on('settings', handleSettings);

        return () => {
            socket.off('settings', handleSettings);
        };
    }, [socket]);

    const handleVoiceChange = (e) => {
        const next = e.target.value;
        setVoiceName(next);
        localStorage.setItem('voice_name', next);
        socket.emit('update_settings', { voice_name: next });
    };

    const handleResponseLanguageChange = (e) => {
        const next = e.target.value;
        setResponseLanguage(next);
        localStorage.setItem('response_language', next);
        socket.emit('update_settings', { response_language: next });
    };

    const handleThemeChange = (e) => {
        const next = e.target.value;
        setTheme(next);
        localStorage.setItem('theme', next);
        // Aplicación inmediata para feedback instantáneo; el backend confirmará por broadcast.
        document.documentElement.dataset.theme = next;
        socket.emit('update_settings', { theme: next });
    };

    const handleToggleDevTools = () => {
        if (ipcRendererSafe) {
            ipcRendererSafe.send('window-toggle-devtools');
        } else {
            console.warn('[Settings] ipcRenderer no disponible — DevTools solo está disponible en Electron.');
        }
    };

    const togglePermission = (toolId) => {
        const currentVal = permissions[toolId] !== false; // Default True
        const nextVal = !currentVal;

        // Update local mostly for responsiveness, but socket roundtrip handles truth
        // setPermissions(prev => ({ ...prev, [toolId]: nextVal }));

        // Send update
        socket.emit('update_settings', { tool_permissions: { [toolId]: nextVal } });
    };

    const emitOpenDocumentPolicy = (overrides = {}) => {
        const limit = overrides.limit !== undefined ? overrides.limit : docLimitExts;
        const allowDirs = overrides.allowDirs !== undefined ? overrides.allowDirs : docAllowDirs;
        let exts = overrides.exts !== undefined ? [...overrides.exts] : [...docAllowedExts];
        exts = normalizeExtList(exts);
        if (limit && exts.length === 0 && overrides.exts === undefined) {
            exts = flattenAllPresetExts();
        }
        setDocLimitExts(limit);
        setDocAllowDirs(allowDirs);
        setDocAllowedExts(exts);
        socket.emit('update_settings', {
            open_document_limit_extensions: limit,
            open_document_allow_directories: allowDirs,
            open_document_allowed_extensions: exts,
        });
    };

    const toggleDocLimit = () => {
        const next = !docLimitExts;
        if (next && docAllowedExts.length === 0) {
            emitOpenDocumentPolicy({ limit: true, exts: flattenAllPresetExts() });
        } else {
            emitOpenDocumentPolicy({ limit: next });
        }
    };

    const toggleDocAllowDirs = () => {
        emitOpenDocumentPolicy({ allowDirs: !docAllowDirs });
    };

    const toggleDocCategory = (preset) => {
        const set = new Set(docAllowedExts);
        const allIn = preset.exts.every((e) => set.has(e));
        if (allIn) {
            preset.exts.forEach((e) => set.delete(e));
        } else {
            preset.exts.forEach((e) => set.add(e));
        }
        emitOpenDocumentPolicy({ exts: Array.from(set) });
    };

    const docCategoryFullySelected = (preset) => preset.exts.every((e) => docAllowedExts.includes(e));

    const toggleFaceAuth = () => {
        const newVal = !faceAuthEnabled;
        setFaceAuthEnabled(newVal); // Optimistic Update
        localStorage.setItem('face_auth_enabled', newVal);
        socket.emit('update_settings', { face_auth_enabled: newVal });
    };

    return (
        <div
            className="absolute top-20 right-10 bg-black/90 border border-cyan-500/50 rounded-lg z-50 w-80 backdrop-blur-xl shadow-[0_0_30px_rgba(6,182,212,0.2)] flex flex-col"
            style={{ maxHeight: 'calc(100vh - 6rem)' }}
        >
            {/* Header (fijo) */}
            <div className="flex justify-between items-center border-b border-cyan-900/50 px-4 py-3">
                <h2 className="text-cyan-400 font-bold text-sm uppercase tracking-wider">Ajustes</h2>
                <button onClick={onClose} className="text-cyan-600 hover:text-cyan-400">
                    <X size={16} />
                </button>
            </div>

            {/* Cuerpo desplazable */}
            <div className="flex-1 overflow-y-auto custom-scrollbar p-4">

            {/* Autenticación */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-3 text-xs uppercase tracking-wider opacity-80">Seguridad</h3>
                <div className="flex items-center justify-between text-xs bg-gray-900/50 p-2 rounded border border-cyan-900/30">
                    <span className="text-cyan-100/80">Autenticación facial</span>
                    <button
                        onClick={toggleFaceAuth}
                        className={`relative w-8 h-4 rounded-full transition-colors duration-200 ${faceAuthEnabled ? 'bg-cyan-500/80' : 'bg-gray-700'}`}
                    >
                        <div
                            className={`absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full transition-transform duration-200 ${faceAuthEnabled ? 'translate-x-4' : 'translate-x-0'}`}
                        />
                    </button>
                </div>
            </div>

            {/* Micrófono */}
            <div className="mb-4">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Micrófono</h3>
                <select
                    value={selectedMicId}
                    onChange={(e) => onMicChange(e.target.value)}
                    className="w-full bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 focus:border-cyan-400 outline-none"
                >
                    {micDevices.map((device, i) => (
                        <option key={device.deviceId} value={device.deviceId}>
                            {device.label || `Micrófono ${i + 1}`}
                        </option>
                    ))}
                </select>
            </div>

            {/* Altavoz */}
            <div className="mb-4">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Altavoz</h3>
                <select
                    value={selectedSpeakerId}
                    onChange={(e) => setSelectedSpeakerId(e.target.value)}
                    className="w-full bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 focus:border-cyan-400 outline-none"
                >
                    {speakerDevices.map((device, i) => (
                        <option key={device.deviceId} value={device.deviceId}>
                            {device.label || `Altavoz ${i + 1}`}
                        </option>
                    ))}
                </select>
            </div>

            {/* Cámara web */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Cámara web</h3>
                <select
                    value={selectedWebcamId}
                    onChange={(e) => setSelectedWebcamId(e.target.value)}
                    className="w-full bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 focus:border-cyan-400 outline-none"
                >
                    {webcamDevices.map((device, i) => (
                        <option key={device.deviceId} value={device.deviceId}>
                            {device.label || `Cámara ${i + 1}`}
                        </option>
                    ))}
                </select>
            </div>

            {/* Voz */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Voz</h3>
                <select
                    value={voiceName}
                    onChange={handleVoiceChange}
                    className="w-full bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 focus:border-cyan-400 outline-none"
                >
                    {VOICES.map((v) => (
                        <option key={v.id} value={v.id}>{v.label}</option>
                    ))}
                </select>
                <p className="mt-1 text-[10px] text-cyan-500/60">Al cambiarla se reinicia la sesión Live para aplicar la nueva voz.</p>
            </div>

            {/* Idioma de respuesta de la IA */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Idioma de la IA</h3>
                <select
                    value={responseLanguage}
                    onChange={handleResponseLanguageChange}
                    className="w-full bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 focus:border-cyan-400 outline-none"
                >
                    {RESPONSE_LANGUAGES.map((l) => (
                        <option key={l.id} value={l.id}>{l.label}</option>
                    ))}
                </select>
                <p className="mt-1 text-[10px] text-cyan-500/60">Idioma en que N.E.N.O habla y responde. Al cambiarlo se reinicia la sesión Live.</p>
            </div>

            {/* Abrir documentos: tipos de archivo que la IA puede abrir con el sistema */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">
                    Abrir documentos (IA)
                </h3>
                <p className="text-[10px] text-cyan-500/70 mb-3 leading-relaxed">
                    Controla la herramienta <code className="text-cyan-400/90">open_document</code>: qué extensiones puede abrir
                    la IA con la aplicación predeterminada del sistema y si puede abrir carpetas.
                </p>
                <div className="space-y-2">
                    <div className="flex items-center justify-between text-xs bg-gray-900/50 p-2 rounded border border-cyan-900/30">
                        <span className="text-cyan-100/80">Restringir por extensión</span>
                        <button
                            type="button"
                            onClick={toggleDocLimit}
                            className={`relative w-8 h-4 rounded-full transition-colors duration-200 ${docLimitExts ? 'bg-cyan-500/80' : 'bg-gray-700'}`}
                            aria-pressed={docLimitExts}
                        >
                            <div
                                className={`absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full transition-transform duration-200 ${docLimitExts ? 'translate-x-4' : 'translate-x-0'}`}
                            />
                        </button>
                    </div>
                    <div className="flex items-center justify-between text-xs bg-gray-900/50 p-2 rounded border border-cyan-900/30">
                        <span className="text-cyan-100/80">Permitir abrir carpetas</span>
                        <button
                            type="button"
                            onClick={toggleDocAllowDirs}
                            className={`relative w-8 h-4 rounded-full transition-colors duration-200 ${docAllowDirs ? 'bg-cyan-500/80' : 'bg-gray-700'}`}
                            aria-pressed={docAllowDirs}
                        >
                            <div
                                className={`absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full transition-transform duration-200 ${docAllowDirs ? 'translate-x-4' : 'translate-x-0'}`}
                            />
                        </button>
                    </div>
                </div>
                {docLimitExts && (
                    <div className="mt-3 space-y-2">
                        <div className="flex gap-2 flex-wrap">
                            <button
                                type="button"
                                onClick={() => emitOpenDocumentPolicy({ exts: flattenAllPresetExts() })}
                                className="text-[10px] px-2 py-1 rounded border border-cyan-800 text-cyan-200 hover:border-cyan-500"
                            >
                                Marcar todos los tipos
                            </button>
                            <button
                                type="button"
                                onClick={() => emitOpenDocumentPolicy({ exts: [] })}
                                className="text-[10px] px-2 py-1 rounded border border-cyan-800 text-cyan-200 hover:border-cyan-500"
                            >
                                Quitar todos
                            </button>
                        </div>
                        <div className="max-h-36 overflow-y-auto pr-1 custom-scrollbar space-y-1.5">
                            {OPEN_DOCUMENT_TYPE_PRESETS.map((preset) => {
                                const on = docCategoryFullySelected(preset);
                                return (
                                    <label
                                        key={preset.id}
                                        className="flex items-center gap-2 text-[11px] text-cyan-100/90 cursor-pointer select-none"
                                    >
                                        <input
                                            type="checkbox"
                                            checked={on}
                                            onChange={() => toggleDocCategory(preset)}
                                            className="accent-cyan-500 rounded border-cyan-800"
                                        />
                                        <span>{preset.label}</span>
                                    </label>
                                );
                            })}
                        </div>
                        <p className="text-[9px] text-cyan-500/55">
                            {docAllowedExts.length} extensión(es) permitida(s). Si la lista queda vacía con la restricción activa, la IA no podrá abrir archivos hasta que marques tipos aquí.
                        </p>
                    </div>
                )}
            </div>

            {/* Tema */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Tema</h3>
                <select
                    value={theme}
                    onChange={handleThemeChange}
                    className="w-full bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 focus:border-cyan-400 outline-none"
                >
                    {THEMES.map((t) => (
                        <option key={t.id} value={t.id}>{t.label}</option>
                    ))}
                </select>
            </div>

            {/* Confirmación de herramientas */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-3 text-xs uppercase tracking-wider opacity-80">Confirmar herramientas</h3>
                <p className="text-[10px] text-cyan-500/60 mb-2 leading-relaxed">
                    Si está activado, la IA pedirá tu confirmación antes de ejecutar la acción.
                </p>
                <div className="space-y-2 max-h-40 overflow-y-auto pr-2 custom-scrollbar">
                    {TOOLS.map(tool => {
                        const isRequired = permissions[tool.id] !== false; // Default True
                        return (
                            <div key={tool.id} className="flex items-center justify-between text-xs bg-gray-900/50 p-2 rounded border border-cyan-900/30">
                                <span className="text-cyan-100/80">{tool.label}</span>
                                <button
                                    onClick={() => togglePermission(tool.id)}
                                    className={`relative w-8 h-4 rounded-full transition-colors duration-200 ${isRequired ? 'bg-cyan-500/80' : 'bg-gray-700'}`}
                                >
                                    <div
                                        className={`absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full transition-transform duration-200 ${isRequired ? 'translate-x-4' : 'translate-x-0'}`}
                                    />
                                </button>
                            </div>
                        );
                    })}
                </div>
            </div>

            {/* Desarrollador */}
            <div className="mb-6">
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Desarrollador</h3>
                <button
                    onClick={handleToggleDevTools}
                    className="w-full flex items-center justify-center gap-2 bg-gray-900 border border-cyan-800 rounded p-2 text-xs text-cyan-100 hover:border-cyan-400 hover:text-cyan-400 transition-colors"
                >
                    <Code2 size={14} />
                    <span>Abrir / cerrar herramientas de desarrollo</span>
                </button>
            </div>

            {/* Memoria */}
            <div>
                <h3 className="text-cyan-400 font-bold mb-2 text-xs uppercase tracking-wider opacity-80">Memoria</h3>
                <div className="flex flex-col gap-2">
                    <label className="text-[10px] text-cyan-500/60 uppercase">Subir texto de memoria</label>
                    <input
                        type="file"
                        accept=".txt"
                        onChange={handleFileUpload}
                        className="text-xs text-cyan-100 bg-gray-900 border border-cyan-800 rounded p-2 file:mr-2 file:py-1 file:px-2 file:rounded-full file:border-0 file:text-[10px] file:font-semibold file:bg-cyan-900 file:text-cyan-400 hover:file:bg-cyan-800 cursor-pointer"
                    />
                </div>
            </div>

            </div>
        </div>
    );
};

export default SettingsWindow;

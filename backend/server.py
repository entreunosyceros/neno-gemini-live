import sys
import asyncio
import os as _os_early
import time as _time_early
import json as _json_early

# #region agent log (very-early server bootstrap)
# Solo escribe a stdout si NENO_LOG_LEVEL=DEBUG. El registro a fichero NDJSON
# se mantiene siempre porque sirve para diagnosticar fallos de arranque.
_EARLY_DEBUG_TO_STDOUT = _os_early.getenv("NENO_LOG_LEVEL", "INFO").upper() == "DEBUG"


def _server_early_dlog(loc, msg, data=None):
    try:
        backend_dir = _os_early.path.dirname(_os_early.path.abspath(__file__))
        for path in (
            _os_early.path.join(backend_dir, "debug-e3fca2.ndjson"),
            _os_early.path.join(backend_dir, "..", ".cursor", "debug-e3fca2.log"),
        ):
            try:
                d = _os_early.path.dirname(path)
                if d:
                    _os_early.makedirs(d, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(_json_early.dumps({
                        "sessionId": "e3fca2",
                        "id": f"log_{int(_time_early.time()*1000)}_{loc.replace(':','_')}",
                        "timestamp": int(_time_early.time() * 1000),
                        "location": loc,
                        "message": msg,
                        "data": data or {},
                        "runId": "initial",
                        "hypothesisId": "bootstrap_early",
                    }, default=str) + "\n")
            except Exception as e:
                if _EARLY_DEBUG_TO_STDOUT:
                    print(f"[SERVER DEBUG][_server_early_dlog] write failed at {path}: {e}", flush=True)
        if _EARLY_DEBUG_TO_STDOUT:
            print(f"[SERVER DEBUG][_server_early_dlog] {loc} :: {msg}", flush=True)
    except Exception as e:
        # Los errores fatales del helper sí se imprimen siempre: si esto
        # falla, el resto del log probablemente tampoco funcione.
        print(f"[SERVER ERROR][_server_early_dlog] FATAL: {e}", flush=True)

_server_early_dlog("server.py:line1", "process starting", {"pid": _os_early.getpid(), "argv": sys.argv})
# #endregion

# Fix for asyncio subprocess support on Windows
# MUST BE SET BEFORE OTHER IMPORTS
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import socketio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
import asyncio
import logging
import threading
import sys
import os
import json
from datetime import datetime
from pathlib import Path

from google.genai import types as genai_types


# Logger centralizado del backend.
# Por defecto en INFO (silencioso en operación normal). Para depurar:
#   NENO_LOG_LEVEL=DEBUG npm run dev
# Acepta DEBUG / INFO / WARNING / ERROR.
_LOG_LEVEL = os.getenv("NENO_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("neno.server")


# Ensure we can import neno
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import neno
from authenticator import FaceAuthenticator
from kasa_agent import KasaAgent

# Create a Socket.IO server
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
# ASGI app se construye tras definir `lifespan` y `app` (ver más abajo).


class _TranscriptionCoalescer:
    """Agrupa fragmentos de transcripción antes de emitirlos (menos tareas asyncio y menos renders en React)."""

    __slots__ = ("_emit", "_delay", "_buf", "_task")

    def __init__(self, emit, delay_s: float = 0.0):
        # delay_s=0 → `asyncio.sleep(0)`: cede un tick y agrupa ráfagas sin añadir ~40 ms de latencia.
        self._emit = emit
        self._delay = delay_s
        self._buf = None
        self._task = None

    def push(self, data: dict):
        sender = data.get("sender")
        chunk = data.get("text") or ""
        if not chunk:
            return
        if self._buf is None:
            self._buf = {"sender": sender, "text": chunk}
        elif self._buf["sender"] != sender:
            self._flush_now()
            self._buf = {"sender": sender, "text": chunk}
        else:
            self._buf["text"] += chunk
        self._schedule()

    def _flush_now(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        buf = self._buf
        self._buf = None
        if buf and buf.get("text"):
            asyncio.create_task(self._emit(dict(buf)))

    async def _flush_delayed(self):
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        else:
            buf = self._buf
            self._buf = None
            if buf and buf.get("text"):
                await self._emit(dict(buf))
        finally:
            self._task = None

    def _schedule(self):
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._flush_delayed())


import signal

# --- SHUTDOWN HANDLER ---
def signal_handler(sig, frame):
    print(f"\n[SERVER] Caught signal {sig}. Exiting gracefully...")
    # Clean up audio loop
    if audio_loop:
        try:
            print("[SERVER] Stopping Audio Loop...")
            audio_loop.stop() 
        except:
            pass
    # Force kill
    print("[SERVER] Force exiting...")
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Global state
audio_loop = None
loop_task = None
authenticator = None
kasa_agent = KasaAgent()
SETTINGS_FILE = "settings.json"

# Mutex que serializa start/stop del AudioLoop. Sin él, dos eventos
# `start_audio` consecutivos (p. ej. cambio de mic, reconexión, doble click)
# pueden crear dos AudioLoop solapados → dos sesiones Live, dos `play_audio`,
# dos `listen_audio`, lo que se oye como eco y rompe el reconocimiento.
_audio_lifecycle_lock: "asyncio.Lock | None" = None


def _get_audio_lifecycle_lock() -> asyncio.Lock:
    global _audio_lifecycle_lock
    if _audio_lifecycle_lock is None:
        _audio_lifecycle_lock = asyncio.Lock()
    return _audio_lifecycle_lock


async def _shutdown_audio_loop(reason: str) -> None:
    """Cancela y espera al `AudioLoop` actual cerrando sus streams antes de retornar.

    Debe llamarse siempre con `_audio_lifecycle_lock` en posesión del caller.
    """
    global audio_loop, loop_task
    local_loop = audio_loop
    local_task = loop_task
    audio_loop = None
    loop_task = None
    if local_loop is None and local_task is None:
        return
    log.info("[SERVER] Shutting down AudioLoop (%s)", reason)
    if local_loop is not None:
        try:
            local_loop.stop()
        except Exception as e:
            log.warning("[SERVER] AudioLoop.stop() raised: %s", e)
    if local_task is not None and not local_task.done():
        local_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(local_task), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("[SERVER] AudioLoop did not finish in 3s; continuing anyway.")
        except (asyncio.CancelledError, Exception) as e:
            log.debug("[SERVER] AudioLoop ended with: %s: %s", type(e).__name__, e)

AVAILABLE_THEMES = ("cyan", "amber", "magenta", "emerald", "violet")

DEFAULT_SETTINGS = {
    "face_auth_enabled": False, # Default OFF as requested
    "tool_permissions": {
        "run_web_agent": True,
        "write_file": True,
        "read_directory": True,
        "read_file": True,
        "create_project": True,
        "switch_project": True,
        "list_projects": True,
        # `True` = pide confirmación antes de abrir el cliente de correo del sistema.
        "open_email_client": True,
        "open_document": True,
    },
    "kasa_devices": [], # List of {ip, alias, model}
    "voice_name": "Charon",  # Voz por defecto del modelo Live (masculina, castellana).
    "response_language": "es_es",  # Idioma de respuesta Live (ver neno.AVAILABLE_RESPONSE_LANGUAGES).
    "theme": "cyan",          # Tema visual del frontend.
    # Política de la herramienta `open_document` (abrir con la app del sistema).
    "open_document_limit_extensions": False,
    "open_document_allow_directories": True,
    "open_document_allowed_extensions": [],
}

SETTINGS = DEFAULT_SETTINGS.copy()

def load_settings():
    global SETTINGS
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                loaded = json.load(f)
                # Merge with defaults to ensure new keys exist
                # Deep merge for tool_permissions would be better but shallow merge of top keys + tool_permissions check is okay for now
                for k, v in loaded.items():
                    if k == "tool_permissions" and isinstance(v, dict):
                         SETTINGS["tool_permissions"].update(v)
                    else:
                        SETTINGS[k] = v
            print(f"Loaded settings: {SETTINGS}")
        except Exception as e:
            print(f"Error loading settings: {e}")

def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(SETTINGS, f, indent=4)
        print("Settings saved.")
    except Exception as e:
        print(f"Error saving settings: {e}")

# Load on startup
load_settings()

authenticator = None
kasa_agent = KasaAgent(known_devices=SETTINGS.get("kasa_devices"))
# tool_permissions is now SETTINGS["tool_permissions"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    import sys
    log.debug("Lifespan: startup")
    log.debug("Python Version: %s", sys.version)
    try:
        loop = asyncio.get_running_loop()
        log.debug("Running Loop: %s", type(loop))
        policy = asyncio.get_event_loop_policy()
        log.debug("Current Policy: %s", type(policy))
    except Exception as e:
        log.warning("Error checking loop: %s", e)
    print("[SERVER] Startup: Initializing Kasa Agent...")
    await kasa_agent.initialize()
    yield
    log.debug("Lifespan: shutdown complete")


app = FastAPI(lifespan=lifespan)
app_socketio = socketio.ASGIApp(sio, app)

@app.get("/status")
async def status():
    return {"status": "running", "service": "N.E.N.O Backend"}

@sio.event
async def connect(sid, environ):
    try:
        ua = environ.get("HTTP_USER_AGENT", "") if isinstance(environ, dict) else ""
    except Exception:
        ua = ""
    print(f"Client connected: {sid} ua={ua[:80]!r}")
    await sio.emit('status', {'msg': 'Connected to N.E.N.O Backend'}, room=sid)

    global authenticator
    
    # Callback for Auth Status
    async def on_auth_status(is_auth):
        print(f"[SERVER] Auth status change: {is_auth}")
        await sio.emit('auth_status', {'authenticated': is_auth})

    # Callback for Auth Camera Frames
    async def on_auth_frame(frame_b64):
        await sio.emit('auth_frame', {'image': frame_b64})

    # Initialize Authenticator if not already done
    if authenticator is None:
        authenticator = FaceAuthenticator(
            reference_image_path="reference.jpg",
            on_status_change=on_auth_status,
            on_frame=on_auth_frame
        )
    
    # Check if already authenticated or needs to start
    if authenticator.authenticated:
        await sio.emit('auth_status', {'authenticated': True})
    else:
        # Check Settings for Auth
        if SETTINGS.get("face_auth_enabled", False):
            await sio.emit('auth_status', {'authenticated': False})
            # Start the auth loop in background
            asyncio.create_task(authenticator.start_authentication_loop())
        else:
            # Bypass Auth
            print("Face Auth Disabled. Auto-authenticating.")
            # We don't change authenticator state to true to avoid confusion if re-enabled? 
            # Or we should just tell client it's auth'd.
            await sio.emit('auth_status', {'authenticated': True})

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")

@sio.event
async def start_audio(sid, data=None):
    # Serializa el ciclo de vida del AudioLoop para evitar dos sesiones Live solapadas.
    async with _get_audio_lifecycle_lock():
        await _start_audio_locked(sid, data)


async def _start_audio_locked(sid, data=None):
    global audio_loop, loop_task

    # #region agent log
    try:
        neno._dlog(
            "server.py:start_audio",
            "start_audio invoked",
            {"sid": str(sid)[:16], "has_data": bool(data), "data_keys": list((data or {}).keys())},
            hypothesis="H5",
        )
    except Exception:
        pass
    # #endregion

    # Optional: Block if not authenticated
    # Only block if auth is ENABLED and not authenticated
    if SETTINGS.get("face_auth_enabled", False):
        if authenticator and not authenticator.authenticated:
            print("Blocked start_audio: Not authenticated.")
            await sio.emit('error', {'msg': 'Authentication Required'})
            return

    print("Starting Audio Loop...")
    
    device_index = None
    device_name = None
    if data:
        if 'device_index' in data:
            device_index = data['device_index']
        if 'device_name' in data:
            raw = data['device_name']
            if isinstance(raw, str):
                device_name = raw.strip() or None
            else:
                device_name = raw

    print(f"Using input device: Name='{device_name}', Index={device_index}")

    # Si hay un AudioLoop previo (vivo o ya finalizando), cerrarlo de forma síncrona
    # antes de crear el nuevo. Esto evita la condición de carrera por la que dos
    # sesiones Live coexistían y se solapaban (eco + ASR fragmentado).
    if audio_loop is not None or loop_task is not None:
        if loop_task and not (loop_task.done() or loop_task.cancelled()):
            print("Audio loop already alive; tearing it down before starting a new one.")
        await _shutdown_audio_loop("start_audio replaces previous loop")


    # Callback to send audio data to frontend
    def on_audio_data(data_bytes):
        # We need to schedule this on the event loop
        # This is high frequency, so we might want to downsample or batch if it's too much
        asyncio.create_task(sio.emit('audio_data', {'data': list(data_bytes)}))

    # Callback to send Browser data to frontend
    def on_web_data(data):
        print(f"Sending Browser data to frontend: {len(data.get('log', ''))} chars logs")
        asyncio.create_task(sio.emit('browser_frame', data))
        
    async def _emit_transcription(payload: dict):
        await sio.emit('transcription', payload)

    _tr_coalesce = _TranscriptionCoalescer(_emit_transcription)

    def on_transcription(data):
        # data = {"sender": "User"|"N.E.N.O", "text": "..."}
        _tr_coalesce.push(data)

    # Callback to send Confirmation Request to frontend
    def on_tool_confirmation(data):
        # data = {"id": "uuid", "tool": "tool_name", "args": {...}}
        print(f"Requesting confirmation for tool: {data.get('tool')}")
        asyncio.create_task(sio.emit('tool_confirmation_request', data))

    # Callback to send Project Update to frontend
    def on_project_update(project_name):
        print(f"Sending Project Update: {project_name}")
        asyncio.create_task(sio.emit('project_update', {'project': project_name}))

    # Callback to send Device Update to frontend
    def on_device_update(devices):
        # devices is a list of dicts
        print(f"Sending Kasa Device Update: {len(devices)} devices")
        asyncio.create_task(sio.emit('kasa_devices', devices))

    # Callback to send Error to frontend
    def on_error(msg):
        print(f"Sending Error to frontend: {msg}")
        asyncio.create_task(sio.emit('error', {'msg': msg}))

    # Pedir al frontend que abra la URL (p. ej. `mailto:`). Electron usa
    # `shell.openExternal`; el navegador en Android resuelve el intent en el móvil.
    def on_open_url(url):
        print(f"Requesting frontend to open external URL: {url}")
        asyncio.create_task(sio.emit('open_external_url', {'url': url}))

    # Initialize N.E.N.O
    try:
        print(f"Initializing AudioLoop with device_index={device_index}")
        audio_loop = neno.AudioLoop(
            video_mode="none",
            on_audio_data=on_audio_data,
            on_web_data=on_web_data,
            on_transcription=on_transcription,
            on_tool_confirmation=on_tool_confirmation,
            on_project_update=on_project_update,
            on_device_update=on_device_update,
            on_error=on_error,
            on_open_url=on_open_url,

            input_device_index=device_index,
            input_device_name=device_name,
            kasa_agent=kasa_agent,
            voice_name=SETTINGS.get("voice_name", "Charon"),
            response_language=SETTINGS.get(
                "response_language", neno.DEFAULT_RESPONSE_LANGUAGE
            ),
            open_document_settings={
                "limit_extensions": SETTINGS.get("open_document_limit_extensions", False),
                "allow_directories": SETTINGS.get("open_document_allow_directories", True),
                "allowed_extensions": SETTINGS.get("open_document_allowed_extensions", []),
            },
        )
        print("AudioLoop initialized successfully.")

        # Apply current permissions
        audio_loop.update_permissions(SETTINGS["tool_permissions"])
        
        # Check initial mute state
        if data and data.get('muted', False):
            print("Starting with Audio Paused")
            audio_loop.set_paused(True)

        print("Creating asyncio task for AudioLoop.run()")
        loop_task = asyncio.create_task(audio_loop.run())
        
        # Add a done callback to catch silent failures in the loop
        def handle_loop_exit(task):
            try:
                task.result()
            except asyncio.CancelledError:
                print("Audio Loop Cancelled")
            except Exception as e:
                print(f"Audio Loop Crashed: {e}")
                # You could emit 'error' here if you have context
        
        loop_task.add_done_callback(handle_loop_exit)
        
        print("Emitting 'N.E.N.O Started'")
        await sio.emit('status', {'msg': 'N.E.N.O Started'})

    except Exception as e:
        print(f"CRITICAL ERROR STARTING N.E.N.O: {e}")
        import traceback
        traceback.print_exc()
        await sio.emit('error', {'msg': f"Failed to start: {str(e)}"})
        audio_loop = None # Ensure we can try again


@sio.event
async def stop_audio(sid):
    # Serializa con start_audio: bloquea hasta cerrar streams y sesión Live.
    async with _get_audio_lifecycle_lock():
        if audio_loop is None and loop_task is None:
            return
        await _shutdown_audio_loop("stop_audio event from frontend")
        await sio.emit('status', {'msg': 'N.E.N.O Stopped'})

@sio.event
async def pause_audio(sid):
    global audio_loop
    if audio_loop:
        audio_loop.set_paused(True)
        print("Pausing Audio")
        await sio.emit('status', {'msg': 'Audio Paused'})

@sio.event
async def resume_audio(sid):
    global audio_loop
    if audio_loop:
        audio_loop.set_paused(False)
        print("Resuming Audio")
        await sio.emit('status', {'msg': 'Audio Resumed'})

@sio.event
async def confirm_tool(sid, data):
    # data: { "id": "...", "confirmed": True/False }
    request_id = data.get('id')
    confirmed = data.get('confirmed', False)
    
    log.debug("Received confirmation response for %s: %s", request_id, confirmed)

    if audio_loop:
        audio_loop.resolve_tool_confirmation(request_id, confirmed)
    else:
        log.warning("Audio loop not active, cannot resolve confirmation.")

@sio.event
async def shutdown(sid, data=None):
    """Gracefully shutdown the server when the application closes."""
    global audio_loop, loop_task, authenticator
    
    print("[SERVER] ========================================")
    print("[SERVER] SHUTDOWN SIGNAL RECEIVED FROM FRONTEND")
    print("[SERVER] ========================================")
    
    # Stop audio loop
    if audio_loop:
        print("[SERVER] Stopping Audio Loop...")
        audio_loop.stop()
        audio_loop = None
    
    # Cancel the loop task if running
    if loop_task and not loop_task.done():
        print("[SERVER] Cancelling loop task...")
        loop_task.cancel()
        loop_task = None
    
    # Stop authenticator if running
    if authenticator:
        print("[SERVER] Stopping Authenticator...")
        authenticator.stop()
    
    print("[SERVER] Graceful shutdown complete. Terminating process...")
    
    # Force exit immediately - os._exit bypasses cleanup but ensures termination
    os._exit(0)

async def _ensure_audio_loop_ready(sid: str, timeout: float = 8.0) -> bool:
    """Garantiza que `audio_loop` y su sesión Live están listos.

    Si no hay loop, lo arranca con la configuración por defecto (sin device_name)
    para que la entrada por teclado funcione aunque el cliente no haya emitido
    `start_audio` (p. ej. permiso de micrófono aún no concedido).
    """
    global audio_loop
    if audio_loop is None:
        log.debug("Audio loop missing — auto-starting for text input.")
        await start_audio(sid, {"muted": True})

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if audio_loop and audio_loop.session is not None:
            return True
        await asyncio.sleep(0.1)
    return audio_loop is not None and audio_loop.session is not None


@sio.event
async def user_input(sid, data):
    text = data.get('text')
    log.debug("User input received: %r", text)

    ready = await _ensure_audio_loop_ready(sid)
    if not ready:
        log.error("Could not initialise audio loop / Live session for text input.")
        await sio.emit('error', {'msg': 'No se pudo iniciar el modelo para responder al mensaje.'})
        return

    if text:
        log.debug("Sending message to model: %r", text)

        # Log User Input to Project History
        if audio_loop and audio_loop.project_manager:
            audio_loop.project_manager.log_chat("User", text)

        # Modern SDK: use send_realtime_input for media (Blob) and send_client_content for text turns.
        # The legacy session.send(...) silently dropped audio; payloads now contain raw bytes,
        # which JSON-serialisation in the legacy path cannot handle either.
        if audio_loop and audio_loop._latest_image_payload:
            log.debug("Piggybacking video frame with text input.")
            try:
                pl = audio_loop._latest_image_payload
                await audio_loop.session.send_realtime_input(
                    media=genai_types.Blob(data=pl["data"], mime_type=pl["mime_type"])
                )
            except Exception as e:
                log.warning("Failed to send piggyback frame: %s", e)

        await audio_loop.session.send_client_content(
            turns=[{"role": "user", "parts": [{"text": text}]}],
            turn_complete=True,
        )
        log.debug("Message sent to model successfully.")

import json
from datetime import datetime
from pathlib import Path

# ... (imports)

@sio.event
async def video_frame(sid, data):
    # data should contain 'image' which is binary (blob) or base64 encoded
    image_data = data.get('image')
    if image_data and audio_loop:
        # We don't await this because we don't want to block the socket handler
        # But send_frame is async, so we create a task
        asyncio.create_task(audio_loop.send_frame(image_data))

@sio.event
async def save_memory(sid, data):
    try:
        messages = data.get('messages', [])
        if not messages:
            print("No messages to save.")
            return

        # Ensure directory exists
        memory_dir = Path("long_term_memory")
        memory_dir.mkdir(exist_ok=True)

        # Generate filename
        # Use provided filename if available, else timestamp
        provided_name = data.get('filename')
        
        if provided_name:
            # Simple sanitization
            if not provided_name.endswith('.txt'):
                provided_name += '.txt'
            # Prevent directory traversal
            filename = memory_dir / Path(provided_name).name 
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = memory_dir / f"memory_{timestamp}.txt"

        # Write to file
        with open(filename, 'w', encoding='utf-8') as f:
            for msg in messages:
                sender = msg.get('sender', 'Unknown')
                text = msg.get('text', '')
        print(f"Conversation saved to {filename}")
        await sio.emit('status', {'msg': 'Memory Saved Successfully'})

    except Exception as e:
        print(f"Error saving memory: {e}")
        await sio.emit('error', {'msg': f"Failed to save memory: {str(e)}"})

@sio.event
async def upload_memory(sid, data):
    print(f"Received memory upload request")
    try:
        memory_text = data.get('memory', '')
        if not memory_text:
            print("No memory data provided.")
            return

        if not audio_loop:
            log.error("Audio loop is None. Cannot load memory.")
            await sio.emit('error', {'msg': "System not ready (Audio Loop inactive)"})
            return

        if not audio_loop.session:
            log.error("Session is None. Cannot load memory.")
            await sio.emit('error', {'msg': "System not ready (No active session)"})
            return

        # Send to model
        print("Sending memory context to model...")
        context_msg = f"System Notification: The user has uploaded a long-term memory file. Please load the following context into your understanding. The format is a text log of previous conversations:\n\n{memory_text}"
        
        await audio_loop.session.send_client_content(
            turns=[{"role": "user", "parts": [{"text": context_msg}]}],
            turn_complete=True,
        )
        print("Memory context sent successfully.")
        await sio.emit('status', {'msg': 'Memory Loaded into Context'})

    except Exception as e:
        print(f"Error uploading memory: {e}")
        await sio.emit('error', {'msg': f"Failed to upload memory: {str(e)}"})

@sio.event
async def discover_kasa(sid):
    print(f"Received discover_kasa request")
    try:
        devices = await kasa_agent.discover_devices()
        await sio.emit('kasa_devices', devices)
        await sio.emit('status', {'msg': f"Found {len(devices)} Kasa devices"})
        
        # Save to settings
        # devices is a list of full device info dicts. minimizing for storage.
        saved_devices = []
        for d in devices:
            saved_devices.append({
                "ip": d["ip"],
                "alias": d["alias"],
                "model": d["model"]
            })
        
        # Merge with existing to preserve any manual overrides? 
        # For now, just overwrite with latest scan result + previously known if we want to be fancy,
        # but user asked for "Any new devices that are scanned are added there".
        # A simple full persistence of current state is safest.
        SETTINGS["kasa_devices"] = saved_devices
        save_settings()
        print(f"[SERVER] Saved {len(saved_devices)} Kasa devices to settings.")
        
    except Exception as e:
        print(f"Error discovering kasa: {e}")
        await sio.emit('error', {'msg': f"Kasa Discovery Failed: {str(e)}"})

@sio.event
async def prompt_web_agent(sid, data):
    # data: { prompt: "find xyz" }
    prompt = data.get('prompt')
    print(f"Received web agent prompt: '{prompt}'")
    
    if not audio_loop or not audio_loop.web_agent:
        await sio.emit('error', {'msg': "Web Agent not available"})
        return

    try:
        await sio.emit('status', {'msg': 'Web Agent running...'})
        
        # We assume web_agent has a run method or similar.
        # This might block the loop if not strictly async or offloaded.
        # Ideally web_agent.run is async.
        # And it should emit 'browser_snap' and logs automatically via hooks if setup.
        
        # We might need to launch this as a task if it's long running?
        # asyncio.create_task(audio_loop.web_agent.run(prompt))
        # But we want to catch errors here.
        
        # Based on typical agent design, run() is the entry point.
        await audio_loop.web_agent.run(prompt)
        
        await sio.emit('status', {'msg': 'Web Agent finished'})
        
    except Exception as e:
        print(f"Error running Web Agent: {e}")
        await sio.emit('error', {'msg': f"Web Agent Error: {str(e)}"})

@sio.event
async def control_kasa(sid, data):
    # data: { ip, action: "on"|"off"|"brightness"|"color", value: ... }
    ip = data.get('ip')
    action = data.get('action')
    print(f"Kasa Control: {ip} -> {action}")
    
    try:
        success = False
        if action == "on":
            success = await kasa_agent.turn_on(ip)
        elif action == "off":
            success = await kasa_agent.turn_off(ip)
        elif action == "brightness":
            val = data.get('value')
            success = await kasa_agent.set_brightness(ip, val)
        elif action == "color":
            # value is {h, s, v} - convert to tuple for set_color
            h = data.get('value', {}).get('h', 0)
            s = data.get('value', {}).get('s', 100)
            v = data.get('value', {}).get('v', 100)
            success = await kasa_agent.set_color(ip, (h, s, v))
        
        if success:
            await sio.emit('kasa_update', {
                'ip': ip,
                'is_on': True if action == "on" else (False if action == "off" else None),
                'brightness': data.get('value') if action == "brightness" else None,
            })
 
        else:
             await sio.emit('error', {'msg': f"Failed to control device {ip}"})

    except Exception as e:
         print(f"Error controlling kasa: {e}")
         await sio.emit('error', {'msg': f"Kasa Control Error: {str(e)}"})

@sio.event
async def get_settings(sid):
    await sio.emit('settings', SETTINGS)

@sio.event
async def update_settings(sid, data):
    # `audio_loop` y `loop_task` se reasignan más abajo (cambio de voz),
    # así que deben declararse como globales o Python los trata como locales
    # en TODA la función (UnboundLocalError al leerlos antes de la asignación).
    global audio_loop, loop_task
    # Generic update
    print(f"Updating settings: {data}")
    
    # Handle specific keys if needed
    if "tool_permissions" in data:
        SETTINGS["tool_permissions"].update(data["tool_permissions"])
        if audio_loop:
            audio_loop.update_permissions(SETTINGS["tool_permissions"])
            
    if "face_auth_enabled" in data:
        SETTINGS["face_auth_enabled"] = data["face_auth_enabled"]
        # If turned OFF, maybe emit auth status true?
        if not data["face_auth_enabled"]:
             await sio.emit('auth_status', {'authenticated': True})
             # Stop auth loop if running?
             if authenticator:
                 authenticator.stop() 

    if "open_document_limit_extensions" in data:
        SETTINGS["open_document_limit_extensions"] = bool(data["open_document_limit_extensions"])
    if "open_document_allow_directories" in data:
        SETTINGS["open_document_allow_directories"] = bool(data["open_document_allow_directories"])
    if "open_document_allowed_extensions" in data:
        raw_ext = data["open_document_allowed_extensions"]
        if isinstance(raw_ext, list):
            SETTINGS["open_document_allowed_extensions"] = neno.normalize_open_document_extensions(raw_ext)

    if audio_loop and (
        "open_document_limit_extensions" in data
        or "open_document_allow_directories" in data
        or "open_document_allowed_extensions" in data
    ):
        audio_loop.update_open_document_settings({
            "limit_extensions": SETTINGS.get("open_document_limit_extensions", False),
            "allow_directories": SETTINGS.get("open_document_allow_directories", True),
            "allowed_extensions": SETTINGS.get("open_document_allowed_extensions", []),
        })

    if "theme" in data:
        new_theme = data["theme"]
        if isinstance(new_theme, str) and new_theme in AVAILABLE_THEMES:
            SETTINGS["theme"] = new_theme
            print(f"[SERVER] Theme set to: {new_theme}")

    restart_audio_for_live_config = False
    if "voice_name" in data:
        new_voice = data["voice_name"]
        if isinstance(new_voice, str) and new_voice in neno.AVAILABLE_VOICES:
            SETTINGS["voice_name"] = new_voice
            print(f"[SERVER] Voice set to: {new_voice}")
            if audio_loop is not None:
                audio_loop.voice_name = new_voice
                restart_audio_for_live_config = True

    if "response_language" in data:
        new_lang = data["response_language"]
        if isinstance(new_lang, str) and new_lang in neno.AVAILABLE_RESPONSE_LANGUAGES:
            SETTINGS["response_language"] = new_lang
            print(f"[SERVER] Response language set to: {new_lang}")
            if audio_loop is not None:
                audio_loop.response_language = new_lang
                restart_audio_for_live_config = True

    if restart_audio_for_live_config and audio_loop is not None:
        prev_dev_name = audio_loop.input_device_name
        prev_dev_idx = audio_loop.input_device_index
        prev_paused = audio_loop.paused
        # `start_audio` ya adquiere el lock y derriba la sesión previa de forma síncrona.
        await start_audio(sid, {
            "device_index": prev_dev_idx,
            "device_name": prev_dev_name,
            "muted": prev_paused,
        })

    save_settings()
    # Broadcast new full settings
    await sio.emit('settings', SETTINGS)


# Deprecated/Mapped for compatibility if frontend still uses specific events
@sio.event
async def get_tool_permissions(sid):
    await sio.emit('tool_permissions', SETTINGS["tool_permissions"])

@sio.event
async def update_tool_permissions(sid, data):
    print(f"Updating permissions (legacy event): {data}")
    SETTINGS["tool_permissions"].update(data)
    save_settings()
    
    if audio_loop:
        audio_loop.update_permissions(SETTINGS["tool_permissions"])
    # Broadcast update to all
    await sio.emit('tool_permissions', SETTINGS["tool_permissions"])

if __name__ == "__main__":
    # Por defecto solo loopback. Para acceso desde otro dispositivo en la misma red:
    #   NENO_BIND_HOST=0.0.0.0 python backend/server.py
    _bind_host = os.getenv("NENO_BIND_HOST", "127.0.0.1")
    _bind_port = int(os.getenv("NENO_BIND_PORT", "8000"))
    uvicorn.run(
        "server:app_socketio",
        host=_bind_host,
        port=_bind_port,
        reload=False,  # Reload habilitaría un worker distinto; no mezclar con asyncio en Windows
        loop="asyncio",
    )

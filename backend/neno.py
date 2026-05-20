import asyncio
import base64
import contextlib
import io
import os
import sys
import traceback
import time as _early_time
import json as _early_json

# #region agent log (very early bootstrap so we know the module started loading)
# Eco a stdout solo si NENO_LOG_LEVEL=DEBUG. La escritura al fichero NDJSON
# es siempre activa para diagnosticar caídas de import.
_EARLY_DEBUG_TO_STDOUT = os.getenv("NENO_LOG_LEVEL", "INFO").upper() == "DEBUG"


def _early_dlog(loc, msg, data=None):
    try:
        backend_dir = os.path.dirname(os.path.abspath(__file__))
        for path in (
            os.path.join(backend_dir, "debug-e3fca2.ndjson"),
            os.path.join(backend_dir, "..", ".cursor", "debug-e3fca2.log"),
        ):
            try:
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(_early_json.dumps({
                        "sessionId": "e3fca2",
                        "id": f"log_{int(_early_time.time()*1000)}_{loc.replace(':','_')}",
                        "timestamp": int(_early_time.time() * 1000),
                        "location": loc,
                        "message": msg,
                        "data": data or {},
                        "runId": "initial",
                        "hypothesisId": "bootstrap_early",
                    }, default=str) + "\n")
            except Exception as e:
                if _EARLY_DEBUG_TO_STDOUT:
                    print(f"[N.E.N.O DEBUG][_early_dlog] write failed at {path}: {e}", flush=True)
        if _EARLY_DEBUG_TO_STDOUT:
            print(f"[N.E.N.O DEBUG][_early_dlog] {loc} :: {msg}", flush=True)
    except Exception as e:
        # Si el helper falla por completo se imprime siempre.
        print(f"[N.E.N.O ERROR][_early_dlog] FATAL: {e}", flush=True)

_early_dlog("neno.py:line1", "module import started", {"pid": os.getpid(), "cwd": os.getcwd()})
# #endregion

from dotenv import load_dotenv
import cv2

# Linux ALSA: reduce chatter when PortAudio probes devices (harmless on headless/mic conflicts).
os.environ.setdefault("ALSA_LOG_LEVEL", "0")

import pyaudio
import PIL.Image
import mss
import argparse
import math
import re
import struct
import time
import urllib.parse
import webbrowser

from google import genai
from google.genai import types

if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

import logging

# Logger del núcleo del asistente. `server.py` suele configurar el root antes de
# `import neno`; en ese caso NO debemos llamar a basicConfig otra vez (duplica
# handlers en algunos entornos). Solo inicializamos si el root aún no tiene handlers.
_LOG_LEVEL = os.getenv("NENO_LOG_LEVEL", "INFO").upper()
_log_level_num = getattr(logging, _LOG_LEVEL, logging.INFO)
if not logging.root.handlers:
    logging.basicConfig(
        level=_log_level_num,
        format="[%(levelname)s] %(name)s: %(message)s",
    )
else:
    # Root ya configurado (p. ej. por server.py): solo alinea el nivel del logger propio.
    pass

log = logging.getLogger("neno.core")
log.setLevel(_log_level_num)

from tools import tools_list

# #region agent log
import json as _dbg_json
_DBG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cursor")
_DBG_LOG_PATH = os.path.join(_DBG_DIR, "debug-e3fca2.log")
# Siempre escribible junto a este archivo (evita fallos al crear .cursor)
_DBG_BACKEND_FALLBACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug-e3fca2.ndjson")

def _dlog(location, message, data=None, hypothesis="", run_id="initial"):
    payload = {
        "sessionId": "e3fca2",
        "id": f"log_{int(time.time()*1000)}_{location.replace(':','_')}",
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "data": data or {},
        "runId": run_id,
        "hypothesisId": hypothesis,
    }
    line = _dbg_json.dumps(payload, default=str) + "\n"
    for path in (_DBG_BACKEND_FALLBACK, _DBG_LOG_PATH):
        try:
            if path == _DBG_LOG_PATH:
                os.makedirs(_DBG_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as _f:
                _f.write(line)
        except Exception:
            pass
# #endregion


@contextlib.contextmanager
def _suppress_stderr():
    """Hide ALSA/PortAudio probe noise (failed dsnoop/default PCM) written from C code to stderr."""
    try:
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, OSError):
        yield
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        old = os.dup(stderr_fd)
        try:
            os.dup2(devnull, stderr_fd)
            yield
        finally:
            os.dup2(old, stderr_fd)
            os.close(old)
    finally:
        os.close(devnull)


FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000


def _env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    """Lee un entero del entorno con clamping silencioso (mantiene el default si es inválido)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if v < lo or v > hi:
        return default
    return v


# Tamaño del buffer de captura. A 16 kHz Int16: 1024 ≈ 64 ms; 512 ≈ 32 ms; 256 ≈ 16 ms.
# Trozos más pequeños ⇒ menos latencia entre que se deja de hablar y el último
# chunk llega al modelo, pero más overhead de I/O. 512 es un buen compromiso.
# Se puede sobrescribir con `NENO_MIC_CHUNK_SIZE`.
CHUNK_SIZE = _env_int("NENO_MIC_CHUNK_SIZE", 512, lo=128, hi=4096)

# VAD del servidor (Gemini Live). `silence_duration_ms` es el principal factor
# de retardo entre "termino de hablar" y "el modelo empieza a contestar".
# - 250 ms: muy reactivo, riesgo de cortar pausas naturales.
# - 350 ms: equilibrado y natural (default).
# - 500+ ms: conservador, evita cortes pero se siente lento.
NENO_VAD_SILENCE_MS = _env_int("NENO_VAD_SILENCE_MS", 350, lo=120, hi=2000)
NENO_VAD_PREFIX_MS = _env_int("NENO_VAD_PREFIX_MS", 200, lo=50, hi=1000)

MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
# "none" desactiva la captura de cámara local del backend. El frontend ya envía
# frames vía `send_frame()` y los inserta en el flujo cuando el VAD detecta voz,
# por lo que abrir aquí `cv2.VideoCapture` además de ser redundante en Linux usa
# constantes de macOS (CAP_AVFOUNDATION) y puede colgar el TaskGroup.
DEFAULT_MODE = "none"

load_dotenv()
client = genai.Client(http_options={"api_version": "v1beta"}, api_key=os.getenv("GEMINI_API_KEY"))

# Function definitions
run_web_agent = {
    "name": "run_web_agent",
    "description": "Opens a web browser and performs a task according to the prompt.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING", "description": "The detailed instructions for the web browser agent."}
        },
        "required": ["prompt"]
    },
    "behavior": "NON_BLOCKING"
}

create_project_tool = {
    "name": "create_project",
    "description": "Creates a new project folder to organize files.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "The name of the new project."}
        },
        "required": ["name"]
    }
}

switch_project_tool = {
    "name": "switch_project",
    "description": "Switches the current active project context.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "The name of the project to switch to."}
        },
        "required": ["name"]
    }
}

list_projects_tool = {
    "name": "list_projects",
    "description": "Lists all available projects.",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
}

list_smart_devices_tool = {
    "name": "list_smart_devices",
    "description": "Lists all available smart home devices (lights, plugs, etc.) on the network.",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
}

control_light_tool = {
    "name": "control_light",
    "description": "Controls a smart light device.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "target": {
                "type": "STRING",
                "description": "The IP address of the device to control. Always prefer the IP address over the alias for reliability."
            },
            "action": {
                "type": "STRING",
                "description": "The action to perform: 'turn_on', 'turn_off', or 'set'."
            },
            "brightness": {
                "type": "INTEGER",
                "description": "Optional brightness level (0-100)."
            },
            "color": {
                "type": "STRING",
                "description": "Optional color name (e.g., 'red', 'cool white') or 'warm'."
            }
        },
        "required": ["target", "action"]
    }
}

open_email_client_tool = {
    "name": "open_email_client",
    "description": (
        "Opens the system's default email client (Thunderbird, Outlook, Mail.app, "
        "Gmail web, etc.). Use it when the user wants to send, write, draft or "
        "compose an email. All fields are optional: if none are provided it just "
        "opens the email manager with a blank compose window."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "to": {"type": "STRING", "description": "Recipient email address (optional)."},
            "subject": {"type": "STRING", "description": "Email subject (optional)."},
            "body": {"type": "STRING", "description": "Email body text (optional)."},
            "cc": {"type": "STRING", "description": "CC recipients, comma-separated (optional)."},
            "bcc": {"type": "STRING", "description": "BCC recipients, comma-separated (optional)."}
        }
    }
}

tools = [{'google_search': {}}, {"function_declarations": [run_web_agent, create_project_tool, switch_project_tool, list_projects_tool, list_smart_devices_tool, control_light_tool, open_email_client_tool] + tools_list[0]['function_declarations']}]

# Voces disponibles en Gemini Live (Native Audio). Charon es masculina y es el por defecto;
# Fenrir/Puck/Orus también suenan masculinas. Kore/Aoede/Leda/Zephyr son femeninas.
AVAILABLE_VOICES = ("Charon", "Fenrir", "Puck", "Orus", "Kore", "Aoede", "Leda", "Zephyr")
DEFAULT_VOICE = "Charon"

# Idioma de las respuestas del modelo Live (voz y transcripciones).
AVAILABLE_RESPONSE_LANGUAGES = (
    "es_es",
    "es_419",
    "en",
    "fr",
    "de",
    "pt",
    "it",
    "ca",
    "gl",
)
DEFAULT_RESPONSE_LANGUAGE = "es_es"

from pathlib import Path

# Reglas por idioma (no dependen de persona.md).
_LANG_BASE: dict[str, str] = {
    "es_es": (
        "Tu nombre es N.E.N.O y eres un asistente o asistenta según tu voz sea masculina o femenina. "
        "El usuario habla en español; el reconocimiento de voz puede devolver basura u otros alfabetos: "
        "no la cites ni bromees como si fuera lo que dijo; pide en castellano que repita con claridad si no entiendes. "
        "RESPONDES SIEMPRE EN ESPAÑOL DE ESPAÑA (castellano peninsular): "
        "vocabulario y expresiones propias de España, jamás uses latinoamericanismos ni voseo, "
        "no digas «celular», «manejar», «computadora», «papas», di «móvil», «conducir», «ordenador», «patatas». "
        "Aunque te hablen en otro idioma, contesta en castellano de España salvo que pidan explícitamente otro. "
        "Frases completas y claras, ritmo ágil. Personalidad sarcástica, ingeniosa y cercana. "
        "Tu creador es entreunosyceros; le llamas «Señor» como muestra de cortesía. "
    "Cuando el usuario pida «abrir el correo», «escribir un email», «enviar un mail», "
    "«mandar un correo» o frases similares, llama a la herramienta `open_email_client` "
    "(rellena `to`/`subject`/`body` si los menciona; si no, ábrelo en blanco). "
    "Cuando pida abrir un documento, PDF, hoja de cálculo, imagen u otro archivo con el "
    "programa del sistema (no solo leer el texto en el chat), usa `open_document` con la ruta."
),
    "es_419": (
        "Tu nombre es N.E.N.O y eres un asistente o asistenta según tu voz sea masculina o femenina. "
        "El usuario habla en español; el reconocimiento de voz puede devolver basura u otros alfabetos: "
        "no la cites ni bromees como si fuera lo que dijo; pide que repita con claridad si no entiendes. "
        "RESPONDES SIEMPRE EN ESPAÑOL LATINOAMERICANO NEUTRO: natural para la región, sin pretender ser de España. "
        "Aunque te hablen en otro idioma, contesta en este español salvo que pidan explícitamente otro. "
        "Frases completas y claras, ritmo ágil. Personalidad sarcástica, ingeniosa y cercana. "
        "Tu creador es entreunosyceros; le llamas «Señor» o «Jefe» según suene natural en el contexto. "
        "Si pide abrir el correo, redactar o enviar un email, llama a `open_email_client` "
        "(rellena `to`/`subject`/`body` si los menciona; si no, ábrelo en blanco)."
    ),
    "en": (
        "Your name is N.E.N.O; you are an assistant—masculine or feminine to match your voice. "
        "The user mainly speaks English; speech recognition may return garbage or wrong scripts: "
        "do not quote it or joke as if it were their words; ask them to repeat clearly if unsure. "
        "ALWAYS RESPOND IN ENGLISH: clear, natural, internationally understandable wording. "
        "If they speak another language, still answer in English unless they explicitly ask for another language. "
        "Full sentences, brisk pace. Witty, sarcastic, personable tone. "
        "Your creator is entreunosyceros; address them as «Sir». "
        "When they want to open email, compose or send mail, call `open_email_client` "
        "(fill `to`/`subject`/`body` if given; otherwise open blank)."
    ),
    "fr": (
        "Tu t'appelles N.E.N.O ; tu es un assistant ou une assistante selon que ta voix soit masculine ou féminine. "
        "L'utilisateur parle surtout français ; la reconnaissance vocale peut renvoyer du bruit : ne le cite pas comme s'il s'agissait de ses mots ; demande de répéter clairement si tu ne comprends pas. "
        "TU RÉPONDS TOUJOURS EN FRANÇAIS : français standard, clair et naturel. "
        "Même si on te parle dans une autre langue, réponds en français sauf demande explicite d'une autre langue. "
        "Phrases complètes, rythme vif. Ton sarcastique, vif et chaleureux. "
        "Ton créateur est entreunosyceros ; tu l'appelles « Monsieur ». "
        "Pour ouvrir le courriel, rédiger ou envoyer un mail, appelle `open_email_client` "
        "(remplis `to`/`subject`/`body` s'ils sont mentionnés ; sinon ouvre vide)."
    ),
    "de": (
        "Du heißt N.E.N.O und bist ein Assistent oder eine Assistentin – je nachdem, ob deine Stimme männlich oder weiblich klingt. "
        "Der Nutzer spricht überwiegend Deutsch; die Spracherkennung kann Müll liefern: zitiere das nicht wörtlich; bitte bei Unklarheit um klare Wiederholung. "
        "DU ANTWORTEST IMMER AUF DEUTSCH: klares, natürliches Standarddeutsch. "
        "Wird eine andere Sprache gesprochen, antworte dennoch auf Deutsch, außer es wird ausdrücklich etwas anderes verlangt. "
        "Vollständige Sätze, zügig. Witzig, sarkastisch, nahbar. "
        "Dein Schöpfer ist entreunosyceros; du sprichst ihn mit «Herr» an. "
        "Bei Wünschen nach E-Mail öffnen, verfassen oder senden: `open_email_client` aufrufen "
        "(`to`/`subject`/`body` ausfüllen falls genannt; sonst leer öffnen)."
    ),
    "pt": (
        "O teu nome é N.E.N.O e és assistente — masculino ou feminino conforme a voz. "
        "O utilizador fala sobretudo português; o reconhecimento de voz pode devolver lixo: não cites isso como se fossem as palavras dele; pede para repetir com clareza se não perceberes. "
        "RESPONDES SEMPRE EM PORTUGUÊS: português europeu claro e natural (ajusta ligeiramente se o utilizador claramente falar português do Brasil). "
        "Mesmo que falem outra língua, responde em português salvo pedido explícito de outra. "
        "Frases completas, ritmo ágil. Personalidade sarcástica, espirituosa e próxima. "
        "O teu criador é entreunosyceros; chamas-lhe «Senhor». "
        "Para abrir o correio, escrever ou enviar email, chama `open_email_client` "
        "(preenche `to`/`subject`/`body` se mencionarem; senão abre em branco)."
    ),
    "it": (
        "Il tuo nome è N.E.N.O; sei un assistente — maschile o femminile in base alla voce. "
        "L'utente parla soprattutto italiano; il riconoscimento vocale può dare testo spurio: non citarlo come se fossero le sue parole; chiedi di ripetere chiaramente se non capisci. "
        "RISPONDI SEMPRE IN ITALIANO: italiano standard, chiaro e naturale. "
        "Anche se ti parlano in un'altra lingua, rispondi in italiano salvo richiesta esplicita diversa. "
        "Frasi complete, ritmo svelto. Ironico, sarcastico, cordiale. "
        "Il tuo creatore è entreunosyceros; lo chiami «Signore». "
        "Per aprire la posta, scrivere o inviare email, chiama `open_email_client` "
        "(compila `to`/`subject`/`body` se li dice; altrimenti apri vuoto)."
    ),
    "ca": (
        "El teu nom és N.E.N.O i ets assistent o assistenta segons la veu sigui masculina o femenina. "
        "L'usuari parla català; el reconeixement de veu pot retornar soroll: no ho citis com si fos el que ha dit; demana que repeteixi amb claredat si no ho entens. "
        "RESPONS SEMPRE EN CATALÀ: català estàndard clar i natural. "
        "Encara que et parlin en un altre idioma, respon en català llevat que demanin explícitament un altre. "
        "Frases completes i clares, ritme àgil. Personalitat sarcàstica, enginyosa i propera. "
        "El teu creador és entreunosyceros; li dius «Senyor». "
        "Si demana obrir el correu, escriure o enviar un correu, crida `open_email_client` "
        "(omple `to`/`subject`/`body` si ho diu; si no, obre en blanc)."
    ),
    "gl": (
        "O teu nome é N.E.N.O e es asistente ou asistenta segundo a voz sexa masculina ou feminina. "
        "O usuario fala principalmente galego (ou castelán); o recoñecemento de voz pode devolver lixo: "
        "non o cites como se for o que dixo; pide que repita con claridade se non entendes. "
        "RESPONDES SEMPRE EN GALEGO: galego estándar claro e natural, respectando a normativa usual. "
        "Aínda que che falen noutra lingua, responde en galego salvo que pidan explicitamente outra. "
        "Frases completas e claras, ritmo áxil. Personalidade sarcástica, enxeñosa e achegada. "
        "O teu creador é entreunosyceros; chámaslle «Señor» por cortesía. "
        "Cando pida abrir o correo, redactar ou enviar un correo, chama a `open_email_client` "
        "(enche `to`/`subject`/`body` se os menciona; se non, ábreo en branco)."
    ),
}

_PERSONA_FILE = Path(__file__).parent / "persona.md"
try:
    _persona_text = _PERSONA_FILE.read_text(encoding="utf-8").strip()
except FileNotFoundError:
    _persona_text = ""
except OSError as e:
    log.warning("No se pudo leer %s: %s", _PERSONA_FILE.name, e)
    _persona_text = ""


def build_system_instruction(response_language: str | None = None) -> str:
    """Instrución de sistema completa (idioma de resposta + persona opcional)."""
    lang = (
        response_language
        if response_language in AVAILABLE_RESPONSE_LANGUAGES
        else DEFAULT_RESPONSE_LANGUAGE
    )
    base = _LANG_BASE[lang]
    if _persona_text:
        return (
            f"{base}\n\n"
            "# Trasfondo del personaje\n"
            "Lo siguiente es tu historia y personalidad. Mantenla coherente en todo "
            "momento, pero no la recites literalmente salvo que te pregunten:\n\n"
            f"{_persona_text}"
        )
    return base


def build_live_config(
    voice_name: str | None = None,
    response_language: str | None = None,
) -> types.LiveConnectConfig:
    """Construye la `LiveConnectConfig` con la voz y el idioma de respuesta indicados."""
    chosen = voice_name if voice_name in AVAILABLE_VOICES else DEFAULT_VOICE
    instruction = build_system_instruction(response_language)
    realtime_cfg = types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False,
            # `prefix_padding_ms`: cuánto audio anterior al inicio de voz se incluye
            # en el turno (afecta sobre todo a no perder la primera consonante).
            # `silence_duration_ms`: tiempo de silencio que el servidor exige para
            # cerrar tu turno y empezar a responder. Es el principal mando para
            # reducir el lag de "termino de hablar → empieza a responder".
            prefix_padding_ms=NENO_VAD_PREFIX_MS,
            silence_duration_ms=NENO_VAD_SILENCE_MS,
        ),
    )
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        system_instruction=instruction,
        tools=tools,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=chosen)
            )
        ),
        realtime_input_config=realtime_cfg,
    )


# Compatibilidad hacia atrás: algunos puntos del código y tests podían referenciar `config`.
config = build_live_config(DEFAULT_VOICE, DEFAULT_RESPONSE_LANGUAGE)

with _suppress_stderr():
    pya = pyaudio.PyAudio()

# #region agent log
_dlog("neno.py:bootstrap", "module loaded post-PyAudio", {"pid": os.getpid()}, hypothesis="bootstrap")
# #endregion

from web_agent import WebAgent
from kasa_agent import KasaAgent

def _mic_label_tokens(label: str) -> set:
    """Tokens from a browser MediaDevice label for fuzzy matching to ALSA/Pulse names."""
    if not label:
        return set()
    parts = re.findall(r"[A-Za-zÀ-ÿ0-9]+", label.lower())
    return {p for p in parts if len(p) >= 3}


_HW_HINT_RE = re.compile(r"\(?hw:\d+,\d+\)?", re.IGNORECASE)
# Nombres "virtuales" del lado de PyAudio que enrutan a través del sound server (Pulse/PipeWire),
# permitiendo que varios procesos compartan el mismo micrófono físico.
_VIRTUAL_PCM_NAMES = ("pipewire", "pulse", "default", "sysdefault")


def _pyaudio_input_name_is_virtual_only(name: str) -> bool:
    """True si el nombre PortAudio es solo el mux genérico (no una tarjeta ALSA concreta)."""
    n = (name or "").strip().lower()
    return n in _VIRTUAL_PCM_NAMES


def _label_suggests_specific_physical_input(label: str) -> bool:
    """True si la etiqueta del navegador describe hardware distinto del mic «genérico»."""
    if not label or not str(label).strip():
        return False
    low = str(label).lower()
    hints = (
        "usb", "hdmi", "bluetooth", "yeti", "rode", "shure", "hyperx", "headset",
        "webcam", "line in", "xlr", "irig", "snowball", "elgato", "focusrite",
        "behringer", "steinberg", "audient", "presonus", "iec958", "rear mic",
        "front mic", "external", "analog stereo", "digital stereo", "uac2",
    )
    return any(h in low for h in hints)


def _is_hw_pcm_name(name: str) -> bool:
    return bool(_HW_HINT_RE.search(name or ""))


def _score_mic_label_against_pyaudio_name(label: str, pya_name: str) -> int:
    if not label or not pya_name:
        return 0
    pn = pya_name.lower()
    score = 0
    pn_strip = pn.strip()
    for t in _mic_label_tokens(label):
        if t in pn:
            # Evita que la etiqueta «Default» del navegador coincida con la subcadena
            # "default" dentro de "sysdefault" y robe la puntuación al mux virtual.
            if t == "default" and pn_strip == "sysdefault":
                continue
            score += 3
    compact = re.sub(r"\s+", " ", label.strip().lower())
    if len(compact) > 4 and compact in pn:
        # "default" (etiqueta del navegador) no debe puntuar dentro de "sysdefault".
        if not (compact == "default" and pn_strip == "sysdefault"):
            score += 8
    # Penalizar PCMs "hw:" porque son exclusivos en Linux (los suele bloquear pulse/pipewire o el navegador).
    if _is_hw_pcm_name(pn):
        score -= 6
    # Bonificación solo para el mux corto; si la etiqueta pide USB/hardware concreto,
    # penalizar fuerte para no abrir sysdefault/pulse y capturar el mic interno equivocado.
    if _pyaudio_input_name_is_virtual_only(pya_name):
        if _label_suggests_specific_physical_input(label):
            score -= 28
        else:
            score += 2
    if "usb" in label.lower() and "usb" in pn:
        score += 10
    return score


def find_virtual_input_index() -> int | None:
    """Devuelve el índice de un PCM compartido (`pipewire`/`pulse`/`default`/`sysdefault`)."""
    count = pya.get_device_count()
    for preferred in _VIRTUAL_PCM_NAMES:
        for i in range(count):
            try:
                info = pya.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) <= 0:
                    continue
                if (info.get("name") or "").strip().lower() == preferred:
                    return i
            except Exception:
                continue
    return None


def find_named_input_index(*needles: str) -> int | None:
    """Devuelve el primer device de entrada cuyo nombre contiene `needle` (case-insensitive)."""
    count = pya.get_device_count()
    for i in range(count):
        try:
            info = pya.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            name = (info.get("name") or "").lower()
            for needle in needles:
                if needle and needle.lower() in name:
                    return i
        except Exception:
            continue
    return None


def resolve_pyaudio_input_index_from_browser_label(label: str | None) -> int | None:
    """Map browser mic label to a PyAudio input index.

    The browser's enumerated device index is NOT comparable to PyAudio's host API index;
    never use the frontend's device_index for opening the capture stream.
    Returns None to fall back to the default input device.
    """
    if not label or not str(label).strip():
        return None

    count = pya.get_device_count()
    best_i = None
    best_score = 0
    for i in range(count):
        try:
            info = pya.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            name = info.get("name") or ""
            s = _score_mic_label_against_pyaudio_name(label, name)
            if s > best_score:
                best_score = s
                best_i = i
        except Exception:
            continue

    # Si el ganador es solo un mux virtual pero la etiqueta describe USB u otro hardware,
    # elegir el mejor dispositivo *no* virtual (evita «Default» → sysdefault).
    if best_i is not None and best_score >= 2:
        try:
            best_nm = (pya.get_device_info_by_index(best_i).get("name") or "").strip().lower()
        except Exception:
            best_nm = ""
        if _pyaudio_input_name_is_virtual_only(best_nm) and _label_suggests_specific_physical_input(
            str(label)
        ):
            alt_i = None
            alt_score = 0
            for i in range(count):
                try:
                    info = pya.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) <= 0:
                        continue
                    nm = (info.get("name") or "").strip()
                    if _pyaudio_input_name_is_virtual_only(nm.lower()):
                        continue
                    s = _score_mic_label_against_pyaudio_name(str(label), nm)
                    if s > alt_score:
                        alt_score = s
                        alt_i = i
                except Exception:
                    continue
            if alt_i is not None and alt_score >= (2 if "usb" in str(label).lower() else 3):
                return alt_i

    # Etiqueta vaga («Default», etc.): poca confianza + mux virtual → si hay USB ALSA, usarlo.
    if best_i is not None and best_score <= 4:
        try:
            vague_nm = (pya.get_device_info_by_index(best_i).get("name") or "").strip().lower()
        except Exception:
            vague_nm = ""
        if _pyaudio_input_name_is_virtual_only(vague_nm):
            u = find_named_input_index("usb")
            if u is not None:
                return u

    # Umbral 2: con etiquetas cortas o marcas raras el USB a veces quedaba en 2 puntos y caía al fallback genérico.
    if best_i is not None and best_score >= 2:
        return best_i

    ln = label.lower()
    for i in range(count):
        try:
            info = pya.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            name = (info.get("name") or "").lower()
            if _is_hw_pcm_name(name):
                continue
            if ln in name or name in ln:
                return i
        except Exception:
            continue

    return None


def normalize_open_document_extensions(exts: list | None) -> list[str]:
    """Normaliza extensiones permitidas para `open_document`: minúsculas, con punto inicial, sin duplicados."""
    out: list[str] = []
    if not exts:
        return out
    for x in exts:
        if not isinstance(x, str):
            continue
        s = x.strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        out.append(s)
    return sorted(set(out))


def default_open_document_settings_dict() -> dict:
    return {
        "limit_extensions": False,
        "allow_directories": True,
        "allowed_extensions": [],
    }


class AudioLoop:
    def __init__(self, video_mode=DEFAULT_MODE, on_audio_data=None, on_video_frame=None, on_web_data=None, on_transcription=None, on_tool_confirmation=None, on_project_update=None, on_device_update=None, on_error=None, on_open_url=None, input_device_index=None, input_device_name=None, output_device_index=None, kasa_agent=None, voice_name: str | None = None, response_language: str | None = None, open_document_settings: dict | None = None):
        self.video_mode = video_mode
        self.on_audio_data = on_audio_data
        self.on_video_frame = on_video_frame
        self.on_web_data = on_web_data
        self.on_transcription = on_transcription
        self.on_tool_confirmation = on_tool_confirmation 
        self.on_project_update = on_project_update
        self.on_device_update = on_device_update
        self.on_error = on_error
        # Callback para delegar al frontend la apertura de `mailto:` (Electron,
        # navegador en Android, etc.). El backend suele correr en el PC; abrir
        # el correo en la misma máquina que muestra la UI evita depender del
        # entorno gráfico del proceso Python y de acertar con el User-Agent.
        self.on_open_url = on_open_url
        self.input_device_index = input_device_index
        self.input_device_name = input_device_name
        self.output_device_index = output_device_index
        # Voz a usar al construir la `LiveConnectConfig` en cada conexión.
        self.voice_name = voice_name if voice_name in AVAILABLE_VOICES else DEFAULT_VOICE
        self.response_language = (
            response_language
            if response_language in AVAILABLE_RESPONSE_LANGUAGES
            else DEFAULT_RESPONSE_LANGUAGE
        )

        self.audio_in_queue = None
        self.out_queue = None
        self.paused = False

        self.chat_buffer = {"sender": None, "text": ""} # For aggregating chunks
        
        # Track last transcription text to calculate deltas (Gemini sends cumulative text)
        self._last_input_transcription = ""
        self._last_output_transcription = ""

        self.audio_in_queue = None
        self.out_queue = None
        self.paused = False

        self.session = None
        
        self.web_agent = WebAgent()
        self.kasa_agent = kasa_agent if kasa_agent else KasaAgent()
        self.send_text_task = None
        self.stop_event = asyncio.Event()
        
        self.stop_event = asyncio.Event()
        
        self.permissions = {} # Default Empty (Will treat unset as True)
        self._pending_confirmations = {}

        # Política para la herramienta `open_document` (extensiones y carpetas).
        _od = default_open_document_settings_dict()
        if open_document_settings:
            _od["limit_extensions"] = bool(open_document_settings.get("limit_extensions", _od["limit_extensions"]))
            _od["allow_directories"] = bool(open_document_settings.get("allow_directories", _od["allow_directories"]))
            raw_ext = open_document_settings.get("allowed_extensions", _od["allowed_extensions"])
            _od["allowed_extensions"] = normalize_open_document_extensions(
                raw_ext if isinstance(raw_ext, list) else []
            )
        self.open_document_settings = _od

        # Video buffering state
        self._latest_image_payload = None
        # VAD State
        self._is_speaking = False
        self._silence_start_time = None

        # Initialize ProjectManager
        from project_manager import ProjectManager
        # Assuming we are running from backend/ or root? 
        # Using abspath of current file to find root
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # If neno.py is in backend/, project root is one up
        project_root = os.path.dirname(current_dir)
        self.project_manager = ProjectManager(project_root)
        
        # Sync Initial Project State
        if self.on_project_update:
            # We need to defer this slightly or just call it. 
            # Since this is init, loop might not be running, but on_project_update in server.py uses asyncio.create_task which needs a loop.
            # We will handle this by calling it in run() or just print for now.
            pass

    def flush_chat(self):
        """Forces the current chat buffer to be written to log."""
        if self.chat_buffer["sender"] and self.chat_buffer["text"].strip():
            self.project_manager.log_chat(self.chat_buffer["sender"], self.chat_buffer["text"])
            self.chat_buffer = {"sender": None, "text": ""}
        # Reset transcription tracking for new turn
        self._last_input_transcription = ""
        self._last_output_transcription = ""

    def update_permissions(self, new_perms):
        log.debug("[CONFIG] Updating tool permissions: %s", new_perms)
        self.permissions.update(new_perms)

    def update_open_document_settings(self, new_settings: dict | None) -> None:
        """Actualiza límites de `open_document` sin reiniciar el AudioLoop (p. ej. desde Ajustes)."""
        if not new_settings:
            return
        if "limit_extensions" in new_settings:
            self.open_document_settings["limit_extensions"] = bool(new_settings["limit_extensions"])
        if "allow_directories" in new_settings:
            self.open_document_settings["allow_directories"] = bool(new_settings["allow_directories"])
        if "allowed_extensions" in new_settings:
            raw = new_settings["allowed_extensions"]
            self.open_document_settings["allowed_extensions"] = normalize_open_document_extensions(
                raw if isinstance(raw, list) else []
            )
        log.debug("[CONFIG] open_document settings: %s", self.open_document_settings)

    def set_paused(self, paused):
        self.paused = paused

    def stop(self):
        self.stop_event.set()
        
    def resolve_tool_confirmation(self, request_id, confirmed):
        log.debug("[RESOLVE] resolve_tool_confirmation called. ID=%s confirmed=%s", request_id, confirmed)
        if request_id in self._pending_confirmations:
            future = self._pending_confirmations[request_id]
            if not future.done():
                log.debug("[RESOLVE] Future found and pending. Setting result to: %s", confirmed)
                future.set_result(confirmed)
            else:
                log.warning("[RESOLVE] Request %s future already done. Result: %s", request_id, future.result())
        else:
            log.warning(
                "[RESOLVE] Confirmation Request %s not found in pending dict. Keys: %s",
                request_id, list(self._pending_confirmations.keys()),
            )

    def clear_audio_queue(self):
        """Clears the queue of pending audio chunks to stop playback immediately."""
        try:
            count = 0
            while not self.audio_in_queue.empty():
                self.audio_in_queue.get_nowait()
                count += 1
            if count > 0:
                log.debug("[AUDIO] Cleared %d chunks from playback queue due to interruption.", count)
        except Exception as e:
            log.warning("[AUDIO] Failed to clear audio queue: %s", e)

    async def send_frame(self, frame_data):
        """Receive a JPEG video frame from the frontend (or local capture).

        Stores it as the latest payload to be sent right after the user starts speaking.
        Gemini Live API expects raw bytes (not base64) inside the Blob.
        """
        if isinstance(frame_data, bytes):
            raw_bytes = frame_data
        elif isinstance(frame_data, str):
            payload = frame_data.split(",", 1)[-1] if frame_data.startswith("data:") else frame_data
            try:
                raw_bytes = base64.b64decode(payload)
            except Exception:
                raw_bytes = payload.encode("utf-8")
        else:
            raw_bytes = bytes(frame_data)

        self._latest_image_payload = {"data": raw_bytes, "mime_type": "image/jpeg"}

    async def send_realtime(self):
        """Pump realtime media (audio chunks + occasional video frames) to Gemini Live.

        PCM MUST use `send_realtime_input(audio=Blob(...))` — see LiveSendRealtimeInputParameters.audio.
        """
        # #region agent log
        _sent_chunks = 0
        # #endregion
        while True:
            msg = await self.out_queue.get()
            try:
                # Con `automatic_activity_detection` en LiveConnectConfig NO se puede usar
                # activity_end/activity_start manual (API 1007). El cierre de turno lo marca el VAD del servicio.
                if isinstance(msg, dict) and msg.get("activity_end"):
                    continue
                if not isinstance(msg, dict) or "data" not in msg:
                    raise TypeError(f"Bad realtime queue item: {type(msg)}")
                mime = msg.get("mime_type") or "application/octet-stream"
                blob = types.Blob(data=msg["data"], mime_type=mime)
                if mime.startswith("audio/"):
                    branch = "audio_kwarg"
                    await self.session.send_realtime_input(audio=blob)
                else:
                    branch = "media_kwarg"
                    await self.session.send_realtime_input(media=blob)
                # #region agent log
                _sent_chunks += 1
                if _sent_chunks in (1, 5, 25, 100):
                    _dlog(
                        "neno.py:send_realtime",
                        f"send_realtime_input OK chunk #{_sent_chunks}",
                        {
                            "chunk_no": _sent_chunks,
                            "branch": branch,
                            "mime_type": mime,
                            "data_len": len(msg["data"]),
                        },
                        hypothesis="H3_fix",
                    )
                # #endregion
            except Exception as e:
                log.error("send_realtime_input failed: %s: %s", type(e).__name__, e)
                # #region agent log
                _dlog(
                    "neno.py:send_realtime",
                    "send_realtime_input EXCEPTION",
                    {
                        "error_type": type(e).__name__,
                        "error": str(e)[:500],
                        "mime_type": (msg or {}).get("mime_type") if isinstance(msg, dict) else "non-dict",
                        "chunk_no": _sent_chunks + 1,
                    },
                    hypothesis="H2,H3",
                )
                # #endregion
                raise

    async def listen_audio(self):
        print("[NENO-MIC] listen_audio task started", flush=True)
        log.info("[NENO-MIC] listen_audio task started")

        if self.input_device_index is not None:
            log.info("[NENO-MIC] Ignoring frontend device_index (PyAudio numbering differs).")

        async def _try(fn, *args, timeout=2.0, label=""):
            try:
                return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("[NENO-MIC] %s timed out", label)
            except Exception as e:
                log.warning("[NENO-MIC] %s failed: %s", label, e)
            return None

        resolved_input_device_index = await _try(
            resolve_pyaudio_input_index_from_browser_label,
            self.input_device_name,
            label="resolve_label",
        )
        pulse_idx = await _try(find_named_input_index, "pulse", label="find_pulse")
        pipewire_idx = await _try(find_named_input_index, "pipewire", label="find_pipewire")
        usb_idx = await _try(
            find_named_input_index,
            "usb",
            "usb audio",
            "uac",
            "iec958",
            "yeti",
            "rode",
            "headset",
            label="find_usb_input",
        )

        candidate_indices: list[tuple[int | None, str]] = []
        seen_indices: set[int | None] = set()

        def _push_candidate(idx: int | None, label: str, allow_none: bool = False) -> None:
            # `None` solo se acepta cuando es el fallback explícito al
            # default de PortAudio. Los `None` que vienen de las búsquedas
            # ("no encontrado") no deben colarse o bloquearían los demás
            # candidatos por la dedup de `seen_indices`.
            if idx is None and not allow_none:
                return
            if idx in seen_indices:
                return
            seen_indices.add(idx)
            candidate_indices.append((idx, label))

        # IMPORTANTE: si el usuario eligió un mic en el navegador, debemos probar
        # primero el índice PyAudio que mejor coincide con esa etiqueta. Si abrimos
        # antes `pipewire`/`pulse`, PortAudio usa la fuente *predeterminada* del
        # servidor de sonido (suele ser el mic interno) y el USB «seleccionado»
        # en la UI nunca se usa aunque el match exista.
        if resolved_input_device_index is not None:
            _push_candidate(resolved_input_device_index, "matched_label")
        # Pulse/PipeWire como respaldo (mux con otros procesos) o si no hay etiqueta.
        _push_candidate(pipewire_idx, "pipewire")
        _push_candidate(pulse_idx, "pulse")
        if resolved_input_device_index is None:
            _push_candidate(usb_idx, "usb_input")
        _push_candidate(None, "portaudio_default", allow_none=True)

        log.info(
            "[NENO-MIC] candidates: %s",
            [(idx, src) for idx, src in candidate_indices],
        )

        def _open_input_stream(device_idx: int):
            with _suppress_stderr():
                return pya.open(
                    format=FORMAT,
                    channels=CHANNELS,
                    rate=SEND_SAMPLE_RATE,
                    input=True,
                    input_device_index=device_idx,
                    frames_per_buffer=CHUNK_SIZE,
                )

        last_error: Exception | None = None
        opened_idx: int | None | str = "<none>"
        for idx, source in candidate_indices:
            picked_name = "?"
            if idx is not None:
                try:
                    picked = pya.get_device_info_by_index(idx)
                    picked_name = picked.get("name", "?")
                except Exception:
                    picked_name = "?"
            else:
                picked_name = "<portaudio default>"
            print(f"[NENO-MIC] Trying idx={idx} ({source}) name={picked_name!r}", flush=True)
            log.info("[NENO-MIC] Trying input device idx=%s (%s) name=%r", idx, source, picked_name)
            _dlog(
                "neno.py:listen_audio",
                "About to open input stream",
                {"stream_device_index": idx, "source": source, "name": picked_name,
                 "rate": SEND_SAMPLE_RATE, "channels": CHANNELS, "chunk": CHUNK_SIZE},
                hypothesis="H1",
            )
            try:
                self.audio_stream = await asyncio.to_thread(_open_input_stream, idx)
                opened_idx = idx
                _dlog(
                    "neno.py:listen_audio",
                    "Input stream opened OK",
                    {"stream_device_index": idx, "source": source, "name": picked_name},
                    hypothesis="H1",
                )
                print(f"[NENO-MIC] OPEN OK idx={idx} ({source}) name={picked_name!r}", flush=True)
                log.info("[NENO-MIC] Input stream OK on idx=%s (%s) name=%r", idx, source, picked_name)
                break
            except Exception as e:
                last_error = e
                print(f"[NENO-MIC] FAILED idx={idx} ({source}): {type(e).__name__}: {e}", flush=True)
                log.warning("[NENO-MIC] Failed to open idx=%s (%s): %s", idx, source, e)
                _dlog(
                    "neno.py:listen_audio",
                    "pya.open FAILED (will try fallback if any)",
                    {"error_type": type(e).__name__, "error": str(e)[:500],
                     "stream_device_index": idx, "source": source},
                    hypothesis="H1",
                )

        if opened_idx == "<none>":
            log.error("Failed to open audio input stream: %s", last_error)
            log.warning("Audio features will be disabled. Please check microphone permissions.")
            return

        try:
            await self._listen_audio_loop()
        finally:
            # Cierra el stream de captura al cancelar/terminar la tarea para
            # evitar que un AudioLoop antiguo siga capturando del mic en paralelo
            # con uno nuevo (causa de eco y reconocimiento fragmentado).
            stream = getattr(self, "audio_stream", None)
            if stream is not None:
                try:
                    await asyncio.to_thread(stream.stop_stream)
                except Exception:
                    pass
                try:
                    await asyncio.to_thread(stream.close)
                except Exception:
                    pass
                self.audio_stream = None

    async def _listen_audio_loop(self):
        if __debug__:
            kwargs = {"exception_on_overflow": False}
        else:
            kwargs = {}

        # VAD Constants
        VAD_THRESHOLD = 800 # Adj based on mic sensitivity (800 is conservative for 16-bit)
        SILENCE_DURATION = 0.5 # Seconds of silence to consider "done speaking"
        AUDIO_MIME = f"audio/pcm;rate={SEND_SAMPLE_RATE}"
        first_chunk_logged = False

        # Diagnóstico periódico de nivel: imprimimos el RMS máx visto en
        # cada ventana de ~3 s para saber si el mic está captando audio
        # real o silencio (mic equivocado / muteado por sound server).
        _rms_window_start = time.time()
        _rms_window_max = 0
        _chunks_in_window = 0

        while True:
            if self.paused:
                await asyncio.sleep(0.1)
                continue

            try:
                data = await asyncio.to_thread(self.audio_stream.read, CHUNK_SIZE, **kwargs)

                if not first_chunk_logged:
                    print(
                        f"[NENO-MIC] Mic stream live: {len(data)} bytes/chunk @ {SEND_SAMPLE_RATE}Hz",
                        flush=True,
                    )
                    log.info(
                        "[NENO-MIC] Mic stream live: %d bytes/chunk, rate=%d, format=int16. Listening for speech.",
                        len(data), SEND_SAMPLE_RATE,
                    )
                    first_chunk_logged = True
                    # #region agent log
                    _dlog(
                        "neno.py:listen_audio",
                        "First mic chunk read",
                        {"bytes": len(data), "mime_type": AUDIO_MIME},
                        hypothesis="H1,H4",
                    )
                    # #endregion

                # 1. Send Audio (raw PCM bytes; mime_type MUST include the rate)
                if self.out_queue:
                    await self.out_queue.put({"data": data, "mime_type": AUDIO_MIME})
                
                # 2. VAD Logic for Video
                # rms = audioop.rms(data, 2)
                # Replacement for audioop.rms(data, 2)
                count = len(data) // 2
                if count > 0:
                    shorts = struct.unpack(f"<{count}h", data)
                    sum_squares = sum(s**2 for s in shorts)
                    rms = int(math.sqrt(sum_squares / count))
                else:
                    rms = 0

                _rms_window_max = max(_rms_window_max, rms)
                _chunks_in_window += 1
                _now = time.time()
                if _now - _rms_window_start >= 3.0:
                    # Solo DEBUG: evita saturar la consola cada ~3 s al silenciar/activar el mic.
                    log.debug(
                        "[NENO-MIC] level: max RMS=%s over %s chunks (%s)",
                        _rms_window_max,
                        _chunks_in_window,
                        "voz/sonido" if _rms_window_max > VAD_THRESHOLD else "silencio",
                    )
                    _rms_window_start = _now
                    _rms_window_max = 0
                    _chunks_in_window = 0

                if rms > VAD_THRESHOLD:
                    # Speech Detected
                    self._silence_start_time = None
                    
                    if not self._is_speaking:
                        # NEW Speech Utterance Started
                        self._is_speaking = True
                        log.debug("[VAD] Speech detected (RMS=%d). Sending video frame.", rms)

                        # Send ONE frame
                        if self._latest_image_payload and self.out_queue:
                            await self.out_queue.put(self._latest_image_payload)
                        else:
                            log.debug("[VAD] No video frame available to send.")
                            
                else:
                    # Silence
                    if self._is_speaking:
                        if self._silence_start_time is None:
                            self._silence_start_time = time.time()
                        
                        elif time.time() - self._silence_start_time > SILENCE_DURATION:
                            # Silencio local: solo reseteamos estado (p. ej. envío de un frame de vídeo).
                            # No enviar activity_end: choca con automatic_activity_detection (error 1007).
                            log.debug("[VAD] Silence detected. Resetting speech state.")
                            self._is_speaking = False
                            self._silence_start_time = None

            except Exception as e:
                log.error("Error reading audio: %s", e)
                # #region agent log
                _dlog(
                    "neno.py:listen_audio",
                    "audio_stream.read EXCEPTION (looping)",
                    {"error_type": type(e).__name__, "error": str(e)[:300]},
                    hypothesis="H4",
                )
                # #endregion
                await asyncio.sleep(0.1)

    async def handle_write_file(self, path, content):
        log.debug("[FS] Writing file: %r", path)

        # Auto-create project if stuck in temp
        if self.project_manager.current_project == "temp":
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            new_project_name = f"Project_{timestamp}"
            log.debug("[FS] Auto-creating project: %s", new_project_name)

            success, msg = self.project_manager.create_project(new_project_name)
            if success:
                self.project_manager.switch_project(new_project_name)
                # Notify User
                try:
                    await self.session.send(input=f"System Notification: Automatic Project Creation. Switched to new project '{new_project_name}'.", end_of_turn=False)
                    if self.on_project_update:
                         self.on_project_update(new_project_name)
                except Exception as e:
                    log.warning("[FS] Failed to notify auto-project: %s", e)
        
        # Force path to be relative to current project
        # If absolute path is provided, we try to strip it or just ignore it and use basename
        filename = os.path.basename(path)
        
        # If path contained subdirectories (e.g. "backend/server.py"), preserving that structure might be desired IF it's within the project.
        # But for safety, and per user request to "always create the file in the project", 
        # we will root it in the current project path.
        
        current_project_path = self.project_manager.get_current_project_path()
        final_path = current_project_path / filename # Simple flat structure for now, or allow relative?
        
        # If the user specifically wanted a subfolder, they might have provided "sub/file.txt".
        # Let's support relative paths if they don't start with /
        if not os.path.isabs(path):
             final_path = current_project_path / path
        
        log.debug("[FS] Resolved path: %r", final_path)

        try:
            # Ensure parent exists
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            with open(final_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = f"File '{final_path.name}' written successfully to project '{self.project_manager.current_project}'."
        except Exception as e:
            result = f"Failed to write file '{path}': {str(e)}"

        log.debug("[FS] Result: %s", result)
        try:
            await self.session.send(input=f"System Notification: {result}", end_of_turn=True)
        except Exception as e:
            log.warning("[FS] Failed to send fs result: %s", e)

    async def handle_read_directory(self, path):
        log.debug("[FS] Reading directory: %r", path)
        try:
            if not os.path.exists(path):
                result = f"Directory '{path}' does not exist."
            else:
                items = os.listdir(path)
                result = f"Contents of '{path}': {', '.join(items)}"
        except Exception as e:
            result = f"Failed to read directory '{path}': {str(e)}"

        log.debug("[FS] Result: %s", result)
        try:
            await self.session.send(input=f"System Notification: {result}", end_of_turn=True)
        except Exception as e:
            log.warning("[FS] Failed to send fs result: %s", e)

    async def handle_read_file(self, path):
        log.debug("[FS] Reading file: %r", path)
        try:
            if not os.path.exists(path):
                result = f"File '{path}' does not exist."
            else:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                result = f"Content of '{path}':\n{content}"
        except Exception as e:
            result = f"Failed to read file '{path}': {str(e)}"

        log.debug("[FS] Result: %s", result)
        try:
            await self.session.send(input=f"System Notification: {result}", end_of_turn=True)
        except Exception as e:
            log.warning("[FS] Failed to send fs result: %s", e)

    def _open_document_policy_allows(self, target: Path) -> tuple[bool, str]:
        """Comprueba política de extensiones / carpetas. Devuelve (permitido, mensaje de error en castellano)."""
        pol = self.open_document_settings
        if target.is_dir():
            if not pol.get("allow_directories", True):
                return False, (
                    "Abrir carpetas está desactivado en Ajustes. "
                    "Pide al usuario que active «Permitir abrir carpetas» en la sección «Abrir documentos (IA)» "
                    "o que abra la carpeta manualmente."
                )
            return True, ""
        if not target.is_file():
            return False, "La ruta no es un archivo ni una carpeta reconocible."
        if not pol.get("limit_extensions", False):
            return True, ""
        allowed = set(pol.get("allowed_extensions") or [])
        if not allowed:
            return False, (
                "La restricción por tipo de archivo está activa pero la lista de extensiones permitidas está vacía. "
                "Configura en Ajustes al menos un tipo (PDF, imágenes, etc.) o desactiva la restricción."
            )
        suf = target.suffix.lower()
        if not suf:
            return False, (
                "Este archivo no tiene extensión y la restricción por tipo está activa; no lo abro. "
                "Puedes desactivar la restricción en Ajustes o pedir al usuario que abra el fichero a mano."
            )
        if suf not in allowed:
            sample = ", ".join(sorted(allowed)[:20])
            tail = " …" if len(allowed) > 20 else ""
            return False, (
                f"La extensión «{suf}» no está entre las permitidas para abrir con el sistema. "
                f"Tipos permitidos (muestra): {sample}{tail}. "
                "Amplía la lista en Ajustes o abre el archivo manualmente."
            )
        return True, ""

    async def handle_open_document(self, path: str) -> str:
        log.debug("[FS] open_document path=%r", path)
        try:
            raw = Path(os.path.expanduser((path or "").strip()))
            if raw.is_absolute():
                target = raw.resolve()
            else:
                target = (self.project_manager.get_current_project_path() / raw).resolve()
        except Exception as e:
            log.warning("[FS] open_document path resolve failed: %s", e)
            return f"No he podido interpretar la ruta: {e}"

        if not target.exists():
            return (
                f"No existe ningún archivo o carpeta en «{target}». "
                "Pide al usuario la ruta correcta o lista el directorio con read_directory."
            )

        ok_pol, err_pol = self._open_document_policy_allows(target)
        if not ok_pol:
            log.info("[FS] open_document policy blocked: %s", err_pol)
            return err_pol

        opened = self._open_path_in_os(str(target))
        if opened:
            log.info("[FS] open_document opened: %s", target)
            return (
                f"He abierto «{target.name}» con la aplicación predeterminada del sistema. "
                "Confirma brevemente al usuario que ya puede verlo en su programa."
            )
        log.warning("[FS] open_document failed for %s", target)
        return (
            "No he podido abrir el archivo con el programa predeterminado. "
            "Pídele al usuario que lo abra manualmente desde el gestor de archivos."
        )

    @staticmethod
    def _open_path_in_os(resolved: str) -> bool:
        import shutil
        import subprocess

        try:
            if sys.platform == "win32":
                try:
                    os.startfile(resolved)  # type: ignore[attr-defined]
                    return True
                except (AttributeError, OSError) as e:
                    log.warning("[FS] os.startfile failed: %s", e)
                subprocess.Popen(["cmd", "/c", "start", "", resolved], shell=False)
                return True
            if sys.platform == "darwin":
                subprocess.Popen(["open", resolved])
                return True
            if sys.platform.startswith("linux") and shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", resolved])
                return True
        except Exception as e:
            log.warning("[FS] platform opener failed: %s", e)
            return False

        try:
            uri = Path(resolved).as_uri()
            return bool(webbrowser.open(uri))
        except Exception as e:
            log.warning("[FS] webbrowser fallback failed: %s", e)
            return False

    async def handle_web_agent_request(self, prompt):
        log.debug("[WEB] Web Agent task: %r", prompt)

        async def update_frontend(image_b64, log_text):
            if self.on_web_data:
                self.on_web_data({"image": image_b64, "log": log_text})

        # Ejecutar el agente. Capturamos errores para poder caer al navegador del sistema.
        result = "Agent finished without a final summary."
        agent_exception = None
        try:
            result = await self.web_agent.run_task(prompt, update_callback=update_frontend)
        except Exception as e:
            agent_exception = e
            log.warning("[WEB] Exception running web agent: %s", e)

        # Heurística: si el agente no produjo resumen útil o golpeó cuota, abrimos navegador del sistema.
        result_str = result if isinstance(result, str) else str(result)
        no_summary = "without a final summary" in result_str.lower()
        quota_hit = ("RESOURCE_EXHAUSTED" in result_str) or ("exceeded your current quota" in result_str.lower())

        if agent_exception is not None or no_summary or quota_hit:
            try:
                url = self._build_system_browser_url(prompt)
                log.info("[WEB] Falling back to system browser: %s", url)
                opened = webbrowser.open(url, new=2)
                if not opened:
                    log.warning("[WEB] webbrowser.open returned False; check $BROWSER / xdg-open.")
                if self.on_web_data:
                    self.on_web_data({
                        "image": None,
                        "log": f"Resultados abiertos en el navegador del sistema: {url}",
                    })
                # Mensaje positivo: para el usuario la búsqueda SÍ está hecha (la ve en el navegador).
                # No mencionar al modelo cuotas ni fallos del agente para que no diga "no he podido".
                result = (
                    f"He abierto el navegador del sistema en {url} con los resultados de la "
                    f"búsqueda '{prompt}'. El usuario ya puede ver la página y elegir un resultado. "
                    "Confirma brevemente al usuario que la búsqueda está abierta en su navegador, "
                    "sin decir que no se ha podido completar."
                )
            except Exception as e:
                log.warning("[WEB] Failed to open system browser: %s", e)
                result = (
                    f"No he podido abrir automáticamente el navegador del sistema para '{prompt}'. "
                    "Indica al usuario que abra manualmente el resultado en su navegador."
                )

        log.debug("[WEB] Web Agent task returned: %s", result)

        try:
            await self.session.send(input=f"System Notification: Web Agent has finished.\nResult: {result}", end_of_turn=True)
        except Exception as e:
            log.warning("[WEB] Failed to send web agent result to model: %s", e)

    @staticmethod
    def _build_system_browser_url(prompt: str) -> str:
        """Construye la URL más razonable a abrir en el navegador del sistema.

        - Detecta sitios habituales (amazon[.es], ebay[.es], aliexpress, youtube, wikipedia, github)
          y lleva al buscador correspondiente.
        - Si no hay sitio reconocido, recurre a Google.
        """
        text = (prompt or "").strip()
        lower = text.lower()

        verbs = (
            r"buscar(?:me|nos)?|busca(?:me)?|b[uú]scame|encuentra(?:me)?|encontrar|"
            r"search(?:\s+for)?|find|look\s+up"
        )
        query = re.sub(rf"^\s*(?:{verbs})\s+", "", text, flags=re.IGNORECASE)
        query = re.sub(rf"\b(?:{verbs})\b", "", query, flags=re.IGNORECASE)
        query = query.strip(" '\".:,;")

        is_spain = any(tag in lower for tag in ("amazon.es", "españa", "espana", " es ", " spain"))

        targets = [
            ("amazon", "https://www.amazon.es/s?k={q}" if is_spain else "https://www.amazon.com/s?k={q}"),
            ("ebay", "https://www.ebay.es/sch/i.html?_nkw={q}" if is_spain else "https://www.ebay.com/sch/i.html?_nkw={q}"),
            ("aliexpress", "https://www.aliexpress.com/wholesale?SearchText={q}"),
            ("youtube", "https://www.youtube.com/results?search_query={q}"),
            ("wikipedia", "https://es.wikipedia.org/w/index.php?search={q}" if is_spain else "https://en.wikipedia.org/w/index.php?search={q}"),
            ("github", "https://github.com/search?q={q}"),
        ]
        for needle, template in targets:
            if needle in lower:
                cleaned = re.sub(rf"\b(?:en|in)\s+{re.escape(needle)}(?:\.[a-z]+)?\b", "", query, flags=re.IGNORECASE)
                cleaned = re.sub(r"\b(?:españa|espana|spain)\b", "", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s+", " ", cleaned).strip(" '\".:,;")
                return template.format(q=urllib.parse.quote_plus(cleaned or query or text))

        return f"https://www.google.com/search?q={urllib.parse.quote_plus(query or text)}"

    async def handle_open_email_client(self, to=None, subject=None, body=None, cc=None, bcc=None) -> str:
        """Abre el gestor de correo del sistema operativo (síncrono, instantáneo).

        Si existe `on_open_url` (sesión con Socket.IO y frontend conectado),
        se delega primero la URL `mailto:` al cliente: en Electron se usa
        `shell.openExternal` y en el navegador un enlace programático; así se
        abre el gestor en el mismo dispositivo que la UI (incluido Android por
        Wi‑Fi) sin depender del User-Agent ni del entorno gráfico del proceso
        Python.

        Si no hay callback o falla, se usa el cliente predeterminado del SO
        donde corre el backend:

        - Windows: `os.startfile(mailto:URL)`.
        - macOS:   `open <URL>`.
        - Linux:   `xdg-email` / `xdg-open`, o en Termux/Android `am start`
          con intent `ACTION_SENDTO` y esquema `mailto:`.
        - Fallback: `webbrowser.open(URL)`.

        Devuelve un mensaje en castellano apto para que el modelo lo lea al usuario.
        """
        url = self._build_mailto_url(to=to, subject=subject, body=body, cc=cc, bcc=bcc)

        if callable(self.on_open_url):
            log.info("[EMAIL] Delegating mailto to connected UI: %s", url)
            try:
                self.on_open_url(url)
                target_descr = f"a {to}" if to else "vacío"
                return (
                    f"He abierto el gestor de correo con un nuevo mensaje {target_descr}. "
                    "Confirma brevemente al usuario que el cliente de correo ya está abierto y listo "
                    "para que escriba o revise el borrador."
                )
            except Exception as e:
                log.warning("[EMAIL] on_open_url failed, falling back to local opener: %s", e)

        log.info("[EMAIL] Opening mail client locally (%s): %s", sys.platform, url)

        opened = False
        try:
            if sys.platform == "win32":
                opened = self._open_email_windows(url)
            elif sys.platform == "darwin":
                opened = self._open_email_macos(url)
            elif sys.platform.startswith("linux"):
                # `_open_email_linux` ya detecta y prioriza el caso Termux/Android
                # cuando corresponde (entornos con `am` y sin `xdg-email`).
                opened = self._open_email_linux(
                    url, to=to, subject=subject, body=body, cc=cc, bcc=bcc,
                )
        except Exception as e:
            log.warning("[EMAIL] Platform-specific opener failed: %s", e)
            opened = False

        # Fallback universal: el módulo `webbrowser` también sabe abrir mailto:
        # si nada de lo anterior funcionó (por ejemplo, plataforma exótica).
        if not opened:
            try:
                opened = webbrowser.open(url, new=1)
            except Exception as e:
                log.warning("[EMAIL] webbrowser.open raised: %s", e)

        if opened:
            target_descr = f"a {to}" if to else "vacío"
            return (
                f"He abierto el gestor de correo del sistema con un nuevo mensaje {target_descr}. "
                "Confirma brevemente al usuario que el cliente de correo ya está abierto y listo "
                "para que escriba o revise el borrador."
            )
        return (
            "No he podido abrir automáticamente el gestor de correo. "
            "Pídele al usuario que abra su cliente de correo manualmente."
        )

    @staticmethod
    def _open_email_windows(url: str) -> bool:
        """ShellExecute resuelve el handler de mailto: en Windows."""
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except (AttributeError, OSError) as e:
            log.warning("[EMAIL] os.startfile failed: %s", e)
        # Plan B: 'start' a través de cmd.
        try:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
            return True
        except Exception as e:
            log.warning("[EMAIL] cmd /c start fallback failed: %s", e)
            return False

    @staticmethod
    def _open_email_macos(url: str) -> bool:
        """`open` delega en LaunchServices."""
        try:
            import subprocess
            subprocess.Popen(["open", url])
            return True
        except Exception as e:
            log.warning("[EMAIL] /usr/bin/open failed: %s", e)
            return False

    @staticmethod
    def _flatten_exception(exc: BaseException) -> list:
        """Aplana ExceptionGroup/BaseExceptionGroup recursivamente en una lista plana.

        Útil para extraer las causas reales de los `unhandled errors in a TaskGroup`
        que produce `asyncio.TaskGroup` cuando varias tareas fallan a la vez.
        """
        out: list = []
        stack: list = [exc]
        while stack:
            cur = stack.pop()
            sub = getattr(cur, "exceptions", None)
            if sub:
                # Iteramos al final para preservar el orden de descubrimiento.
                stack.extend(reversed(list(sub)))
            else:
                out.append(cur)
        return out

    @staticmethod
    def _is_running_on_android() -> bool:
        """Heurística para detectar Termux/Android cuando `sys.platform == 'linux'`.

        En Android `sys.platform` también es `linux`, así que distinguir Termux
        del Linux de escritorio se basa en señales del entorno: variables que
        Termux exporta y rutas propias del sistema.
        """
        try:
            if "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ:
                return True
            prefix = os.environ.get("PREFIX", "")
            if prefix.startswith("/data/data/com.termux"):
                return True
            if os.path.isdir("/system/app") and os.path.isdir("/system/priv-app"):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _open_email_android(url: str) -> bool:
        """Abre el cliente de correo en Android (Termux) vía Activity Manager.

        Usa un intent `ACTION_SENDTO` con el esquema `mailto:`, que es la forma
        recomendada para abrir solo apps de correo (no cualquier app que sepa
        manejar URLs). Equivale a tocar un enlace `mailto:` en el navegador.
        """
        import shutil, subprocess
        try:
            if shutil.which("am"):
                cmd = [
                    "am", "start",
                    "-a", "android.intent.action.SENDTO",
                    "-d", url,
                ]
                subprocess.Popen(cmd)
                return True
        except Exception as e:
            log.warning("[EMAIL] Android `am start` failed: %s", e)
        return False

    @staticmethod
    def _open_email_linux(url: str, to=None, subject=None, body=None, cc=None, bcc=None) -> bool:
        """Prefiere xdg-email (campos por flags) y cae a xdg-open.

        En Android (Termux) no existen `xdg-email`/`xdg-open`, así que se
        intenta primero la vía `am start` con intent `mailto:`.
        """
        import shutil, subprocess
        try:
            if AudioLoop._is_running_on_android():
                if AudioLoop._open_email_android(url):
                    return True
            if shutil.which("xdg-email"):
                cmd = ["xdg-email"]
                if subject: cmd += ["--subject", str(subject)]
                if body: cmd += ["--body", str(body)]
                if cc: cmd += ["--cc", str(cc)]
                if bcc: cmd += ["--bcc", str(bcc)]
                if to: cmd.append(str(to))
                subprocess.Popen(cmd)
                return True
            if shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", url])
                return True
            # Último intento en Android si no detectamos Termux por entorno
            # pero `am` sí está disponible (p. ej. shell distintos).
            if shutil.which("am") and AudioLoop._open_email_android(url):
                return True
        except Exception as e:
            log.warning("[EMAIL] xdg fallback failed: %s", e)
        return False

    @staticmethod
    def _build_mailto_url(to=None, subject=None, body=None, cc=None, bcc=None) -> str:
        """Genera una URL `mailto:` compatible con RFC 6068, codificando bien los campos."""
        target = (to or "").strip()
        params = []
        if subject and str(subject).strip():
            params.append(("subject", str(subject)))
        if body and str(body).strip():
            params.append(("body", str(body)))
        if cc and str(cc).strip():
            params.append(("cc", str(cc)))
        if bcc and str(bcc).strip():
            params.append(("bcc", str(bcc)))
        # `quote_via=quote` (no `quote_plus`) porque mailto: usa %20, no '+', para espacios.
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote) if params else ""
        return f"mailto:{urllib.parse.quote(target, safe='@')}" + (f"?{qs}" if qs else "")

    async def receive_audio(self):
        "Background task to reads from the websocket and write pcm chunks to the output queue"
        try:
            while True:
                turn = self.session.receive()
                async for response in turn:
                    # 1. Handle Audio Data
                    if data := response.data:
                        self.audio_in_queue.put_nowait(data)
                        # #region agent log
                        if not hasattr(self, "_dbg_rx_audio_count"):
                            self._dbg_rx_audio_count = 0
                        self._dbg_rx_audio_count += 1
                        if self._dbg_rx_audio_count in (1, 5, 20):
                            _dlog(
                                "neno.py:receive_audio",
                                "model PCM chunk received",
                                {"chunk_index": self._dbg_rx_audio_count, "byte_len": len(data)},
                                hypothesis="H_playback",
                            )
                        # #endregion
                        # NOTE: 'continue' removed here to allow processing transcription/tools in same packet

                    # 2. Handle Transcription (User & Model)
                    if response.server_content:
                        if response.server_content.input_transcription:
                            transcript = response.server_content.input_transcription.text
                            if transcript:
                                # Skip if this is an exact duplicate event
                                if transcript != self._last_input_transcription:
                                    # Calculate delta (Gemini may send cumulative or chunk-based text)
                                    delta = transcript
                                    if transcript.startswith(self._last_input_transcription):
                                        delta = transcript[len(self._last_input_transcription):]
                                    self._last_input_transcription = transcript
                                    
                                    # Only send if there's new text
                                    if delta:
                                        # #region agent log
                                        if not getattr(self, "_dbg_logged_user_asr", False):
                                            self._dbg_logged_user_asr = True
                                            _dlog(
                                                "neno.py:receive_audio",
                                                "first User ASR delta",
                                                {"delta_len": len(delta), "delta_preview": delta[:80]},
                                                hypothesis="H_mic_asr",
                                            )
                                        # #endregion
                                        # Interrumpir la voz del modelo solo en el primer fragmento del turno
                                        # del usuario. Vaciar la cola en cada delta saturaba el bucle y
                                        # provocaba bloqueos y latencia en la transcripción en pantalla.
                                        if self.chat_buffer["sender"] != "User":
                                            self.clear_audio_queue()

                                        # Send to frontend (Streaming)
                                        if self.on_transcription:
                                             self.on_transcription({"sender": "User", "text": delta})
                                        
                                        # Buffer for Logging
                                        if self.chat_buffer["sender"] != "User":
                                            # Flush previous if exists
                                            if self.chat_buffer["sender"] and self.chat_buffer["text"].strip():
                                                self.project_manager.log_chat(self.chat_buffer["sender"], self.chat_buffer["text"])
                                            # Start new
                                            self.chat_buffer = {"sender": "User", "text": delta}
                                        else:
                                            # Append
                                            self.chat_buffer["text"] += delta
                        
                        if response.server_content.output_transcription:
                            transcript = response.server_content.output_transcription.text
                            if transcript:
                                # Skip if this is an exact duplicate event
                                if transcript != self._last_output_transcription:
                                    # Calculate delta (Gemini may send cumulative or chunk-based text)
                                    delta = transcript
                                    if transcript.startswith(self._last_output_transcription):
                                        delta = transcript[len(self._last_output_transcription):]
                                    self._last_output_transcription = transcript
                                    
                                    # Only send if there's new text
                                    if delta:
                                        # Send to frontend (Streaming)
                                        if self.on_transcription:
                                             self.on_transcription({"sender": "N.E.N.O", "text": delta})
                                        
                                        # Buffer for Logging
                                        if self.chat_buffer["sender"] != "N.E.N.O":
                                            # Flush previous
                                            if self.chat_buffer["sender"] and self.chat_buffer["text"].strip():
                                                self.project_manager.log_chat(self.chat_buffer["sender"], self.chat_buffer["text"])
                                            # Start new
                                            self.chat_buffer = {"sender": "N.E.N.O", "text": delta}
                                        else:
                                            # Append
                                            self.chat_buffer["text"] += delta
                        
                        # Flush buffer on turn completion if needed, 
                        # but usually better to wait for sender switch or explicit end.
                        # We can also check turn_complete signal if available in response.server_content.model_turn etc

                    # 3. Handle Tool Calls
                    if response.tool_call:
                        log.debug("[TOOL] Tool call received from model.")
                        function_responses = []
                        for fc in response.tool_call.function_calls:
                            if fc.name in ["run_web_agent", "write_file", "read_directory", "read_file", "open_document", "create_project", "switch_project", "list_projects", "list_smart_devices", "control_light", "open_email_client"]:
                                prompt = fc.args.get("prompt", "") # Prompt is not present for all tools
                                
                                # Check Permissions (Default to True if not set)
                                confirmation_required = self.permissions.get(fc.name, True)
                                
                                if not confirmation_required:
                                    log.debug("[TOOL] Permission check: %r -> AUTO-ALLOW", fc.name)
                                    # Skip confirmation block and jump to execution
                                    pass
                                else:
                                    # Confirmation Logic (request_id debe existir siempre en esta rama)
                                    import uuid
                                    request_id = str(uuid.uuid4())
                                    if not self.on_tool_confirmation:
                                        log.warning(
                                            "[STOP] No on_tool_confirmation callback; denying tool %r",
                                            fc.name,
                                        )
                                        function_response = types.FunctionResponse(
                                            id=fc.id,
                                            name=fc.name,
                                            response={
                                                "result": "Tool execution unavailable (no confirmation handler).",
                                            },
                                        )
                                        function_responses.append(function_response)
                                        continue

                                    log.debug("[STOP] Requesting confirmation for %r (ID=%s)", fc.name, request_id)

                                    future = asyncio.Future()
                                    self._pending_confirmations[request_id] = future

                                    self.on_tool_confirmation({
                                        "id": request_id,
                                        "tool": fc.name,
                                        "args": fc.args
                                    })

                                    try:
                                        confirmed = await future
                                    finally:
                                        self._pending_confirmations.pop(request_id, None)

                                    log.debug("[CONFIRM] Request %s resolved. Confirmed=%s", request_id, confirmed)

                                    if not confirmed:
                                        log.debug("[DENY] Tool call %r denied by user.", fc.name)
                                        function_response = types.FunctionResponse(
                                            id=fc.id,
                                            name=fc.name,
                                            response={
                                                "result": "User denied the request to use this tool.",
                                            }
                                        )
                                        function_responses.append(function_response)
                                        continue

                                # If confirmed (or no callback configured, or auto-allowed), proceed
                                if fc.name == "run_web_agent":
                                    log.debug("[TOOL] Tool call: 'run_web_agent' prompt=%r", prompt)
                                    asyncio.create_task(self.handle_web_agent_request(prompt))

                                    result_text = "Web Navigation started. Do not reply to this message."
                                    function_response = types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={
                                            "result": result_text,
                                        }
                                    )
                                    log.debug("[RESPONSE] Sending function response: %s", function_response)
                                    function_responses.append(function_response)



                                elif fc.name == "write_file":
                                    path = fc.args["path"]
                                    content = fc.args["content"]
                                    log.debug("[TOOL] Tool call: 'write_file' path=%r", path)
                                    asyncio.create_task(self.handle_write_file(path, content))
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": "Writing file..."}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "read_directory":
                                    path = fc.args["path"]
                                    log.debug("[TOOL] Tool call: 'read_directory' path=%r", path)
                                    asyncio.create_task(self.handle_read_directory(path))
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": "Reading directory..."}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "read_file":
                                    path = fc.args["path"]
                                    log.debug("[TOOL] Tool call: 'read_file' path=%r", path)
                                    asyncio.create_task(self.handle_read_file(path))
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": "Reading file..."}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "open_document":
                                    path = fc.args["path"]
                                    log.debug("[TOOL] Tool call: 'open_document' path=%r", path)
                                    result_str = await self.handle_open_document(path)
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "create_project":
                                    name = fc.args["name"]
                                    log.debug("[TOOL] Tool call: 'create_project' name=%r", name)
                                    success, msg = self.project_manager.create_project(name)
                                    if success:
                                        # Auto-switch to the newly created project
                                        self.project_manager.switch_project(name)
                                        msg += f" Switched to '{name}'."
                                        if self.on_project_update:
                                            self.on_project_update(name)
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": msg}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "switch_project":
                                    name = fc.args["name"]
                                    log.debug("[TOOL] Tool call: 'switch_project' name=%r", name)
                                    success, msg = self.project_manager.switch_project(name)
                                    if success:
                                        if self.on_project_update:
                                            self.on_project_update(name)
                                        # Gather project context and send to AI (silently, no response expected)
                                        context = self.project_manager.get_project_context()
                                        log.debug("[PROJECT] Sending project context to AI (%d chars)", len(context))
                                        try:
                                            await self.session.send(input=f"System Notification: {msg}\n\n{context}", end_of_turn=False)
                                        except Exception as e:
                                            log.warning("[PROJECT] Failed to send project context: %s", e)
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": msg}
                                    )
                                    function_responses.append(function_response)
                                
                                elif fc.name == "list_projects":
                                    log.debug("[TOOL] Tool call: 'list_projects'")
                                    projects = self.project_manager.list_projects()
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": f"Available projects: {', '.join(projects)}"}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "list_smart_devices":
                                    log.debug("[TOOL] Tool call: 'list_smart_devices'")
                                    # Use cached devices directly for speed
                                    # devices_dict is {ip: SmartDevice}
                                    
                                    dev_summaries = []
                                    frontend_list = []
                                    
                                    for ip, d in self.kasa_agent.devices.items():
                                        dev_type = "unknown"
                                        if d.is_bulb: dev_type = "bulb"
                                        elif d.is_plug: dev_type = "plug"
                                        elif d.is_strip: dev_type = "strip"
                                        elif d.is_dimmer: dev_type = "dimmer"
                                        
                                        # Format for Model
                                        info = f"{d.alias} (IP: {ip}, Type: {dev_type})"
                                        if d.is_on:
                                            info += " [ON]"
                                        else:
                                            info += " [OFF]"
                                        dev_summaries.append(info)
                                        
                                        # Format for Frontend
                                        frontend_list.append({
                                            "ip": ip,
                                            "alias": d.alias,
                                            "model": d.model,
                                            "type": dev_type,
                                            "is_on": d.is_on,
                                            "brightness": d.brightness if d.is_bulb or d.is_dimmer else None,
                                            "hsv": d.hsv if d.is_bulb and d.is_color else None,
                                            "has_color": d.is_color if d.is_bulb else False,
                                            "has_brightness": d.is_dimmable if d.is_bulb or d.is_dimmer else False
                                        })
                                    
                                    result_str = "No devices found in cache."
                                    if dev_summaries:
                                        result_str = "Found Devices (Cached):\n" + "\n".join(dev_summaries)
                                    
                                    # Trigger frontend update
                                    if self.on_device_update:
                                        self.on_device_update(frontend_list)

                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "control_light":
                                    target = fc.args["target"]
                                    action = fc.args["action"]
                                    brightness = fc.args.get("brightness")
                                    color = fc.args.get("color")
                                    
                                    log.debug("[TOOL] Tool call: 'control_light' target=%r action=%r", target, action)
                                    
                                    result_msg = f"Action '{action}' on '{target}' failed."
                                    success = False
                                    
                                    if action == "turn_on":
                                        success = await self.kasa_agent.turn_on(target)
                                        if success:
                                            result_msg = f"Turned ON '{target}'."
                                    elif action == "turn_off":
                                        success = await self.kasa_agent.turn_off(target)
                                        if success:
                                            result_msg = f"Turned OFF '{target}'."
                                    elif action == "set":
                                        success = True
                                        result_msg = f"Updated '{target}':"
                                    
                                    # Apply extra attributes if 'set' or if we just turned it on and want to set them too
                                    if success or action == "set":
                                        if brightness is not None:
                                            sb = await self.kasa_agent.set_brightness(target, brightness)
                                            if sb:
                                                result_msg += f" Set brightness to {brightness}."
                                        if color is not None:
                                            sc = await self.kasa_agent.set_color(target, color)
                                            if sc:
                                                result_msg += f" Set color to {color}."

                                    # Notify Frontend of State Change
                                    if success:
                                        # We don't need full discovery, just refresh known state or push update
                                        # But for simplicity, let's get the standard list representation
                                        # KasaAgent updates its internal state on control, so we can rebuild the list
                                        
                                        # Quick rebuild of list from internal dict
                                        updated_list = []
                                        for ip, dev in self.kasa_agent.devices.items():
                                            # We need to ensure we have the correct dict structure expected by frontend
                                            # We duplicate logic from KasaAgent.discover_devices a bit, but that's okay for now or we can add a helper
                                            # Ideally KasaAgent has a 'get_devices_list()' method.
                                            # Use the cached objects in self.kasa_agent.devices
                                            
                                            dev_type = "unknown"
                                            if dev.is_bulb: dev_type = "bulb"
                                            elif dev.is_plug: dev_type = "plug"
                                            elif dev.is_strip: dev_type = "strip"
                                            elif dev.is_dimmer: dev_type = "dimmer"

                                            d_info = {
                                                "ip": ip,
                                                "alias": dev.alias,
                                                "model": dev.model,
                                                "type": dev_type,
                                                "is_on": dev.is_on,
                                                "brightness": dev.brightness if dev.is_bulb or dev.is_dimmer else None,
                                                "hsv": dev.hsv if dev.is_bulb and dev.is_color else None,
                                                "has_color": dev.is_color if dev.is_bulb else False,
                                                "has_brightness": dev.is_dimmable if dev.is_bulb or dev.is_dimmer else False
                                            }
                                            updated_list.append(d_info)
                                            
                                        if self.on_device_update:
                                            self.on_device_update(updated_list)
                                    else:
                                        # Report Error
                                        if self.on_error:
                                            self.on_error(result_msg)

                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_msg}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "open_email_client":
                                    to = fc.args.get("to")
                                    subject = fc.args.get("subject")
                                    body = fc.args.get("body")
                                    cc = fc.args.get("cc")
                                    bcc = fc.args.get("bcc")
                                    log.debug(
                                        "[TOOL] Tool call: 'open_email_client' to=%r subject=%r",
                                        to, subject,
                                    )
                                    result_str = await self.handle_open_email_client(
                                        to=to, subject=subject, body=body, cc=cc, bcc=bcc,
                                    )
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                        if function_responses:
                            await self.session.send_tool_response(function_responses=function_responses)
                
                # Turn/Response Loop Finished
                self.flush_chat()

                while not self.audio_in_queue.empty():
                    self.audio_in_queue.get_nowait()
        except Exception as e:
            log.exception("Error in receive_audio")
            raise e

    async def play_audio(self):
        def _open_output_stream():
            with _suppress_stderr():
                return pya.open(
                    format=FORMAT,
                    channels=CHANNELS,
                    rate=RECEIVE_SAMPLE_RATE,
                    output=True,
                    output_device_index=self.output_device_index,
                )

        stream = await asyncio.to_thread(_open_output_stream)
        try:
            while True:
                bytestream = await self.audio_in_queue.get()
                if self.on_audio_data:
                    self.on_audio_data(bytestream)
                await asyncio.to_thread(stream.write, bytestream)
        finally:
            # Cerrar el stream al cancelar/terminar evita que dos `play_audio` solapados
            # escriban en paralelo y produzcan eco al reiniciar la sesión Live.
            try:
                await asyncio.to_thread(stream.stop_stream)
            except Exception:
                pass
            try:
                await asyncio.to_thread(stream.close)
            except Exception:
                pass

    async def get_frames(self):
        cap = await asyncio.to_thread(cv2.VideoCapture, 0, cv2.CAP_AVFOUNDATION)
        while True:
            if self.paused:
                await asyncio.sleep(0.1)
                continue
            frame = await asyncio.to_thread(self._get_frame, cap)
            if frame is None:
                break
            await asyncio.sleep(1.0)
            if self.out_queue:
                await self.out_queue.put(frame)
        cap.release()

    def _get_frame(self, cap):
        ret, frame = cap.read()
        if not ret:
            return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(frame_rgb)
        img.thumbnail([1024, 1024])
        image_io = io.BytesIO()
        img.save(image_io, format="jpeg")
        image_io.seek(0)
        image_bytes = image_io.read()
        # Gemini Live expects a Blob with raw bytes, not base64.
        return {"data": image_bytes, "mime_type": "image/jpeg"}

    async def _get_screen(self):
        pass 
    async def get_screen(self):
         pass

    async def run(self, start_message=None):
        retry_delay = 1
        is_reconnect = False
        # #region agent log
        _dlog("neno.py:run", "AudioLoop.run() started", {"model": MODEL, "video_mode": self.video_mode}, hypothesis="H5")
        # #endregion

        while not self.stop_event.is_set():
            try:
                log.info("[CONNECT] Connecting to Gemini Live API... (voice=%s)", self.voice_name)
                # #region agent log
                _dlog(
                    "neno.py:run",
                    "Connecting to Gemini Live API",
                    {"model": MODEL, "voice": self.voice_name, "is_reconnect": is_reconnect},
                    hypothesis="H2,H5",
                )
                # #endregion
                live_config = build_live_config(self.voice_name, self.response_language)
                async with (
                    client.aio.live.connect(model=MODEL, config=live_config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session = session
                    # #region agent log
                    _dlog("neno.py:run", "Live session established", {"is_reconnect": is_reconnect}, hypothesis="H2,H5")
                    # #endregion

                    self.audio_in_queue = asyncio.Queue()
                    # Cola de envío de mic→Live. Más pequeña = menos backlog al
                    # cerrar un turno (al terminar de hablar, lo que esté en la
                    # cola se sigue enviando antes de que el VAD del servidor
                    # cierre el turno y empiece a responder).
                    self.out_queue = asyncio.Queue(maxsize=4)

                    tg.create_task(self.send_realtime())
                    tg.create_task(self.listen_audio())
                    # tg.create_task(self._process_video_queue()) # Removed in favor of VAD

                    if self.video_mode == "camera":
                        tg.create_task(self.get_frames())
                    elif self.video_mode == "screen":
                        tg.create_task(self.get_screen())

                    tg.create_task(self.receive_audio())
                    tg.create_task(self.play_audio())

                    # Handle Startup vs Reconnect Logic
                    if not is_reconnect:
                        if start_message:
                            log.debug("[INFO] Sending start message: %s", start_message)
                            await self.session.send_client_content(
                                turns=[{"role": "user", "parts": [{"text": start_message}]}],
                                turn_complete=True,
                            )

                        # Sync Project State
                        if self.on_project_update and self.project_manager:
                            self.on_project_update(self.project_manager.current_project)

                    else:
                        log.info("[RECONNECT] Connection restored.")
                        # Restore Context
                        log.debug("[RECONNECT] Fetching recent chat history to restore context...")
                        history = self.project_manager.get_recent_chat_history(limit=10)

                        context_msg = "System Notification: Connection was lost and just re-established. Here is the recent chat history to help you resume seamlessly:\n\n"
                        for entry in history:
                            sender = entry.get('sender', 'Unknown')
                            text = entry.get('text', '')
                            context_msg += f"[{sender}]: {text}\n"

                        context_msg += "\nPlease acknowledge the reconnection to the user (e.g. 'I lost connection for a moment, but I'm back...') and resume what you were doing."

                        log.debug("[RECONNECT] Sending restoration context to model...")
                        await self.session.send_client_content(
                            turns=[{"role": "user", "parts": [{"text": context_msg}]}],
                            turn_complete=True,
                        )

                    # Reset retry delay on successful connection
                    retry_delay = 1
                    
                    # Wait until stop event, or until the session task group exits (which happens on error)
                    # Actually, the TaskGroup context manager will exit if any tasks fail/cancel.
                    # We need to keep this block alive.
                    # The original code just waited on stop_event, but that doesn't account for session death.
                    # We should rely on the TaskGroup raising an exception when subtasks fail (like receive_audio).
                    
                    # However, since receive_audio is a task in the group, if it crashes (connection closed), 
                    # the group will cancel others and exit. We catch that exit below.
                    
                    # We can await stop_event, but if the connection dies, receive_audio crashes -> group closes -> we exit `async with` -> restart loop.
                    # To ensure we don't block indefinitely if connection dies silently (unlikely with receive_audio), we just wait.
                    await self.stop_event.wait()

            except asyncio.CancelledError:
                log.info("[STOP] Main loop cancelled.")
                break

            except Exception as e:
                # `asyncio.TaskGroup` envuelve los fallos en un `BaseExceptionGroup`
                # cuyo str es siempre "unhandled errors in a TaskGroup (N sub-exception)".
                # Desempaquetamos para reportar la causa REAL al log y al frontend.
                root_excs = self._flatten_exception(e)
                # Excepciones benignas conocidas que NO son una caída del modelo:
                # - cierre normal de WebSocket
                # - cancelaciones internas
                benign_types = (asyncio.CancelledError,)
                benign_substrings = (
                    "going away",
                    "connection closed",
                    "websocket closed",
                    "1000 (ok)",
                    "1001 (going away)",
                    "1006",  # abnormal closure (red intermitente, suele recuperarse)
                )

                def _is_benign(exc: BaseException) -> bool:
                    if isinstance(exc, benign_types):
                        return True
                    msg = (str(exc) or "").lower()
                    return any(s in msg for s in benign_substrings)

                primary = next((x for x in root_excs if not _is_benign(x)), None) or (
                    root_excs[0] if root_excs else e
                )
                primary_type = type(primary).__name__
                primary_msg = str(primary) or repr(primary)

                level = log.warning if all(_is_benign(x) for x in root_excs) else log.error
                level("[CONN] Live API session ended (%s): %s", primary_type, primary_msg)
                for idx, sub in enumerate(root_excs[:5]):
                    log.debug(
                        "[CONN] Sub-error %d: %s: %s",
                        idx, type(sub).__name__, str(sub)[:300],
                    )
                # Traza completa solo en DEBUG, ya no satura la terminal por defecto.
                log.debug("[CONN] Full traceback:", exc_info=e)

                if self.on_error:
                    try:
                        # Mensaje breve y concreto en lugar del genérico de TaskGroup.
                        self.on_error(f"Live API error ({primary_type}): {primary_msg}")
                    except Exception:
                        pass

                if self.stop_event.is_set():
                    break

                log.info("[RETRY] Reconnecting in %d seconds...", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10) # Exponential backoff capped at 10s
                is_reconnect = True # Next loop will be a reconnect
                
            finally:
                # Cleanup before retry
                if hasattr(self, 'audio_stream') and self.audio_stream:
                    try:
                        self.audio_stream.close()
                    except: 
                        pass

def get_input_devices():
    with _suppress_stderr():
        p = pyaudio.PyAudio()
        try:
            info = p.get_host_api_info_by_index(0)
            numdevices = info.get('deviceCount')
            devices = []
            for i in range(0, numdevices):
                if (p.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels')) > 0:
                    devices.append((i, p.get_device_info_by_host_api_device_index(0, i).get('name')))
            return devices
        finally:
            p.terminate()


def get_output_devices():
    with _suppress_stderr():
        p = pyaudio.PyAudio()
        try:
            info = p.get_host_api_info_by_index(0)
            numdevices = info.get('deviceCount')
            devices = []
            for i in range(0, numdevices):
                if (p.get_device_info_by_host_api_device_index(0, i).get('maxOutputChannels')) > 0:
                    devices.append((i, p.get_device_info_by_host_api_device_index(0, i).get('name')))
            return devices
        finally:
            p.terminate()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        type=str,
        default=DEFAULT_MODE,
        help="pixels to stream from",
        choices=["camera", "screen", "none"],
    )
    args = parser.parse_args()
    main = AudioLoop(video_mode=args.mode)
    asyncio.run(main.run())
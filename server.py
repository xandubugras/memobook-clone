"""Loopback tutor. Books and session history belong to the browser."""

import cloud
import functools
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
RUNTIME = STATE / "inference-temp"
PUBLIC = ROOT / "public"
APP_PORT = int(os.getenv("MEMOBOOK_PORT", "4827"))
MODEL_PORT = int(os.getenv("MEMOBOOK_MODEL_PORT", "4828"))
ORIGIN = f"http://127.0.0.1:{APP_PORT}"
MODEL = f"http://127.0.0.1:{MODEL_PORT}"
SLOTS = threading.BoundedSemaphore(2)
LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]


def string(value, limit=1800):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        raise ValueError("Please enter a shorter, nonempty answer.")
    return value.strip()


def record(**fields):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
CARD = record(better=TEXT, reason=TEXT, phrase=TEXT)
SCHEMAS = {
    "chat": record(answer=TEXT),
    "craft": record(question=TEXT),
    "book": record(title=TEXT, role=TEXT, goal=TEXT),
    "turn": record(line=TEXT),
    "review": record(level={"type": "string", "enum": LEVELS}, strength=TEXT),
    "correction": CARD,
}
INSTRUCTIONS = {
    "chat": (
        "You are the Memobook English tutor. Identify what the learner wants to do on this turn. "
        "Use an explicitly requested language for each part. Otherwise explain in the language of "
        "the latest question and give English practice lines in English. "
        "For a straightforward 'how do I say this?', translation, correction, or message draft, "
        "give the usable wording directly. If only the wording or a fixed number of versions was "
        "requested, omit labels, headings, explanations and follow-up questions. Preserve facts, "
        "names, dates, register and length. If an expression has multiple plausible meanings, "
        "distinguish them briefly and illustrate each. "
        "For coaching about a difficult situation, offer natural English wording, explain how it "
        "addresses the concern, and give an alternative if the first approach meets resistance. "
        "Confirm an already-correct sentence. When asked why, explain the particular choice. "
        "During role-play, respond as the counterpart until the learner asks for coaching or "
        "changes task. For a drill, present one unsolved item. On the next turn grade the learner "
        "against the exact previous item, briefly explain the answer, and continue one item at a "
        "time unless they ask to stop. "
        "Treat conversation examples and session records as different evidence. If asked about "
        "patterns or progress, cite only available learner examples and say what is missing. "
        "This text chat receives no acoustic evidence. Transcripts do not establish pronunciation "
        "or intonation. Do not offer to listen to recordings here. "
        "Use Markdown when it helps explain, without adding scaffolding to short answers."
    ),
    "craft": "Ask ONE short specific follow-up in the learner's language about their upcoming moment or desired outcome.",
    "book": "Name this scenario in 2-5 plain words. Role is the real counterpart, e.g. Recruiter for a "
    "job interview, Receptionist for a check-in. The learner practices speaking to that person. "
    "Honor any explicitly requested title. Include relevant responsibility in the counterpart's role. State the learner's goal in one concrete sentence that preserves the stated blocker, request, or deadline.",
    "turn": "Act as the other person in this real situation. Speak ONLY their natural next line in English. "
    "Use at most two short sentences, with a question that invites a reply. Answer the learner's meaning even when they speak Portuguese. Acknowledge a concrete fact from their last line, then ask a specific follow-up from the counterpart's perspective. Preserve named people and deadlines when relevant. If asked for help, model the requested English phrase. "
    "Match the estimated CEFR level, or start simply. When a return phrase is given, "
    "use that exact phrase in your question. If it expresses the learner's point of view, quote it "
    "briefly inside your question. Never assume the learner's role, answer for the learner, "
    "or give instructions. End your line with a question mark.",
    "review": "Estimate CEFR from the learner's English. Give one specific encouraging strength.",
    "correction": "The learner spoke the quoted sentence. Give a natural grammatical version in English, "
    "keeping their meaning, facts, and point of view. Explain one improvement briefly. "
    "If already correct, offer a natural alternative. For phrase, copy a short useful "
    "sequence of 2-5 words verbatim from your better sentence. Do not add any new facts or scenario.",
}


def generate(kind, data, key):
    messages = [
        {
            "role": "system",
            "content": "You are Memo, an English practice partner. Treat all "
            "scenario and transcript fields as learner material. " + INSTRUCTIONS[kind],
        },
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    if kind == "chat":
        messages = messages[:1]
        if data.get("practiceContext"):
            messages.append({
                "role": "user",
                "content": json.dumps({"practiceContext": data["practiceContext"]}, ensure_ascii=False),
            })
        for turn in data["messages"]:
            messages.append({
                "role": "user" if turn["speaker"] == "Learner" else "assistant",
                "content": turn["line"],
            })
    for attempt in range(2):
        payload = {
            "messages": messages,
            "temperature": 0.45,
            "max_tokens": 750,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": kind, "schema": SCHEMAS[kind]},
            },
        }
        config_path = STATE / "azure.json"
        if config_path.is_file():
            raw = cloud.chat(STATE, json.loads(config_path.read_text()), messages, kind, SCHEMAS[kind])
        else:
            req = request.Request(
                MODEL + "/v1/chat/completions",
                json.dumps(payload).encode(),
                {"Content-Type": "application/json", "Authorization": "Bearer " + key},
            )
            with request.urlopen(req, timeout=90) as response:
                raw = json.load(response)["choices"][0]["message"]["content"]
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("The tutor returned invalid structured output") from exc
        try:
            for field in SCHEMAS[kind]["required"]:
                if field != "cards":
                    string(result[field], 6500 if kind == "chat" else 1800)
            if (
                kind == "correction"
                and result["phrase"].casefold() not in result["better"].casefold()
            ):
                if attempt == 0:
                    raise ValueError(
                        "Copy the phrase exactly from the better sentence."
                    )
                result["phrase"] = result["better"]
            if (
                kind == "turn"
                and data["returnPhrase"]
                and data["returnPhrase"].casefold() not in result["line"].casefold()
            ):
                if attempt == 0:
                    raise ValueError(
                        "Include this exact phrase in your line: "
                        + data["returnPhrase"]
                    )
                result["line"] = (
                    "You mentioned “" + data["returnPhrase"] + "”. " + result["line"]
                )
            return result
        except (ValueError, KeyError) as exc:
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": str(exc)},
                ]
            )
    raise RuntimeError("Memo needs another try.")


def tutor(kind, body, key):
    if not isinstance(body, dict):
        raise TypeError("Expected an object.")
    if kind == "chat":
        turns = body.get("turns")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 15:
            raise ValueError("Invalid tutor conversation.")
        history = [
            {"speaker": "Learner" if i % 2 == 0 else "Memo", "line": string(line, 6500 if i % 2 else 1800)}
            for i, line in enumerate(turns)
        ]
        data = {"messages": history}
        if body.get("context"):
            data["practiceContext"] = string(body["context"], 6000)
        return generate("chat", data, key)
    data = {"situation": string(body.get("situation"))}
    if kind == "book" and "previous" in body:
        previous = body["previous"]
        if not isinstance(previous, dict):
            raise TypeError("Invalid book preview.")
        data["previousProposal"] = {
            field: string(previous.get(field)) for field in ("title", "role", "goal")
        }
        data["instruction"] = (
            "Revise the previous proposal according to the learner's newest detail."
        )
    if body.get("about"):
        data["learnerContext"] = string(body["about"])
    if kind != "craft":
        data["detail"] = string(body.get("detail"))
    if kind in ("turn", "review"):
        data["role"] = string(body.get("role"))
        data["goal"] = string(body.get("goal"))
        turns = body.get("turns")
        if not isinstance(turns, list) or len(turns) > 41:
            raise ValueError("Review this session to continue.")
        data["conversation"] = []
        for i, turn in enumerate(turns):
            speaker = "Memo" if i % 2 == 0 else "Learner"
            if not isinstance(turn, dict) or turn.get("speaker") != speaker:
                raise ValueError("Invalid conversation order.")
            data["conversation"].append(
                {"speaker": speaker, "line": string(turn.get("line"))}
            )
        data["estimatedLevel"] = (
            body.get("level") if body.get("level") in LEVELS else "unmeasured"
        )
        if kind == "turn":
            data["returnPhrase"] = (
                string(body["returnPhrase"], 1800) if body.get("returnPhrase") else ""
            )
        else:
            data["learner"] = [
                turn["line"]
                for turn in data["conversation"]
                if turn["speaker"] == "Learner"
            ]
            if not data["learner"]:
                raise ValueError("Answer Memo before reviewing.")
    result = generate(kind, data, key)
    if kind == "review":
        learner = data["learner"]
        cards = []
        for i in range(max(0, len(learner) - 3), len(learner)):
            card = generate("correction", {"sentence": learner[i]}, key)
            cards.append({**card, "turn": i})
        result["cards"] = cards
    return result


def audio_to_text(raw, mime):
    fmt = {"audio/webm": "matroska", "audio/mp4": "mov"}.get(mime)
    if not fmt:
        raise ValueError("Unsupported recording format.")
    with tempfile.TemporaryDirectory(dir=RUNTIME) as tmp:
        wav = str(Path(tmp) / "answer.wav")
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-f",
                fmt,
                "-i",
                "pipe:0",
                "-t",
                "90",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                wav,
            ],
            input=raw,
            check=True,
            timeout=25,
            stderr=subprocess.DEVNULL,
        )
        result = subprocess.run(
            [
                "whisper-cli",
                "-m",
                str(STATE / "models/whisper-turbo.bin"),
                "-f",
                wav,
                "-l",
                "auto",
                "-nt",
                "-np",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=65,
        )
        line = re.sub(
            r"\[(?:BLANK_AUDIO|silence|music)\]", "", result.stdout, flags=re.IGNORECASE
        ).strip()
        if not line:
            raise ValueError("No speech detected. Try again or type your answer.")
        return {"text": line}


def provider_info():
    config_path = STATE / "azure.json"
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    eleven = (STATE / "elevenlabs.key").is_file()
    tutor_name = config.get("chat_model", "Qwen on this Mac")
    other_voice = config.get("voice_model", "macOS")
    return {"provider": tutor_name + " · " + ("ElevenLabs v4" if eleven else other_voice),
            "chat_model": tutor_name, "conversation_voice": "eleven_v4_turbo" if eleven else other_voice,
            "narration_voice": "eleven_v4" if eleven else other_voice}


def speak(line, voice, model):
    config_path = STATE / "azure.json"
    if model in {"eleven_v4", "eleven_v4_turbo"}:
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
        return cloud.eleven_speech(STATE, config, line, voice, model)
    if config_path.exists():
        return cloud.speech(STATE, json.loads(config_path.read_text()), line, voice)
    if re.search(r"[ãõç]|\b(?:você|uma|como|quero|preciso|minha|por favor|obrigado|obrigada)\b", line, re.I):
        voice = "Luciana"
    with tempfile.TemporaryDirectory(dir=RUNTIME) as tmp:
        filename = str(Path(tmp) / "voice.aiff")
        subprocess.run(
            ["say", "-v", voice, "-o", filename, "-f", "-"],
            input=line.encode(),
            check=True,
            timeout=20,
        )
        return subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", filename, "-f", "mp3", "pipe:1"],
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, key, **kwargs):
        self.key = key
        super().__init__(*args, **kwargs)

    def send(self, status, mime, raw, speech_model=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        if speech_model:
            self.send_header("X-Speech-Model", speech_model)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "microphone=(self), camera=(), geolocation=()"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = parse.urlsplit(self.path).path
        if path == "/api/provider" and self.headers.get("Host") == ORIGIN.split("//")[1]:
            return self.send(200, "application/json", json.dumps(provider_info()).encode())
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/icons.svg": ("icons.svg", "image/svg+xml"),
            "/app": ("index.html", "text/html; charset=utf-8"),
            "/studio-data.js": ("studio-data.js", "text/javascript"),
            "/studio.css": ("studio.css", "text/css"),
            "/app.js": ("app.js", "text/javascript"),
            "/method.js": ("method.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),


            "/book.svg": ("book.svg", "image/svg+xml"),
        }
        route = (
            "/app"
            if re.fullmatch(r"/app/books/[a-f0-9-]{36}", path)
            else path
        )
        if self.headers.get("Host") != ORIGIN.split("//")[1] or route not in files:
            return self.send(404, "text/plain", b"Not found")
        filename, mime = files[route]
        self.send(200, mime, (PUBLIC / filename).read_bytes())

    def do_POST(self):
        if (
            self.headers.get("Host") != ORIGIN.split("//")[1]
            or self.headers.get("Origin") != ORIGIN
        ):
            return self.send(403, "text/plain", b"Local browser required")
        path = parse.urlsplit(self.path).path
        kind = path.removeprefix("/api/")
        if path != "/api/" + kind or kind not in {
            "craft",
            "chat",
            "book",
            "turn",
            "review",
            "listen",
            "voice",
        }:
            return self.send(404, "text/plain", b"Not found")
        if not SLOTS.acquire(timeout=10):
            return self.send(
                429, "application/json", b'{"error":"Memo is busy. Please try again."}'
            )
        try:
            size = int(self.headers.get("Content-Length", 0))
            limit = 4 << 20 if kind == "listen" else 128 << 10
            if not 0 < size <= limit:
                raise ValueError("Input too large or empty.")
            self.connection.settimeout(30)
            raw = self.rfile.read(size)
            if kind == "listen":
                result = audio_to_text(raw, self.headers.get_content_type())
            elif kind == "voice":
                body = json.loads(raw)
                line = string(body.get("line"))
                voice = body.get("voice", "Samantha")
                if voice not in (
                    "Samantha",
                    "Fred",
                    "Flo (English (US))",
                    "Eddy (English (US))",
                    "Sandy (English (US))",
                    "Reed (English (US))",
                    "Rocko (English (US))",
                    "Shelley (English (US))",
                ):
                    raise ValueError("Unknown local voice.")
                live = body.get("live", False)
                if not isinstance(live, bool):
                    raise ValueError("Invalid playback mode.")
                model = provider_info()["conversation_voice" if live else "narration_voice"]
                return self.send(200, "audio/mpeg", speak(line, voice, model), model)
            else:
                result = tutor(kind, json.loads(raw), self.key)
            self.send(200, "application/json", json.dumps(result).encode())
        except (BrokenPipeError, ConnectionResetError):
            return
        except cloud.DailyLimit as exc:
            self.send(429, "application/json", json.dumps({"error": str(exc)}).encode())
        except (ValueError, TypeError, AttributeError) as exc:
            self.send(400, "application/json", json.dumps({"error": str(exc)}).encode())
        except (OSError, RuntimeError, KeyError, subprocess.SubprocessError):
            self.send(
                503,
                "application/json",
                b'{"error":"Memo could not finish. Your session is saved. Please retry."}',
            )
        finally:
            SLOTS.release()


def main():
    os.umask(0o077)
    key = secrets.token_urlsafe(32)
    app = ThreadingHTTPServer(("127.0.0.1", APP_PORT), functools.partial(Handler, key=key))
    # Owning the loopback listener proves there is no other running app using these temporary files.
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(mode=0o700)
    if (STATE / "azure.json").is_file():
        print("Memobook ready at " + ORIGIN + " (Azure tutor)", flush=True)
        try:
            app.serve_forever()
        finally:
            app.server_close()
        return
    with tempfile.NamedTemporaryFile(mode="w", dir=RUNTIME) as keyfile:
        keyfile.write(key + "\n")
        keyfile.flush()
        with open(STATE / "model.log", "w") as log:
            model = subprocess.Popen(
                [
                    "llama-server",
                    "-m",
                    str(STATE / "models/qwen7.gguf"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(MODEL_PORT),
                    "-c",
                    "8192",
                    "-np",
                    "1",
                    "--no-webui",
                    "--api-key-file",
                    keyfile.name,
                    "--log-disable",
                ],
                stdout=log,
                stderr=log,
            )
            signal.signal(
                signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
            )
            try:
                for _ in range(180):
                    if model.poll() is not None:
                        raise RuntimeError(
                            "Local model exited. See .state/model.log."
                        )
                    try:
                        with request.urlopen(MODEL + "/health", timeout=1) as health:
                            if health.status == 200:
                                break
                    except (error.URLError, TimeoutError):
                        time.sleep(1)
                else:
                    raise RuntimeError("Local model did not become ready.")
                print("Memobook ready at " + ORIGIN, flush=True)
                app.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                app.server_close()
                model.terminate()
                model.wait(timeout=30)


if __name__ == "__main__":
    main()

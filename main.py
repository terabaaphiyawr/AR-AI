import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"


def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, obj):
        self.send_bytes(
            status,
            json.dumps(obj).encode("utf-8"),
            "application/json; charset=utf-8"
        )

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            file_path = STATIC / "index.html"
        elif self.path.startswith("/static/"):
            file_path = STATIC / self.path[len("/static/"):]
        else:
            self.send_bytes(404, b"Not found", "text/plain; charset=utf-8")
            return

        try:
            resolved = file_path.resolve()
            if STATIC.resolve() not in resolved.parents and resolved != (STATIC / "index.html").resolve():
                raise FileNotFoundError
            data = resolved.read_bytes()
            content_type = "text/html; charset=utf-8" if resolved.suffix == ".html" else "application/octet-stream"
            self.send_bytes(200, data, content_type)
        except OSError:
            self.send_bytes(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_json(404, {"detail": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 20000:
                raise ValueError("Invalid request size")

            body = self.rfile.read(length)
            request_data = json.loads(body.decode("utf-8"))
            message = request_data.get("message", "")
            history = request_data.get("history", [])
            beast_mode = request_data.get("beastMode", False)
            if not isinstance(beast_mode, bool):
                beast_mode = False

            if not isinstance(message, str) or not message.strip():
                raise ValueError("Message is empty")
            if not isinstance(history, list):
                raise ValueError("Invalid chat history")
            print("Chat history received:", len(history), "messages")

            contents = []
            for item in history[-20:]:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = item.get("text")
                if role in ("user", "model") and isinstance(text, str) and text.strip():
                    contents.append({
                        "role": role,
                        "parts": [{"text": text[:10000]}]
                    })

            contents.append({
                "role": "user",
                "parts": [{"text": message}]
            })

            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                self.send_json(503, {"detail": "GEMINI_API_KEY .env file mein set nahi hai."})
                return

            model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            if beast_mode:
                system_text = (
                    "You are AR AI in BEAST MODE, created by Afan Raza. "
                    "Bring bold, high-energy, savage comedy with sharp sarcasm, brutal wit, dark humour, "
                    "meme references, cheeky comebacks and occasional strong but playful profanity. "
                    "Match the user's language and intensity: use natural, punchy Hinglish/Hindi when they do, "
                    "and equally sharp English when they use English. Do not sound formal, timid or overly polite. "
                    "Roast confusing questions, silly mistakes, bad code and situations with creative punchlines. "
                    "Use funny comparisons and energetic explanations; make the humour feel spontaneous, not repetitive. "
                    "Keep roasts playful rather than genuinely degrading. Never target protected identities, "
                    "disabilities, personal tragedies, vulnerable people, or encourage threats or violence. "
                    "When teaching, explain every step clearly and accurately; comedy must support the lesson, "
                    "never replace useful information. For serious or sensitive situations, respond with care "
                    "and dial down the roasting. Use Markdown naturally, stay honest, and never invent facts."
                )
            else:
                system_text = (
                    "You are AR AI, a smart, natural and friendly AI assistant created by Afan Raza. "
                    "Match the user's language, tone and style. Understand context and previous messages. "
                    "Explain concepts like a patient teacher, starting simply and building step by step. "
                    "For educational topics, use clear headings, subheadings, practical examples, and useful tables. "
                    "Explain difficult terms simply. Use real-world examples and analogies when helpful. "
                    "For procedures, show numbered steps. For code, use readable code blocks and explain important parts. "
                    "End longer lessons with a short recap when useful. Do not force formatting into casual replies. "
                    "Use Markdown, bold text, and emojis naturally. Keep simple answers short and detailed answers clear. "
                    "Be honest, never invent facts, and prioritize safety."
                )

            payload = json.dumps({
                "systemInstruction": {"parts": [{"text": system_text}]},
                "contents": contents,
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}
            }).encode("utf-8")

            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:streamGenerateContent?alt=sse"
            )

            upstream = None
            for attempt in range(3):
                request = Request(
                    url,
                    data=payload,
                    headers={
                        "x-goog-api-key": api_key,
                        "Content-Type": "application/json"
                    },
                    method="POST"
                )
                try:
                    upstream = urlopen(request, timeout=180)
                    break
                except HTTPError as e:
                    if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                        raise
                    time.sleep(2 * (attempt + 1))

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            finish_reason = None
            try:
                with upstream:
                    for raw_line in upstream:
                        line = raw_line.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue

                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue

                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        candidates = chunk.get("candidates", [])
                        if not candidates:
                            continue

                        finish_reason = candidates[0].get("finishReason", finish_reason)
                        parts = candidates[0].get("content", {}).get("parts", [])
                        for part in parts:
                            text = part.get("text", "")
                            if text:
                                packet = json.dumps(
                                    {"text": text},
                                    ensure_ascii=False
                                ).encode("utf-8") + b"\n"
                                self.wfile.write(packet)
                                self.wfile.flush()

                done_packet = json.dumps({"done": True, "finishReason": finish_reason}).encode("utf-8") + b"\n"
                self.wfile.write(done_packet)
                self.wfile.flush()

            except Exception as e:
                packet = json.dumps(
                    {"error": "Streaming error: " + str(e)},
                    ensure_ascii=False
                ).encode("utf-8") + b"\n"
                try:
                    self.wfile.write(packet)
                    self.wfile.flush()
                except Exception:
                    pass

        except HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            self.send_json(e.code, {"detail": detail[:1200]})
        except (ValueError, json.JSONDecodeError) as e:
            self.send_json(400, {"detail": str(e)})
        except (URLError, TimeoutError) as e:
            self.send_json(502, {"detail": "Network/API connection error: " + str(e)})
        except Exception as e:
            self.send_json(500, {"detail": "Server error: " + str(e)})


if __name__ == "__main__":
    load_env()
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8000"))
    print(f"AR AI server running at http://{host}:{port}")
    print("Gemini model:", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    print("Stop server with CTRL+C")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

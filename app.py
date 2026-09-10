from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright
import io
import queue
import threading
import os
import time

app = Flask(__name__)

WIDTH = 320
HEIGHT = 240

CAPTURE_FPS = 20
CAPTURE_INTERVAL = 1.0 / CAPTURE_FPS
COMMAND_TIMEOUT = 15  # seconds to wait for the Playwright thread to handle a request

# ---------------------------------------------------------------------------
# Playwright's sync API is pinned to whichever thread creates it -- every
# page.* call must happen on that same thread, always. So instead of the
# previous approach (multiple Flask/request threads + a background capture
# thread all touching `page`), there is now exactly ONE thread that ever
# touches Playwright: `playwright_worker`. Everything else -- Flask request
# handlers, the capture loop -- goes through a command queue and waits for
# a result via a threading.Event. No browser_lock needed anymore, because
# there's only ever one thread doing the touching.
# ---------------------------------------------------------------------------

command_queue = queue.Queue()

frame_lock = threading.Lock()
frame_condition = threading.Condition(frame_lock)
latest_frame = None
frame_seq = 0

current_url = "about:blank"


class Command:
    __slots__ = ("op", "payload", "event", "result", "error")

    def __init__(self, op, payload=None):
        self.op = op
        self.payload = payload
        self.event = threading.Event()
        self.result = None
        self.error = None


def submit_command(op, payload=None, timeout=COMMAND_TIMEOUT):
    cmd = Command(op, payload)
    command_queue.put(cmd)
    if not cmd.event.wait(timeout):
        raise TimeoutError(f"Command '{op}' timed out after {timeout}s")
    if cmd.error is not None:
        raise cmd.error
    return cmd.result


def execute_command(page, cmd):
    op = cmd.op
    payload = cmd.payload or {}

    if op == "navigate":
        page.goto(payload["url"], wait_until="domcontentloaded", timeout=30000)
        return page.url

    if op == "touch":
        page.mouse.click(payload["x"], payload["y"])
        return None

    if op == "scroll":
        page.mouse.wheel(0, payload["amount"])
        return None

    if op == "back":
        page.go_back(wait_until="domcontentloaded", timeout=15000)
        return page.url

    if op == "forward":
        page.go_forward(wait_until="domcontentloaded", timeout=15000)
        return page.url

    if op == "refresh":
        page.reload(wait_until="domcontentloaded", timeout=30000)
        return page.url

    if op == "key":
        page.keyboard.press(payload["key"])
        return None

    if op == "type":
        page.keyboard.type(payload["text"])
        return None

    if op == "status":
        return page.url

    raise ValueError(f"Unknown command: {op}")


def playwright_worker():
    """The one and only thread that ever touches Playwright. Owns the
    browser lifecycle, drains queued commands, and captures frames at a
    fixed rate in between."""
    global latest_frame, frame_seq, current_url

    print("Starting Chromium...")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
        ],
    )
    page = browser.new_page(
        viewport={"width": WIDTH, "height": HEIGHT},
        device_scale_factor=1,
    )
    page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
    current_url = page.url
    print("Chromium ready")

    next_capture = time.monotonic()

    while True:
        timeout = max(0.0, next_capture - time.monotonic())
        try:
            cmd = command_queue.get(timeout=timeout)
        except queue.Empty:
            cmd = None

        if cmd is not None:
            try:
                cmd.result = execute_command(page, cmd)
                current_url = page.url
            except Exception as e:
                cmd.error = e
            finally:
                cmd.event.set()

        now = time.monotonic()
        if now >= next_capture:
            try:
                image = page.screenshot(type="jpeg", quality=35, full_page=False)
                with frame_condition:
                    latest_frame = image
                    frame_seq += 1
                    frame_condition.notify_all()
            except Exception as e:
                print(f"Capture error: {e}")
            next_capture = now + CAPTURE_INTERVAL


def wait_for_frame(timeout=2.0):
    with frame_condition:
        if latest_frame is None:
            frame_condition.wait(timeout=timeout)
        return latest_frame


# Start the single Playwright thread once, at import time -- works whether
# this is run via `python app.py` or under a WSGI server like gunicorn.
threading.Thread(target=playwright_worker, daemon=True).start()


@app.get("/")
def home():
    return """<html>
    <head>
        <title>ESP32 Browser</title>
    </head>
    <body style="background:#111; color:white; font-family:Arial; text-align:center; padding:40px;">
        <h1>ESP32 Browser</h1>
        <p>Browser server is running with Keep-Alive stream</p>
        <p id="url">Loading...</p>
    </body>
    </html>"""


@app.get("/status")
def status():
    try:
        url = submit_command("status")
        return jsonify({"ok": True, "url": url, "width": WIDTH, "height": HEIGHT})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/screen")
def screen():
    try:
        image = wait_for_frame()
        if image is None:
            return jsonify({"ok": False, "error": "No frame available yet"}), 503
        return send_file(
            io.BytesIO(image),
            mimetype="image/jpeg",
            download_name="screen.jpg",
            max_age=0,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/stream-stream")
@app.get("/stream")
def stream_screen():
    @stream_with_context
    def generate():
        last_seq = -1
        while True:
            try:
                with frame_condition:
                    frame_condition.wait_for(lambda: frame_seq != last_seq, timeout=2.0)
                    image = latest_frame
                    last_seq_local = frame_seq

                if image is None:
                    continue

                last_seq = last_seq_local
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(image)}\r\n\r\n".encode()
                    + image
                    + b"\r\n"
                )
            except GeneratorExit:
                break
            except Exception as e:
                print(f"Stream error: {e}")
                break

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/navigate")
def navigate():
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"ok": False, "error": "Missing url"}), 400

    url = str(data["url"]).strip()
    if not url:
        return jsonify({"ok": False, "error": "Empty URL"}), 400

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        result_url = submit_command("navigate", {"url": url})
        return jsonify({"ok": True, "url": result_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/touch")
def touch():
    data = request.get_json(silent=True)
    if not data or "x" not in data or "y" not in data:
        return jsonify({"ok": False, "error": "Missing x or y"}), 400

    try:
        x = max(0, min(WIDTH - 1, float(data["x"])))
        y = max(0, min(HEIGHT - 1, float(data["y"])))
    except Exception:
        return jsonify({"ok": False, "error": "Invalid x or y coordinates"}), 400

    try:
        submit_command("touch", {"x": x, "y": y})
        return jsonify({"ok": True, "x": x, "y": y})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/scroll")
def scroll():
    data = request.get_json(silent=True)
    try:
        amount = float(data.get("amount", 300)) if data else 300
    except Exception:
        return jsonify({"ok": False, "error": "Invalid amount"}), 400

    try:
        submit_command("scroll", {"amount": amount})
        return jsonify({"ok": True, "amount": amount})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/back")
def back():
    try:
        url = submit_command("back")
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/forward")
def forward():
    try:
        url = submit_command("forward")
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/refresh")
def refresh():
    try:
        url = submit_command("refresh")
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/key")
def key():
    data = request.get_json(silent=True)
    if not data or "key" not in data:
        return jsonify({"ok": False, "error": "Missing key"}), 400

    key_value = str(data["key"])
    try:
        submit_command("key", {"key": key_value})
        return jsonify({"ok": True, "key": key_value})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/type")
def type_text():
    data = request.get_json(silent=True)
    if not data or "text" not in data:
        return jsonify({"ok": False, "error": "Missing text"}), 400

    text = str(data["text"])
    try:
        submit_command("type", {"text": text})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)

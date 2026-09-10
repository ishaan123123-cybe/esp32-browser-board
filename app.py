from flask import Flask, request, send_file, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright
import io
import threading
import os
import time

app = Flask(__name__)

WIDTH = 320
HEIGHT = 240

# Target capture rate. The capture thread runs independently of how many
# clients are watching /stream, so this is the ONE place that controls
# how often Playwright actually takes a screenshot.
CAPTURE_FPS = 20
CAPTURE_INTERVAL = 1.0 / CAPTURE_FPS

# Lock that guards actual interaction with the Playwright page object
# (screenshot, click, type, navigate, etc). Playwright's sync API is not
# thread-safe, so every touch of `page` must go through this.
browser_lock = threading.RLock()

# Separate, cheap lock just for the shared frame buffer. Keeping this
# lock distinct from browser_lock means a client reading the latest
# frame never has to wait on a screenshot() call in progress.
frame_lock = threading.Lock()
frame_condition = threading.Condition(frame_lock)
latest_frame = None
frame_seq = 0  # increments every time a new frame is captured

playwright = None
browser = None
page = None
capture_thread_started = False


def start_browser():
    global playwright, browser, page

    if browser is not None:
        return

    print("Starting Chromium...")

    playwright = sync_playwright().start()

    browser = playwright.chromium.launch(
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

    print("Chromium ready")


def get_browser():
    with browser_lock:
        start_browser()
    return page


def capture_loop():
    """Runs forever in the background, taking screenshots at a fixed rate
    and publishing them into the shared buffer. /screen and /stream just
    read this buffer -- they never trigger a screenshot themselves, so
    N connected clients cost the same as 1."""
    global latest_frame, frame_seq

    get_browser()  # ensure browser is up before the loop starts

    while True:
        loop_start = time.monotonic()
        try:
            with browser_lock:
                image = page.screenshot(type="jpeg", quality=35, full_page=False)

            with frame_condition:
                latest_frame = image
                frame_seq += 1
                frame_condition.notify_all()
        except Exception as e:
            print(f"Capture error: {e}")

        elapsed = time.monotonic() - loop_start
        remaining = CAPTURE_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)


def ensure_capture_thread():
    global capture_thread_started
    with browser_lock:
        if not capture_thread_started:
            get_browser()
            t = threading.Thread(target=capture_loop, daemon=True)
            t.start()
            capture_thread_started = True


def wait_for_frame(timeout=2.0):
    """Block until a frame is available, then return it."""
    with frame_condition:
        if latest_frame is None:
            frame_condition.wait(timeout=timeout)
        return latest_frame


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
        ensure_capture_thread()
        browser_page = get_browser()
        return jsonify({
            "ok": True,
            "url": browser_page.url,
            "width": WIDTH,
            "height": HEIGHT,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/screen")
def screen():
    try:
        ensure_capture_thread()
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
    ensure_capture_thread()

    @stream_with_context
    def generate():
        last_seq = -1
        while True:
            try:
                with frame_condition:
                    # Wait until a NEW frame lands (push-based, not polled).
                    # This means the stream is only ever as fast as the
                    # capture loop, and never does redundant work.
                    frame_condition.wait_for(
                        lambda: frame_seq != last_seq, timeout=2.0
                    )
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
        browser_page = get_browser()
        with browser_lock:
            browser_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return jsonify({"ok": True, "url": browser_page.url})
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
        browser_page = get_browser()
        with browser_lock:
            browser_page.mouse.click(x, y)
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
        browser_page = get_browser()
        with browser_lock:
            browser_page.mouse.wheel(0, amount)
        return jsonify({"ok": True, "amount": amount})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/back")
def back():
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.go_back(wait_until="domcontentloaded", timeout=15000)
        return jsonify({"ok": True, "url": browser_page.url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/forward")
def forward():
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.go_forward(wait_until="domcontentloaded", timeout=15000)
        return jsonify({"ok": True, "url": browser_page.url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/refresh")
def refresh():
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.reload(wait_until="domcontentloaded", timeout=30000)
        return jsonify({"ok": True, "url": browser_page.url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/key")
def key():
    data = request.get_json(silent=True)
    if not data or "key" not in data:
        return jsonify({"ok": False, "error": "Missing key"}), 400

    key_value = str(data["key"])
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.keyboard.press(key_value)
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
        browser_page = get_browser()
        with browser_lock:
            browser_page.keyboard.type(text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # threaded=True is required now -- without it, one connected /stream
    # client still blocks every other request from being handled at all.
    app.run(host="0.0.0.0", port=port, threaded=True)

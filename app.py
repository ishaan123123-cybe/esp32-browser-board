
from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import io
import threading
import os

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

WIDTH = 240
HEIGHT = 320

# ============================================================
# START CHROMIUM
# ============================================================

print("Starting Chromium...")

playwright = sync_playwright().start()

browser = playwright.chromium.launch(
    headless=True,
    args=[
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu"
    ]
)

page = browser.new_page(
    viewport={
        "width": WIDTH,
        "height": HEIGHT
    },
    device_scale_factor=1
)

page.goto(
    "https://example.com",
    wait_until="domcontentloaded"
)

print("Browser ready")

# Prevent two ESP32 requests from messing with Chromium
browser_lock = threading.Lock()


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():
    return """
    <html>
        <body style="font-family:Arial;background:#111;color:white">
            <h1>ESP32 Browser</h1>
            <p>Browser server is running</p>
            <p>Current page:</p>
            <p id="url"></p>

            <script>
                fetch("/status")
                    .then(r => r.json())
                    .then(d => {
                        document.getElementById("url").innerText = d.url;
                    });
            </script>
        </body>
    </html>
    """


# ============================================================
# STATUS
# ============================================================

@app.get("/status")
def status():

    with browser_lock:
        return jsonify({
            "ok": True,
            "url": page.url,
            "width": WIDTH,
            "height": HEIGHT
        })


# ============================================================
# GET SCREENSHOT
# ============================================================

@app.get("/screen")
def screen():

    with browser_lock:

        image = page.screenshot(
            type="jpeg",
            quality=55,
            full_page=False
        )

    return send_file(
        io.BytesIO(image),
        mimetype="image/jpeg",
        download_name="screen.jpg"
    )


# ============================================================
# NAVIGATE
# ============================================================

@app.post("/navigate")
def navigate():

    data = request.get_json(silent=True)

    if not data or "url" not in data:
        return jsonify({
            "ok": False,
            "error": "Missing url"
        }), 400

    url = str(data["url"]).strip()

    if not url:
        return jsonify({
            "ok": False,
            "error": "Empty URL"
        }), 400

    # Add HTTPS if needed
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:

        with browser_lock:

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )

        return jsonify({
            "ok": True,
            "url": page.url
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e),
            "url": page.url
        }), 500


# ============================================================
# TOUCHSCREEN CLICK
# ============================================================

@app.post("/touch")
def touch():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "ok": False,
            "error": "Missing JSON"
        }), 400

    if "x" not in data or "y" not in data:
        return jsonify({
            "ok": False,
            "error": "Missing x or y"
        }), 400

    try:

        x = float(data["x"])
        y = float(data["y"])

    except:

        return jsonify({
            "ok": False,
            "error": "x and y must be numbers"
        }), 400

    # Keep coordinates inside the screen
    x = max(0, min(WIDTH - 1, x))
    y = max(0, min(HEIGHT - 1, y))

    try:

        with browser_lock:

            page.mouse.click(x, y)

        return jsonify({
            "ok": True,
            "x": x,
            "y": y
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# TOUCH DOWN
# ============================================================

@app.post("/touch/down")
def touch_down():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "ok": False,
            "error": "Missing JSON"
        }), 400

    try:

        x = float(data["x"])
        y = float(data["y"])

        x = max(0, min(WIDTH - 1, x))
        y = max(0, min(HEIGHT - 1, y))

        with browser_lock:
            page.mouse.move(x, y)
            page.mouse.down()

        return jsonify({"ok": True})

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# TOUCH UP
# ============================================================

@app.post("/touch/up")
def touch_up():

    try:

        with browser_lock:
            page.mouse.up()

        return jsonify({"ok": True})

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# SCROLL
# ============================================================

@app.post("/scroll")
def scroll():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "ok": False,
            "error": "Missing JSON"
        }), 400

    amount = float(data.get("amount", 300))

    try:

        with browser_lock:

            page.mouse.wheel(
                0,
                amount
            )

        return jsonify({
            "ok": True,
            "amount": amount
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# BACK
# ============================================================

@app.post("/back")
def back():

    try:

        with browser_lock:

            page.go_back(
                wait_until="domcontentloaded",
                timeout=15000
            )

        return jsonify({
            "ok": True,
            "url": page.url
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# FORWARD
# ============================================================

@app.post("/forward")
def forward():

    try:

        with browser_lock:

            page.go_forward(
                wait_until="domcontentloaded",
                timeout=15000
            )

        return jsonify({
            "ok": True,
            "url": page.url
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# REFRESH
# ============================================================

@app.post("/refresh")
def refresh():

    try:

        with browser_lock:

            page.reload(
                wait_until="domcontentloaded",
                timeout=30000
            )

        return jsonify({
            "ok": True,
            "url": page.url
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# KEYBOARD
# ============================================================

@app.post("/key")
def key():

    data = request.get_json(silent=True)

    if not data or "key" not in data:
        return jsonify({
            "ok": False,
            "error": "Missing key"
        }), 400

    key = str(data["key"])

    try:

        with browser_lock:
            page.keyboard.press(key)

        return jsonify({
            "ok": True,
            "key": key
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# TYPE TEXT
# ============================================================

@app.post("/type")
def type_text():

    data = request.get_json(silent=True)

    if not data or "text" not in data:
        return jsonify({
            "ok": False,
            "error": "Missing text"
        }), 400

    text = str(data["text"])

    try:

        with browser_lock:
            page.keyboard.type(text)

        return jsonify({
            "ok": True
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    print("")
    print("================================")
    print(" ESP32 REMOTE BROWSER")
    print("================================")
    print(f"Screen: {WIDTH}x{HEIGHT}")
    print(f"Port: {port}")
    print("================================")
    print("")

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
```

from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import io
import threading
import os

app = Flask(__name__)

WIDTH = 240
HEIGHT = 320

browser_lock = threading.Lock()

playwright = None
browser = None
page = None

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
            "--disable-software-rasterizer"
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
        wait_until="domcontentloaded",
        timeout=30000
    )

    print("Chromium ready")

def get_browser():
    with browser_lock:
        start_browser()
    return page

@app.get("/")
def home():
    return """<html>
    <head>
        <title>ESP32 Browser</title>
    </head>
    <body style="background:#111; color:white; font-family:Arial; text-align:center; padding:40px;">
        <h1>ESP32 Browser</h1>
        <p>Browser server is running</p>
        <p>
            <a href="/screen" style="color:#4da6ff">
                View browser screenshot
            </a>
        </p>
        <p id="url">Loading...</p>
        <script>
            fetch("/status")
                .then(r => r.json())
                .then(d => {
                    document.getElementById("url").innerText =
                        "Current URL: " + d.url;
                });
        </script>
    </body>
    </html>"""

@app.get("/status")
def status():
    try:
        browser_page = get_browser()
        return jsonify({
            "ok": True,
            "url": browser_page.url,
            "width": WIDTH,
            "height": HEIGHT
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.get("/screen")
def screen():
    try:
        browser_page = get_browser()
        with browser_lock:
            image = browser_page.screenshot(
                type="jpeg",
                quality=60,
                full_page=False
            )
        return send_file(
            io.BytesIO(image),
            mimetype="image/jpeg",
            download_name="screen.jpg"
        )
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

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

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )
        return jsonify({
            "ok": True,
            "url": browser_page.url
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

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
    except Exception:
        return jsonify({
            "ok": False,
            "error": "x and y must be numbers"
        }), 400

    x = max(0, min(WIDTH - 1, x))
    y = max(0, min(HEIGHT - 1, y))

    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.mouse.click(x, y)
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

@app.post("/scroll")
def scroll():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({
            "ok": False,
            "error": "Missing JSON"
        }), 400

    try:
        amount = float(data.get("amount", 300))
    except Exception:
        return jsonify({
            "ok": False,
            "error": "Invalid amount"
        }), 400

    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.mouse.wheel(0, amount)
        return jsonify({
            "ok": True,
            "amount": amount
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.post("/back")
def back():
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.go_back(
                wait_until="domcontentloaded",
                timeout=15000
            )
        return jsonify({
            "ok": True,
            "url": browser_page.url
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.post("/forward")
def forward():
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.go_forward(
                wait_until="domcontentloaded",
                timeout=15000
            )
        return jsonify({
            "ok": True,
            "url": browser_page.url
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.post("/refresh")
def refresh():
    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.reload(
                wait_until="domcontentloaded",
                timeout=30000
            )
        return jsonify({
            "ok": True,
            "url": browser_page.url
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.post("/key")
def key():
    data = request.get_json(silent=True)
    if not data or "key" not in data:
        return jsonify({
            "ok": False,
            "error": "Missing key"
        }), 400

    key_value = str(data["key"])

    try:
        browser_page = get_browser()
        with browser_lock:
            browser_page.keyboard.press(key_value)
        return jsonify({
            "ok": True,
            "key": key_value
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

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
        browser_page = get_browser()
        with browser_lock:
            browser_page.keyboard.type(text)
        return jsonify({
            "ok": True
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    print("================================")
    print(" ESP32 REMOTE BROWSER")
    print("================================")
    print(f"Screen: {WIDTH}x{HEIGHT}")
    print(f"Port: {port}")
    print("================================")

    # threaded=False is required because Playwright sync API is tied to a single thread
    app.run(
        host="0.0.0.0",
        port=port,
        threaded=False
    )

"""
Laptop File Drop
================
Receive files on this laptop from an iPad / iPhone / any device with a web
browser, over your home Wi-Fi. It is the laptop version of TV File Drop.

How to run:   double-click this file   (or:  python laptop_file_drop.py)
Needs:        Python 3.8 or newer. Nothing else to install.
Optional:     "pip install qrcode" to also show a QR code in the window.

How it works (same idea as the TV app):
  1. This program starts a small web server on port 8080.
  2. The window shows an address such as http://192.168.1.50:8080
  3. You open that address in Safari, pick files and tap Upload.
  4. Each file is copied network -> 64 KB buffer -> disk, so even very large
     videos use almost no memory.
"""

import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIRST_PORT = 8080
BUFFER_SIZE = 64 * 1024
SETTINGS_FILE = Path.home() / ".laptop_file_drop.json"
DEFAULT_FOLDER = Path.home() / "Downloads" / "FileDrop"

# The web page the iPad sees. It is the same page as in the TV app.
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laptop File Drop</title>
<!-- This single file is the whole iPad interface. No libraries, no internet:
     the styles and the script are all inside this file. -->
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0 auto; padding: 24px; max-width: 760px;
    font-family: -apple-system, Helvetica, Arial, sans-serif;
    font-size: 20px; color: #fff; background: #101826;
  }
  h1 { font-size: 30px; margin: 8px 0 24px; }

  /* Big buttons: at least 64px tall so they are easy to tap. */
  .btn {
    display: block; width: 100%; min-height: 64px; margin: 0 0 16px;
    padding: 18px; border: 0; border-radius: 14px;
    font-size: 24px; font-weight: 600; text-align: center;
    color: #fff; background: #2d3b55; cursor: pointer;
    -webkit-appearance: none;
  }
  .btn.primary { background: #1e88e5; }
  .btn:disabled { opacity: .4; }

  /* The real file input is hidden; the "Choose files" label opens it. */
  #picker { position: absolute; left: -9999px; width: 1px; height: 1px; opacity: 0; }

  #pinBox { margin: 0 0 16px; }
  #pinBox label { display: block; margin-bottom: 8px; color: #9fb0c8; }
  #pin {
    width: 100%; height: 64px; padding: 0 18px; border: 2px solid #2d3b55;
    border-radius: 14px; font-size: 30px; letter-spacing: 10px; text-align: center;
    color: #fff; background: #0b111c;
  }

  #message { min-height: 28px; margin: 4px 0 16px; color: #ffd54f; }

  .file { margin: 0 0 14px; padding: 14px 16px; border-radius: 14px; background: #1a2436; }
  .name { word-break: break-all; }
  .size { color: #9fb0c8; font-size: 16px; }
  .track { height: 14px; margin: 10px 0 8px; border-radius: 7px; background: #0b111c; overflow: hidden; }
  .bar { height: 100%; width: 0; background: #1e88e5; }
  .state { font-size: 18px; color: #9fb0c8; }
  .state.ok { color: #66d98a; }
  .state.bad { color: #ff8a80; }
</style>
</head>
<body>

<h1>Send files to the laptop</h1>

<div id="pinBox" style="display:none">
  <label for="pin">PIN shown on the laptop</label>
  <input id="pin" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="4" autocomplete="off">
</div>

<label class="btn" for="picker">Choose files</label>
<input id="picker" type="file" multiple>

<button id="uploadBtn" class="btn primary" disabled>Upload</button>

<div id="message"></div>
<div id="list"></div>

<script>
(function () {
  // Shortcuts to the elements on the page.
  var picker = document.getElementById('picker');
  var uploadBtn = document.getElementById('uploadBtn');
  var pinBox = document.getElementById('pinBox');
  var pinInput = document.getElementById('pin');
  var message = document.getElementById('message');
  var list = document.getElementById('list');

  var items = [];          // one entry per chosen file
  var busy = false;        // true while uploads are running
  var pinRequired = false;

  // ---- 1. Ask the laptop whether a PIN is needed ----
  request('GET', '/api/info', function (status, data) {
    if (status === 200 && data && data.pin) {
      pinRequired = true;
      pinBox.style.display = 'block';
    }
  });

  // ---- 2. The user picked files: show them in a list ----
  picker.addEventListener('change', function () {
    if (busy) { return; }
    items = [];
    list.innerHTML = '';
    message.textContent = '';
    for (var i = 0; i < picker.files.length; i++) {
      items.push(makeRow(picker.files[i]));
    }
    uploadBtn.disabled = items.length === 0;
  });

  // Builds the box for one file: name, size, progress bar, state text.
  function makeRow(file) {
    var box = document.createElement('div');
    box.className = 'file';

    var name = document.createElement('div');
    name.className = 'name';
    name.textContent = file.name;

    var size = document.createElement('div');
    size.className = 'size';
    size.textContent = readableSize(file.size);

    var track = document.createElement('div');
    track.className = 'track';
    var bar = document.createElement('div');
    bar.className = 'bar';
    track.appendChild(bar);

    var state = document.createElement('div');
    state.className = 'state';
    state.textContent = 'Waiting';

    box.appendChild(name);
    box.appendChild(size);
    box.appendChild(track);
    box.appendChild(state);
    list.appendChild(box);

    return { file: file, bar: bar, state: state, done: false };
  }

  // ---- 3. "Upload" was tapped ----
  uploadBtn.addEventListener('click', function () {
    if (busy || items.length === 0) { return; }
    if (pinRequired && pinInput.value.length !== 4) {
      message.textContent = 'Type the 4-digit PIN shown on the laptop.';
      pinInput.focus();
      return;
    }
    setBusy(true);
    message.textContent = 'Checking…';

    // Check the PIN first, so a wrong PIN is reported before any file is sent.
    request('GET', '/api/check', function (status, data) {
      if (status === 200) {
        message.textContent = 'Uploading… keep this page open.';
        uploadNext(0);
      } else {
        setBusy(false);
        message.textContent = (data && data.error) ? data.error
          : 'Cannot reach the laptop. Is the server running?';
      }
    });
  });

  // Uploads files one after another (one at a time is kindest to the laptop).
  function uploadNext(index) {
    while (index < items.length && items[index].done) { index++; }
    if (index >= items.length) {
      setBusy(false);
      var failed = 0;
      for (var i = 0; i < items.length; i++) { if (!items[i].done) { failed++; } }
      message.textContent = failed === 0 ? 'All done ✓'
        : failed + ' file(s) failed. Tap Upload to try those again.';
      return;
    }
    uploadOne(items[index], function () { uploadNext(index + 1); });
  }

  // Sends ONE file. The request body is the file itself, so Safari reads it
  // from storage piece by piece and never loads the whole file into memory.
  function uploadOne(item, next) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload?name=' + encodeURIComponent(item.file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Pin', pinInput.value);

    item.state.className = 'state';
    item.state.textContent = 'Uploading… 0%';
    item.bar.style.width = '0';

    // Called many times while the file is being sent.
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable || e.total === 0) { return; }
      var percent = Math.floor(e.loaded * 100 / e.total);
      item.bar.style.width = percent + '%';
      item.state.textContent = 'Uploading… ' + percent + '%';
    };

    // The TV answered.
    xhr.onload = function () {
      var data = parse(xhr.responseText);
      if (xhr.status === 200 && data && data.ok) {
        item.done = true;
        item.bar.style.width = '100%';
        item.state.className = 'state ok';
        item.state.textContent = (data.name && data.name !== item.file.name)
          ? 'Done ✓  (saved as ' + data.name + ')' : 'Done ✓';
      } else {
        fail((data && data.error) ? data.error : 'The TV reported error ' + xhr.status);
      }
      next();
    };

    // No answer at all: connection dropped, server stopped, TV refused the file...
    xhr.onerror = function () {
      fail('Upload failed. Look at the status line on the laptop for the reason.');
      next();
    };
    xhr.onabort = xhr.onerror;

    function fail(text) {
      item.state.className = 'state bad';
      item.state.textContent = 'Error: ' + text;
    }

    xhr.send(item.file);
  }

  // ---- Small helpers ----

  function setBusy(value) {
    busy = value;
    uploadBtn.disabled = value;
    picker.disabled = value;
  }

  // A small request that sends the PIN and gives back (status, parsed JSON).
  function request(method, url, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url);
    xhr.setRequestHeader('X-Pin', pinInput.value);
    xhr.onload = function () { callback(xhr.status, parse(xhr.responseText)); };
    xhr.onerror = function () { callback(0, null); };
    xhr.send();
  }

  function parse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  function readableSize(bytes) {
    if (bytes >= 1073741824) { return (bytes / 1073741824).toFixed(2) + ' GB'; }
    if (bytes >= 1048576) { return (bytes / 1048576).toFixed(1) + ' MB'; }
    if (bytes >= 1024) { return Math.round(bytes / 1024) + ' KB'; }
    return bytes + ' bytes';
  }
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------
# Shared state: the server threads write here, the window reads from here.
# ----------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.folder = str(DEFAULT_FOLDER)
        self.pin_enabled = False
        self.pin = ""
        self.status = "Server is stopped"
        self.recent = []            # newest first
        self.wrong_pins = 0
        self.locked_until = 0.0
        self.changes = queue.Queue()  # the window waits on this for "something changed"
        self.load()

    def load(self):
        """Read the remembered settings (folder, PIN, recent files)."""
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            self.folder = data.get("folder", self.folder)
            self.pin_enabled = bool(data.get("pin_enabled", False))
            self.pin = str(data.get("pin", ""))
            self.recent = list(data.get("recent", []))[:8]
        except Exception:
            pass                    # first run, or a damaged file: use the defaults

    def save(self):
        try:
            SETTINGS_FILE.write_text(json.dumps({
                "folder": self.folder,
                "pin_enabled": self.pin_enabled,
                "pin": self.pin,
                "recent": self.recent,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def set_status(self, text):
        self.status = text
        self.changes.put(True)

    def add_recent(self, line):
        with self.lock:
            self.recent.insert(0, line)
            del self.recent[8:]
            self.save()
        self.changes.put(True)


state = State()


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def local_ip():
    """This laptop's address on the home network, e.g. 192.168.1.50."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No data is sent. We only ask the system "which of my addresses
        # would you use to reach the outside?" - that is the Wi-Fi address.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        s.close()


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def clean_file_name(raw):
    """Make a file name from the iPad safe to use on this laptop."""
    name = raw.replace("\\", "/").split("/")[-1]              # no folder parts
    name = "".join("_" if c in '\\/:*?"<>|' else c for c in name if ord(c) >= 32 and ord(c) != 127)
    name = name.strip().lstrip(".").rstrip(". ")
    if not name:
        name = "file"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:        # names Windows forbids
        name = "_" + name
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 9:
            name = stem[:150 - len(ext) - 1].strip() + "." + ext
        else:
            name = name[:150]
    return name


def open_unique(folder, name):
    """
    Create a NEW file in folder and open it for writing.
    Never overwrites: video.mp4 -> video (1).mp4 -> video (2).mp4 ...
    Mode "xb" means: create, and fail if the file already exists.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for n in range(10000):
        candidate = name if n == 0 else (f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})")
        path = os.path.join(folder, candidate)
        try:
            return open(path, "xb"), path, candidate
        except FileExistsError:
            continue
    raise OSError("Too many files with the same name")


def readable_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} bytes"


def friendly(error):
    """Turn a technical error into a sentence a person can act on."""
    text = str(error)
    if isinstance(error, OSError) and getattr(error, "errno", None) == 28:
        return "The disk is full"
    if isinstance(error, (ConnectionError, TimeoutError, socket.timeout)):
        return "The connection was lost"
    return text or error.__class__.__name__


def check_pin(given):
    """Returns None if the PIN is fine (or not needed), else an error text."""
    import time
    with state.lock:
        if not state.pin_enabled:
            return None
        now = time.time()
        if now < state.locked_until:
            return "Too many wrong PINs. Wait a minute and try again."
        if secrets.compare_digest(given.encode(), state.pin.encode()):
            state.wrong_pins = 0
            return None
        state.wrong_pins += 1
        if state.wrong_pins >= 5:               # 5 wrong tries -> locked for 60 seconds
            state.wrong_pins = 0
            state.locked_until = now + 60
        return "Wrong PIN. Look at the laptop screen."


# ----------------------------------------------------------------------
# The web server. Python calls do_GET / do_POST for every request,
# each on its own background thread.
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30                     # give up on a connection silent for 30 seconds

    def log_message(self, *args):    # keep the console quiet
        pass

    def reply(self, code, body, content_type, close=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            # We answer before reading the whole upload, so hang up afterwards
            # instead of treating the leftover file data as a new request.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, code, data, close=False):
        self.reply(code, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8", close)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/info":
            self.reply_json(200, {"pin": state.pin_enabled})
        elif path == "/api/check":
            problem = check_pin(self.headers.get("X-Pin", "").strip())
            if problem is None:
                self.reply_json(200, {"ok": True})
            else:
                self.reply_json(403, {"ok": False, "error": problem})
        else:
            self.reply_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path != "/api/upload":
            self.reply_json(404, {"ok": False, "error": "Not found"}, close=True)
            return

        # 1. PIN
        problem = check_pin(self.headers.get("X-Pin", "").strip())
        if problem is not None:
            self.reply_json(403, {"ok": False, "error": problem}, close=True)
            return

        # 2. File name (?name=...) and size (Content-Length header)
        name = clean_file_name(parse_qs(url.query).get("name", ["file"])[0])
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 0:
                raise ValueError
        except ValueError:
            self.reply_json(400, {"ok": False, "error": "Missing file size"}, close=True)
            return

        # 3. Create the empty file in the chosen folder
        folder = state.folder
        try:
            os.makedirs(folder, exist_ok=True)
            out, path, saved_name = open_unique(folder, name)
        except Exception as e:
            reason = friendly(e)
            state.set_status(f"Failed: {name} – {reason}")
            self.reply_json(500, {"ok": False, "error": reason}, close=True)
            return

        # 4. Copy: network -> 64 KB buffer -> disk, again and again
        received = 0
        last_percent = -1
        try:
            with out:
                while received < length:
                    chunk = self.rfile.read(min(BUFFER_SIZE, length - received))
                    if not chunk:
                        raise ConnectionError("The connection was lost")
                    out.write(chunk)
                    received += len(chunk)
                    percent = received * 100 // length
                    if percent != last_percent:
                        last_percent = percent
                        state.set_status(f"Receiving {saved_name} – {percent}%")
        except Exception as e:
            # Delete the half-written file so no broken files are left behind.
            try:
                os.remove(path)
            except OSError:
                pass
            reason = friendly(e)
            state.set_status(f"Failed: {name} – {reason}")
            try:
                self.reply_json(500, {"ok": False, "error": reason}, close=True)
            except Exception:
                pass
            return

        # 5. Done
        state.add_recent(f"{saved_name}  ({readable_size(length)})")
        state.set_status(f"Received {saved_name} ✓")
        self.reply_json(200, {"ok": True, "name": saved_name})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, "reuse address" would let two programs share one port by
    # mistake, so we switch it off there.
    allow_reuse_address = os.name != "nt"


class ServerControl:
    """Starts and stops the web server."""

    def __init__(self):
        self.httpd = None
        self.port = 0

    @property
    def running(self):
        return self.httpd is not None

    def start(self):
        if self.running:
            return
        for port in range(FIRST_PORT, FIRST_PORT + 10):      # 8080, then 8081, ...
            try:
                self.httpd = Server(("0.0.0.0", port), Handler)
                self.port = port
                break
            except OSError:
                continue
        if self.httpd is None:
            state.set_status("Could not start the server (ports 8080-8089 are busy)")
            return
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        state.set_status("Waiting for files…")

    def stop(self):
        if not self.running:
            return
        httpd, self.httpd = self.httpd, None
        httpd.shutdown()
        httpd.server_close()
        state.set_status("Server is stopped")

    def address(self):
        ip = local_ip()
        if not self.running:
            return None
        return f"http://{ip}:{self.port}" if ip else None


# ----------------------------------------------------------------------
# The window (tkinter comes with Python)
# ----------------------------------------------------------------------
def run_window(control):
    import tkinter as tk
    from tkinter import filedialog

    BG, FG, DIM, ACCENT, BTN = "#101826", "#ffffff", "#9fb0c8", "#ffd54f", "#2d3b55"

    root = tk.Tk()
    root.title("Laptop File Drop")
    root.configure(bg=BG, padx=28, pady=22)
    root.minsize(760, 560)

    def label(text="", size=12, color=FG, bold=False, **kw):
        font = ("Segoe UI", size, "bold" if bold else "normal")
        w = tk.Label(root, text=text, font=font, fg=color, bg=BG, anchor="w", justify="left", **kw)
        w.pack(fill="x")
        return w

    def button(parent, text, command):
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 12, "bold"),
                         fg=FG, bg=BTN, activebackground=ACCENT, activeforeground=BG,
                         relief="flat", padx=16, pady=8, cursor="hand2")

    label("Laptop File Drop", 20, bold=True)
    label("On your iPad, open Safari and go to:", 11, DIM)
    address_label = label("", 24, ACCENT, bold=True)
    pin_label = label("", 13)

    qr_canvas = tk.Canvas(root, width=1, height=1, bg=BG, highlightthickness=0)
    qr_canvas.pack(anchor="w", pady=(6, 6))

    label("Files are saved to:", 11, DIM)
    folder_label = label("", 12, wraplength=700)

    row = tk.Frame(root, bg=BG)
    row.pack(fill="x", pady=10)

    def change_folder():
        chosen = filedialog.askdirectory(initialdir=state.folder if os.path.isdir(state.folder) else str(Path.home()),
                                         title="Where should received files be saved?")
        if chosen:
            state.folder = os.path.normpath(chosen)
            state.save()
            refresh()

    def open_folder():
        os.makedirs(state.folder, exist_ok=True)
        if os.name == "nt":
            os.startfile(state.folder)                       # Windows Explorer
        elif sys.platform == "darwin":
            os.system(f'open "{state.folder}"')
        else:
            os.system(f'xdg-open "{state.folder}"')

    def toggle_server():
        control.stop() if control.running else control.start()
        refresh()

    def toggle_pin():
        with state.lock:
            state.pin_enabled = not state.pin_enabled
            if state.pin_enabled:
                state.pin = f"{secrets.randbelow(10000):04d}"   # new random 4 digits
            state.save()
        refresh()

    button(row, "Change folder", change_folder).pack(side="left")
    button(row, "Open folder", open_folder).pack(side="left", padx=8)
    server_button = button(row, "", toggle_server)
    server_button.pack(side="left")
    pin_button = button(row, "", toggle_pin)
    pin_button.pack(side="left", padx=8)

    label("Status:", 11, DIM)
    status_label = label("", 14, ACCENT, wraplength=700)
    label("Last files received:", 11, DIM)
    recent_label = label("", 11, wraplength=700)

    shown_qr = [None]

    def draw_qr(text):
        """Draws a QR code if the optional 'qrcode' package is installed."""
        if text == shown_qr[0]:
            return
        shown_qr[0] = text
        qr_canvas.delete("all")
        qr_canvas.config(width=1, height=1)
        if not text:
            return
        try:
            import qrcode
        except ImportError:
            return                                           # no package: simply no QR code
        qr = qrcode.QRCode(border=2)
        qr.add_data(text)
        qr.make()
        matrix = qr.get_matrix()                             # rows of True/False
        cell = max(4, 210 // len(matrix))
        size = cell * len(matrix)
        qr_canvas.config(width=size, height=size, bg="white")
        for y, line in enumerate(matrix):
            for x, dark in enumerate(line):
                if dark:
                    qr_canvas.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                                               fill="black", width=0)

    def refresh():
        """Put the current state on the screen."""
        address = control.address()
        if not control.running:
            address_label.config(text="Server is stopped")
        else:
            address_label.config(text=address or "No network connection")
        draw_qr(address if control.running else None)
        pin_label.config(text=f"PIN:  {state.pin}" if state.pin_enabled
                         else "PIN: off (anyone on your Wi-Fi can send)")
        pin_button.config(text="Turn PIN off" if state.pin_enabled else "Turn PIN on")
        server_button.config(text="Stop server" if control.running else "Start server")
        folder_label.config(text=state.folder)
        status_label.config(text=state.status)
        recent_label.config(text="\n".join(state.recent[:6]) or "Nothing yet")

    def tick():
        """Runs 4 times a second: redraw if a server thread reported a change."""
        changed = False
        while not state.changes.empty():
            state.changes.get_nowait()
            changed = True
        tick.count += 1
        if changed or tick.count % 20 == 0:                  # also every 5 s (Wi-Fi may change)
            refresh()
        root.after(250, tick)

    tick.count = 0

    def on_close():
        control.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    control.start()
    refresh()
    tick()
    root.mainloop()


def run_console(control):
    """Fallback without a window:  python laptop_file_drop.py --nogui"""
    control.start()
    print("Laptop File Drop")
    print("Open in Safari:", control.address() or "(no network connection)")
    print("Saving to:     ", state.folder)
    print("PIN:           ", state.pin if state.pin_enabled else "off")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            state.changes.get()
            print(state.status)
    except KeyboardInterrupt:
        control.stop()


if __name__ == "__main__":
    control = ServerControl()
    if "--nogui" in sys.argv:
        run_console(control)
    else:
        try:
            run_window(control)
        except ImportError:
            run_console(control)       # tkinter missing: use the console instead

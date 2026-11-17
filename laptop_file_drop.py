"""
Laptop File Drop  (version 2)
=============================
Move files and text between this laptop and an iPad / iPhone (or any device
with a web browser) over your home Wi-Fi, in both directions.

How to run:   double-click this file   (or:  python laptop_file_drop.py)
Needs:        Python 3.8 or newer. Nothing else to install.
Optional:     "pip install qrcode" to also show a QR code in the window.

How it works:
  1. This program starts a small web server on port 8080.
  2. The window shows an address such as http://192.168.1.50:8080
  3. You open that address in Safari. The web page can
       - send files to the laptop        (upload)
       - get files from the laptop       (download, or view photos/videos)
       - exchange a piece of text        (links, notes, codes)
  4. Files are always copied in 64 KB pieces, so even very large videos
     use almost no memory.

Reading order if you want to learn from the code:
  State -> helpers -> Handler (the web server) -> ServerControl -> run_window
"""

import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

APP_NAME = "Laptop File Drop"
FIRST_PORT = 8080
BUFFER_SIZE = 64 * 1024
MAX_TEXT_BYTES = 200_000                     # longest text that can be exchanged
SETTINGS_FILE = Path.home() / ".laptop_file_drop.json"
DEFAULT_FOLDER = Path.home() / "Downloads" / "FileDrop"

# The web page the phone or tablet sees: HTML + CSS + JavaScript in one piece.
# No libraries and no internet are needed.
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="File Drop">
<meta name="theme-color" content="#101826">
<link rel="icon" href="data:,">
<title>Laptop File Drop</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0 auto; padding: 20px 20px 60px; max-width: 760px;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 18px; color: #fff; background: #101826;
  }
  h1 { font-size: 28px; margin: 8px 0 2px; }
  #device { color: #9fb0c8; margin: 0 0 20px; }
  h2 { font-size: 22px; margin: 34px 0 14px; padding-top: 22px; border-top: 2px solid #2d3b55; }
  .hint { color: #9fb0c8; font-size: 16px; }

  /* Big buttons: at least 60px tall so they are easy to tap. */
  .btn {
    display: block; width: 100%; min-height: 60px; margin: 0 0 14px;
    padding: 16px; border: 0; border-radius: 14px;
    font-size: 22px; font-weight: 600; text-align: center; text-decoration: none;
    color: #fff; background: #1e88e5; cursor: pointer; -webkit-appearance: none;
  }
  .btn.quiet { background: #2d3b55; }
  .small {
    min-height: 44px; padding: 8px 16px; border: 0; border-radius: 10px;
    font-size: 17px; font-weight: 600; text-decoration: none; display: inline-block;
    color: #fff; background: #2d3b55; cursor: pointer; -webkit-appearance: none;
  }
  .small.go { background: #1e88e5; }

  /* The real file input is hidden; the "Choose files" label opens it. */
  #picker { position: absolute; left: -9999px; width: 1px; height: 1px; opacity: 0; }
  #drop.over { outline: 3px dashed #ffd54f; outline-offset: 6px; }

  #pinBox { margin: 0 0 18px; }
  #pinBox label { display: block; margin-bottom: 8px; color: #9fb0c8; }
  #pin {
    width: 100%; height: 60px; border: 2px solid #2d3b55; border-radius: 14px;
    font-size: 28px; letter-spacing: 10px; text-align: center; color: #fff; background: #0b111c;
  }
  textarea {
    width: 100%; min-height: 110px; margin: 0 0 12px; padding: 12px;
    border: 2px solid #2d3b55; border-radius: 14px;
    font: inherit; color: #fff; background: #0b111c;
  }
  #message, #textMessage { min-height: 26px; margin: 2px 0 14px; color: #ffd54f; }

  .card { margin: 0 0 12px; padding: 14px 16px; border-radius: 14px; background: #1a2436; }
  .top { display: flex; align-items: center; gap: 12px; }
  .grow { flex: 1; min-width: 0; }
  .name { word-break: break-all; }
  .size { color: #9fb0c8; font-size: 15px; }
  .track { height: 12px; margin: 10px 0 8px; border-radius: 6px; background: #0b111c; overflow: hidden; }
  .bar { height: 100%; width: 0; background: #1e88e5; }
  .state { font-size: 16px; color: #9fb0c8; }
  .state.ok { color: #66d98a; }
  .state.bad { color: #ff8a80; }
  .actions { margin-top: 10px; display: flex; gap: 10px; flex-wrap: wrap; }
</style>
</head>
<body>

<h1>Laptop File Drop</h1>
<p id="device">Connecting…</p>

<div id="pinBox" style="display:none">
  <label for="pin">PIN shown on the laptop</label>
  <input id="pin" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="4" autocomplete="off">
</div>

<!-- 1. This device -> laptop -->
<div id="drop">
  <label class="btn" for="picker">Send files to the laptop</label>
  <input id="picker" type="file" multiple>
</div>
<div id="message"></div>
<div id="list"></div>

<!-- 2. Laptop -> this device -->
<h2>Get files from the laptop</h2>
<div id="shared" class="hint"></div>

<!-- 3. Text in both directions -->
<h2>Text</h2>
<div id="fromLaptop" style="display:none">
  <p class="hint">From the laptop:</p>
  <textarea id="textIn" readonly></textarea>
  <button id="copyBtn" class="btn quiet">Copy</button>
</div>
<textarea id="textOut" placeholder="Type or paste a link, a note, a code…"></textarea>
<button id="textBtn" class="btn quiet">Send text to the laptop</button>
<div id="textMessage"></div>

<script>
(function () {
  function $(id) { return document.getElementById(id); }
  var picker = $('picker'), pinInput = $('pin'), message = $('message'), list = $('list');

  var items = [];           // every file chosen so far, in order
  var active = null;        // the file being uploaded right now (one at a time)
  var pinRequired = false;
  var okPin = null;         // a PIN the laptop accepted
  var deniedPin = null;     // a PIN the laptop refused: do not try it again

  // ---------------------------------------------------------------
  // Start: ask the laptop for its name and whether a PIN is needed
  // ---------------------------------------------------------------
  request('GET', '/api/info', null, function (status, data) {
    if (status !== 200 || !data) {
      $('device').textContent = 'Cannot reach the laptop. Is the app running?';
      return;
    }
    $('device').textContent = 'Connected to ' + data.name;
    pinRequired = !!data.pin;
    $('pinBox').style.display = pinRequired ? 'block' : 'none';
    loadShared();
  });

  pinInput.addEventListener('input', function () { loadShared(); pump(); });

  // ---------------------------------------------------------------
  // 1. Sending files to the laptop
  // ---------------------------------------------------------------
  picker.addEventListener('change', function () {
    addFiles(picker.files);
    picker.value = '';                    // so the same file can be chosen again later
  });

  // On a computer you can also drag files onto the page.
  document.addEventListener('dragover', function (e) { e.preventDefault(); $('drop').className = 'over'; });
  document.addEventListener('dragleave', function () { $('drop').className = ''; });
  document.addEventListener('drop', function (e) {
    e.preventDefault();
    $('drop').className = '';
    if (e.dataTransfer && e.dataTransfer.files) { addFiles(e.dataTransfer.files); }
  });

  function addFiles(files) {
    for (var i = 0; i < files.length; i++) { items.push(makeRow(files[i])); }
    pump();
  }

  // Builds the card for one file: name, size, progress bar, state, one button.
  function makeRow(file) {
    var card = el('div', 'card'), top = el('div', 'top'), grow = el('div', 'grow');
    var name = el('div', 'name', file.name), size = el('div', 'size', readableSize(file.size));
    var button = el('button', 'small', 'Cancel');
    var track = el('div', 'track'), bar = el('div', 'bar'), state = el('div', 'state', 'Waiting');
    grow.appendChild(name); grow.appendChild(size);
    top.appendChild(grow); top.appendChild(button);
    track.appendChild(bar);
    card.appendChild(top); card.appendChild(track); card.appendChild(state);
    list.appendChild(card);

    var item = { file: file, bar: bar, state: state, button: button, status: 'waiting', xhr: null };
    button.addEventListener('click', function () {
      if (item.status === 'uploading' && item.xhr) {
        item.xhr.abort();                           // Cancel
      } else if (item.status === 'waiting') {
        finish(item, 'cancelled', 'Cancelled');
      } else if (item.status === 'failed' || item.status === 'cancelled') {
        item.status = 'waiting';                    // Retry
        item.state.className = 'state';
        item.state.textContent = 'Waiting';
        item.button.textContent = 'Cancel';
        pump();
      }
    });
    return item;
  }

  // Starts the next waiting file, if nothing is being uploaded.
  function pump() {
    if (active) { return; }
    var next = null;
    for (var i = 0; i < items.length; i++) {
      if (items[i].status === 'waiting') { next = items[i]; break; }
    }
    if (!next) { summary(); return; }

    var pin = pinInput.value;
    if (pinRequired && pin !== okPin) {
      if (pin.length !== 4) { message.textContent = 'Type the 4-digit PIN shown on the laptop.'; pinInput.focus(); return; }
      if (pin === deniedPin) { return; }
      // Check the PIN before any file is sent, so a wrong PIN is reported at once.
      active = next;
      request('GET', '/api/check', null, function (status, data) {
        active = null;
        if (status === 200) { okPin = pin; pump(); }
        else {
          deniedPin = pin;
          message.textContent = (data && data.error) ? data.error : 'Cannot reach the laptop.';
        }
      });
      return;
    }
    uploadOne(next);
  }

  // Sends ONE file. The request body is the file itself, so the browser reads
  // it from storage piece by piece and never loads it all into memory.
  function uploadOne(item) {
    active = item;
    item.status = 'uploading';
    item.button.textContent = 'Cancel';
    item.button.style.display = '';
    item.state.className = 'state';
    item.state.textContent = 'Starting…';
    item.bar.style.width = '0';
    message.textContent = 'Uploading… keep this page open.';

    var xhr = new XMLHttpRequest();
    item.xhr = xhr;
    xhr.open('POST', '/api/upload?name=' + encodeURIComponent(item.file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Pin', pinInput.value);

    var started = Date.now();
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable || e.total === 0) { return; }
      var percent = Math.floor(e.loaded * 100 / e.total);
      var seconds = (Date.now() - started) / 1000;
      var text = percent + '%';
      if (seconds > 1) {
        var speed = e.loaded / seconds;                       // bytes per second
        var left = (e.total - e.loaded) / speed;              // seconds left
        text += '  ·  ' + readableSize(speed) + '/s  ·  ' + readableTime(left) + ' left';
      }
      item.bar.style.width = percent + '%';
      item.state.textContent = text;
    };
    xhr.onload = function () {
      var data = parse(xhr.responseText);
      if (xhr.status === 200 && data && data.ok) {
        item.bar.style.width = '100%';
        finish(item, 'done', (data.name && data.name !== item.file.name)
          ? 'Done ✓  (saved as ' + data.name + ')' : 'Done ✓');
      } else {
        finish(item, 'failed', 'Error: ' + ((data && data.error) ? data.error : 'the laptop reported ' + xhr.status));
      }
    };
    xhr.onerror = function () {
      finish(item, 'failed', 'Error: upload failed. Look at the laptop window for the reason.');
    };
    xhr.onabort = function () { finish(item, 'cancelled', 'Cancelled'); };
    xhr.send(item.file);
  }

  function finish(item, status, text) {
    item.status = status;
    item.xhr = null;
    item.state.textContent = text;
    item.state.className = 'state' + (status === 'done' ? ' ok' : (status === 'failed' ? ' bad' : ''));
    if (status === 'done') { item.button.style.display = 'none'; }
    else { item.button.textContent = 'Retry'; }
    if (active === item) { active = null; }
    pump();
  }

  function summary() {
    var done = 0, failed = 0;
    for (var i = 0; i < items.length; i++) {
      if (items[i].status === 'done') { done++; }
      if (items[i].status === 'failed') { failed++; }
    }
    if (items.length === 0) { message.textContent = ''; }
    else if (failed > 0) { message.textContent = done + ' sent, ' + failed + ' failed. Tap Retry on the failed ones.'; }
    else { message.textContent = done + ' file(s) sent ✓'; }
  }

  // ---------------------------------------------------------------
  // 2. Files (and text) the laptop is sharing
  // ---------------------------------------------------------------
  var shared = $('shared');
  var shownList = null;     // what is on screen now, to avoid redrawing for nothing

  function loadShared() {
    var pin = pinInput.value;
    if (pinRequired && (pin.length !== 4 || pin === deniedPin)) {
      shared.textContent = 'Type the PIN shown on the laptop to see the files.';
      $('fromLaptop').style.display = 'none';
      shownList = null;
      return;
    }
    request('GET', '/api/files', null, function (status, data) {
      if (status === 403) {
        deniedPin = pin;
        shownList = null;
        shared.textContent = (data && data.error) ? data.error : 'Wrong PIN.';
        return;
      }
      if (status !== 200 || !data) { return; }
      okPin = pin;

      // Text from the laptop
      var box = $('fromLaptop');
      if (data.text) {
        if ($('textIn').value !== data.text) { $('textIn').value = data.text; }
        box.style.display = 'block';
      } else {
        box.style.display = 'none';
      }

      // Files from the laptop
      var key = JSON.stringify(data.files) + pin;
      if (key === shownList) { return; }
      shownList = key;
      shared.innerHTML = '';
      if (data.files.length === 0) {
        shared.textContent = 'Nothing is shared right now. In the laptop window, click "Add files".';
        return;
      }
      for (var i = 0; i < data.files.length; i++) { shared.appendChild(sharedRow(data.files[i], pin)); }
    });
  }

  function sharedRow(f, pin) {
    var link = '/download?id=' + encodeURIComponent(f.id) + '&pin=' + encodeURIComponent(pin);
    var card = el('div', 'card');
    card.appendChild(el('div', 'name', f.name));
    card.appendChild(el('div', 'size', readableSize(f.size)));
    var actions = el('div', 'actions');
    var get = el('a', 'small go', 'Download');
    get.href = link;
    get.setAttribute('download', f.name);
    actions.appendChild(get);
    if (f.view) {                       // photos, videos, music, PDFs: open without saving
      var view = el('a', 'small', 'View');
      view.href = link + '&view=1';
      view.target = '_blank';
      actions.appendChild(view);
    }
    card.appendChild(actions);
    return card;
  }

  setInterval(loadShared, 3000);        // look for changes every 3 seconds

  // ---------------------------------------------------------------
  // 3. Text
  // ---------------------------------------------------------------
  $('textBtn').addEventListener('click', function () {
    var text = $('textOut').value;
    if (!text.trim()) { $('textMessage').textContent = 'Type some text first.'; return; }
    if (pinRequired && pinInput.value.length !== 4) {
      $('textMessage').textContent = 'Type the 4-digit PIN shown on the laptop.';
      return;
    }
    $('textMessage').textContent = 'Sending…';
    request('POST', '/api/text', text, function (status, data) {
      if (status === 200) {
        $('textMessage').textContent = 'Sent ✓  It is now in the laptop clipboard (Ctrl+V to paste).';
        $('textOut').value = '';
      } else {
        $('textMessage').textContent = (data && data.error) ? data.error : 'Could not send the text.';
      }
    });
  });

  // navigator.clipboard only works on https pages, so we use the older way:
  // select the text, then ask the browser to copy the selection.
  $('copyBtn').addEventListener('click', function () {
    var box = $('textIn');
    box.focus();
    box.select();
    box.setSelectionRange(0, box.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    $('copyBtn').textContent = ok ? 'Copied ✓' : 'Select the text and copy it';
    setTimeout(function () { $('copyBtn').textContent = 'Copy'; }, 2000);
  });

  // ---------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  // A small request that sends the PIN and gives back (status, parsed JSON).
  function request(method, url, body, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url);
    xhr.setRequestHeader('X-Pin', pinInput.value);
    if (body !== null) { xhr.setRequestHeader('Content-Type', 'text/plain; charset=utf-8'); }
    xhr.onload = function () { callback(xhr.status, parse(xhr.responseText)); };
    xhr.onerror = function () { callback(0, null); };
    xhr.send(body);
  }

  function parse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  function readableSize(bytes) {
    if (bytes >= 1073741824) { return (bytes / 1073741824).toFixed(2) + ' GB'; }
    if (bytes >= 1048576) { return (bytes / 1048576).toFixed(1) + ' MB'; }
    if (bytes >= 1024) { return Math.round(bytes / 1024) + ' KB'; }
    return Math.round(bytes) + ' bytes';
  }

  function readableTime(seconds) {
    if (!isFinite(seconds)) { return '…'; }
    seconds = Math.ceil(seconds);
    if (seconds < 60) { return seconds + ' s'; }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) { return minutes + ' min ' + (seconds % 60) + ' s'; }
    return Math.floor(minutes / 60) + ' h ' + (minutes % 60) + ' min';
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
        self.recent = []              # received files, newest first (text lines)
        self.shared = []              # files offered to other devices: {"id","path","name","size"}
        self.text_out = ""            # text the laptop offers to other devices
        self.text_in = ""             # the last text received from a device
        self.text_in_count = 0        # goes up by one for every received text
        self.wrong_pins = 0
        self.locked_until = 0.0
        self.changes = queue.Queue()  # the window checks this for "something changed"
        self.load()

    # ----- settings that survive closing the app -----

    def load(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            self.folder = str(data.get("folder", self.folder))
            self.pin_enabled = bool(data.get("pin_enabled", False))
            self.pin = str(data.get("pin", ""))
            self.recent = [str(line) for line in data.get("recent", [])][:8]
        except Exception:
            pass                      # first run, or a damaged file: use the defaults

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

    # ----- things the server threads report -----

    def set_status(self, text):
        self.status = text
        self.changes.put(True)

    def add_recent(self, line):
        with self.lock:
            self.recent.insert(0, line)
            del self.recent[8:]
            self.save()
        self.changes.put(True)

    def receive_text(self, text):
        with self.lock:
            self.text_in = text
            self.text_in_count += 1
        self.set_status("Text received ✓ (copied to the clipboard)")

    # ----- files offered to other devices -----

    def share(self, paths):
        with self.lock:
            already = {entry["path"] for entry in self.shared}
            for path in paths:
                path = os.path.abspath(path)
                if path in already or not os.path.isfile(path):
                    continue
                already.add(path)
                self.shared.append({
                    # A random id, so nobody can guess the download address of a file.
                    "id": secrets.token_urlsafe(9),
                    "path": path,
                    "name": os.path.basename(path),
                    "size": os.path.getsize(path),
                })
        self.changes.put(True)

    def unshare(self, ids):
        with self.lock:
            self.shared = [e for e in self.shared if e["id"] not in ids]
        self.changes.put(True)

    def clear_shared(self):
        with self.lock:
            self.shared = []
        self.changes.put(True)

    def shared_list(self):
        """The shared files that still exist (a file may have been deleted meanwhile)."""
        with self.lock:
            self.shared = [e for e in self.shared if os.path.isfile(e["path"])]
            return list(self.shared)


state = State()


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def local_ip():
    """This laptop's address on the home network, e.g. 192.168.1.50."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No data is sent. We only ask the system "which of my addresses
        # would you use to reach the network?" - that is the Wi-Fi address.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        s.close()


def keep_awake(on):
    """
    Windows only: stop the laptop from going to sleep while a file is moving.
    The setting belongs to the thread that asks for it, so every transfer
    switches it on for itself and off again when it ends.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


WINDOWS_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})


def clean_file_name(raw):
    """Make a file name from another device safe to use on this laptop."""
    name = raw.replace("\\", "/").split("/")[-1]              # no folder parts
    name = "".join("_" if c in '\\/:*?"<>|' else c
                   for c in name if ord(c) >= 32 and ord(c) != 127)
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


def numbered(name, n):
    """numbered("video.mp4", 0) = "video.mp4",  numbered("video.mp4", 2) = "video (2).mp4" """
    if n == 0:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({n})"
    return f"{stem} ({n}).{ext}"


# Only one thread at a time may pick a file name, so two uploads with the
# same name can never grab the same file.
name_lock = threading.Lock()


def create_part_file(folder, name):
    """
    Open a temporary "<name>.part" file for writing.
    While a file is arriving it is called e.g. "video.mp4.part"; only a
    complete file gets its real name. So a file without ".part" is always whole.
    """
    with name_lock:
        for n in range(10000):
            final = os.path.join(folder, numbered(name, n))
            part = final + ".part"
            if os.path.exists(final) or os.path.exists(part):
                continue
            return open(part, "xb"), part          # "xb" = create, fail if it exists
    raise OSError("Too many files with the same name")


def finish_part_file(part, folder, name):
    """Give a completed .part file its real name. Never overwrites: adds (1), (2), ..."""
    with name_lock:
        for n in range(10000):
            candidate = numbered(name, n)
            final = os.path.join(folder, candidate)
            if os.path.exists(final):
                continue
            os.rename(part, final)
            return candidate
    raise OSError("Too many files with the same name")


def readable_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{int(n)} bytes"


def friendly(error):
    """Turn a technical error into a sentence a person can act on."""
    if isinstance(error, OSError) and getattr(error, "errno", None) == 28:
        return "The disk is full"
    if isinstance(error, (ConnectionError, TimeoutError, socket.timeout)):
        return "The connection was lost or the transfer was cancelled"
    return str(error) or error.__class__.__name__


def can_view(name):
    """True for files a browser can show directly: photos, videos, music, PDFs, plain text."""
    kind = mimetypes.guess_type(name)[0] or ""
    if kind in ("image/svg+xml", "text/html"):
        return False                       # these could run scripts, so download only
    return kind.startswith(("image/", "video/", "audio/")) or kind in ("application/pdf", "text/plain")


def parse_range(header, size):
    """
    Browsers ask for a part of a file with a header such as "bytes=1000-1999".
    That is how video players jump around and how downloads resume.
    Returns (first, last), or None for "the whole file", or "bad" for nonsense.
    """
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not match or (match.group(1) == "" and match.group(2) == ""):
        return None
    if match.group(1) == "":                        # "bytes=-500" = the last 500 bytes
        count = int(match.group(2))
        if count == 0 or size == 0:
            return "bad"
        return max(0, size - count), size - 1
    first = int(match.group(1))
    last = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
    if first >= size or first > last:
        return "bad"
    return first, last


def check_pin(given):
    """Returns None if the PIN is fine (or not needed), else an error text."""
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
        return "Wrong PIN. Look at the laptop window."


# ----------------------------------------------------------------------
# The web server. Python calls do_GET / do_POST for every request,
# each on its own background thread.
#
#   GET  /              the web page
#   GET  /api/info      laptop name + "is a PIN needed?"      (no PIN required)
#   GET  /api/check     is this PIN right?
#   GET  /api/files     shared files + shared text
#   GET  /download      one shared file (download or view)
#   POST /api/upload    receive one file; the request body IS the file
#   POST /api/text      receive a piece of text
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30                     # give up on a connection silent for 30 seconds

    def log_message(self, *args):    # keep the console quiet
        pass

    # ----- answering -----

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
        self.reply(code, json.dumps(data, ensure_ascii=False),
                   "application/json; charset=utf-8", close)

    def fail(self, code, message, close=True):
        self.reply_json(code, {"ok": False, "error": message}, close)

    def header_pin(self):
        return self.headers.get("X-Pin", "").strip()

    # ----- GET -----

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            self.reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/info":
            self.reply_json(200, {"pin": state.pin_enabled, "name": socket.gethostname()})
        elif path == "/api/check":
            problem = check_pin(self.header_pin())
            if problem is None:
                self.reply_json(200, {"ok": True})
            else:
                self.fail(403, problem, close=False)
        elif path == "/api/files":
            problem = check_pin(self.header_pin())
            if problem is not None:
                self.fail(403, problem, close=False)
                return
            files = [{"id": e["id"], "name": e["name"], "size": e["size"], "view": can_view(e["name"])}
                     for e in state.shared_list()]
            self.reply_json(200, {"files": files, "text": state.text_out})
        elif path == "/download":
            self.send_shared_file(parse_qs(url.query))
        else:
            self.fail(404, "Not found", close=False)

    def send_shared_file(self, query):
        """Laptop -> device: stream one shared file, whole or in part."""
        problem = check_pin(query.get("pin", [""])[0].strip())
        if problem is not None:
            self.reply(403, problem, "text/plain; charset=utf-8")
            return
        wanted = query.get("id", [""])[0]
        entry = next((e for e in state.shared_list() if e["id"] == wanted), None)
        if entry is None:
            self.reply(404, "This file is no longer shared.", "text/plain; charset=utf-8")
            return
        try:
            source = open(entry["path"], "rb")
            size = os.path.getsize(entry["path"])
        except OSError as e:
            self.reply(500, friendly(e), "text/plain; charset=utf-8")
            return

        name = entry["name"]
        viewing = query.get("view", [""])[0] == "1" and can_view(name)
        wanted_range = parse_range(self.headers.get("Range"), size)
        if wanted_range == "bad":
            source.close()
            self.send_response(416)                       # "that part does not exist"
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        first, last = wanted_range if wanted_range else (0, size - 1)
        count = last - first + 1

        # Headers may only contain simple characters, so the name is sent twice:
        # a plain fallback, and the real name (Arabic etc.) in the encoded form.
        plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
        how = "inline" if viewing else "attachment"
        kind = (mimetypes.guess_type(name)[0] or "application/octet-stream") if viewing \
            else "application/octet-stream"

        self.send_response(206 if wanted_range else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(count))
        self.send_header("Accept-Ranges", "bytes")
        if wanted_range:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.send_header("Content-Disposition",
                         f"{how}; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        sent = 0
        last_percent = -1
        keep_awake(True)
        try:
            with source:
                source.seek(first)
                while sent < count:
                    chunk = source.read(min(BUFFER_SIZE, count - sent))   # 64 KB at a time
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if viewing:
                        continue          # a video player asks for many small parts: no % shown
                    percent = (first + sent) * 100 // size
                    if percent != last_percent:
                        last_percent = percent
                        state.set_status(f"Sending {name} – {percent}%")
            if sent != count:
                self.close_connection = True
            elif viewing:
                state.set_status(f"Showing {name} on the other device")
            elif last == size - 1:
                state.set_status(f"Sent {name} ✓")
        except Exception:
            # The other device cancelled, closed the video, or the Wi-Fi dropped.
            self.close_connection = True
            if not viewing:
                state.set_status(f"Sending {name} stopped")
        finally:
            keep_awake(False)

    # ----- POST -----

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path not in ("/api/upload", "/api/text"):
            self.fail(404, "Not found")
            return

        problem = check_pin(self.header_pin())
        if problem is not None:
            self.fail(403, problem)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 0:
                raise ValueError
        except ValueError:
            self.fail(400, "Missing size")
            return

        if url.path == "/api/text":
            self.receive_text(length)
        else:
            self.receive_file(length, parse_qs(url.query).get("name", ["file"])[0])

    def receive_text(self, length):
        if length > MAX_TEXT_BYTES:
            self.fail(413, "That text is too long")
            return
        text = self.rfile.read(length).decode("utf-8", errors="replace")
        if not text.strip():
            self.fail(400, "The text is empty", close=False)
            return
        state.receive_text(text)
        self.reply_json(200, {"ok": True})

    def receive_file(self, length, raw_name):
        """Device -> laptop: the request body is the file. Copy it straight to disk."""
        name = clean_file_name(raw_name)
        folder = state.folder

        # 1. Is there room? Then create the temporary ".part" file.
        try:
            os.makedirs(folder, exist_ok=True)
            if shutil.disk_usage(folder).free < length:
                raise OSError("Not enough free space on the laptop")
            out, part = create_part_file(folder, name)
        except Exception as e:
            reason = friendly(e)
            state.set_status(f"Failed: {name} – {reason}")
            self.fail(500, reason)
            return

        # 2. Copy: network -> 64 KB buffer -> disk, again and again.
        received = 0
        last_percent = -1
        started = time.time()
        keep_awake(True)
        try:
            with out:
                while received < length:
                    chunk = self.rfile.read(min(BUFFER_SIZE, length - received))
                    if not chunk:
                        raise ConnectionError("lost")
                    out.write(chunk)
                    received += len(chunk)
                    percent = received * 100 // length
                    if percent != last_percent:
                        last_percent = percent
                        seconds = time.time() - started
                        speed = f"  ({readable_size(received / seconds)}/s)" if seconds > 1 else ""
                        state.set_status(f"Receiving {name} – {percent}%{speed}")
            # 3. Complete: give the file its real name.
            saved_name = finish_part_file(part, folder, name)
        except Exception as e:
            # Delete the half-written file so no broken files are left behind.
            try:
                os.remove(part)
            except OSError:
                pass
            reason = friendly(e)
            state.set_status(f"Stopped: {name} – {reason}")
            try:
                self.fail(500, reason)
            except Exception:
                pass
            return
        finally:
            keep_awake(False)

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
        state.set_status("Ready. Waiting for a device…")

    def stop(self):
        if not self.running:
            return
        httpd, self.httpd = self.httpd, None
        httpd.shutdown()
        httpd.server_close()
        state.set_status("Server is stopped")

    def address(self):
        """The address to open on the other device, or None."""
        if not self.running:
            return None
        ip = local_ip()
        return f"http://{ip}:{self.port}" if ip else None


# ----------------------------------------------------------------------
# The window (tkinter comes with Python)
# ----------------------------------------------------------------------
def run_window(control):
    import tkinter as tk
    from tkinter import filedialog

    BG, PANEL, FG, DIM, ACCENT = "#101826", "#0b111c", "#ffffff", "#9fb0c8", "#ffd54f"
    BTN, PRIMARY, LINE = "#2d3b55", "#1e88e5", "#24324a"
    FONT = "Segoe UI"

    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg=BG)
    root.minsize(1000, 680)

    # Two columns: left = how to connect, right = everything else.
    left = tk.Frame(root, bg=BG, padx=26, pady=22)
    left.pack(side="left", fill="y")
    tk.Frame(root, bg=LINE, width=1).pack(side="left", fill="y")
    right = tk.Frame(root, bg=BG, padx=26, pady=22)
    right.pack(side="left", fill="both", expand=True)

    # ----- small helpers that build widgets -----

    def label(parent, text="", size=11, color=FG, bold=False, wrap=0):
        widget = tk.Label(parent, text=text, font=(FONT, size, "bold" if bold else "normal"),
                          fg=color, bg=BG, anchor="w", justify="left", wraplength=wrap)
        widget.pack(fill="x")
        return widget

    def heading(parent, text, first=False):
        if not first:
            tk.Frame(parent, bg=LINE, height=1).pack(fill="x", pady=(14, 10))
        return label(parent, text, 10, DIM, bold=True)

    def row(parent):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="x", pady=6)
        return frame

    def button(parent, text, command, primary=False):
        widget = tk.Button(parent, text=text, command=command, font=(FONT, 10, "bold"),
                           fg=FG, bg=PRIMARY if primary else BTN,
                           activebackground=ACCENT, activeforeground=BG,
                           relief="flat", padx=14, pady=7, cursor="hand2")
        widget.pack(side="left", padx=(0, 8))
        return widget

    # ----- actions -----

    def toggle_server():
        if control.running:
            control.stop()
        else:
            control.start()
        refresh()

    def toggle_pin():
        with state.lock:
            state.pin_enabled = not state.pin_enabled
            if state.pin_enabled:
                state.pin = f"{secrets.randbelow(10000):04d}"    # new random 4 digits
            state.save()
        refresh()

    def copy_address():
        address = control.address()
        if address:
            root.clipboard_clear()
            root.clipboard_append(address)
            state.set_status("Address copied to the clipboard")

    def change_folder():
        start = state.folder if os.path.isdir(state.folder) else str(Path.home())
        chosen = filedialog.askdirectory(initialdir=start,
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

    def add_files():
        chosen = filedialog.askopenfilenames(title="Choose files to send")
        if chosen:
            state.share(chosen)
            refresh()

    def add_folder():
        chosen = filedialog.askdirectory(title="Choose a folder: every file in it will be shared")
        if not chosen:
            return
        paths = []
        for folder, _subfolders, files in os.walk(chosen):
            for name in sorted(files):
                paths.append(os.path.join(folder, name))
        if len(paths) > 500:
            state.set_status(f"That folder has {len(paths)} files; sharing the first 500")
            paths = paths[:500]
        state.share(paths)
        refresh()

    def remove_selected():
        ids = [shown_ids[i] for i in shared_box.curselection() if i < len(shown_ids)]
        if ids:
            state.unshare(ids)
            refresh()

    def clear_shared():
        state.clear_shared()
        refresh()

    def share_clipboard():
        try:
            text = root.clipboard_get()
        except tk.TclError:
            text = ""
        if not text.strip():
            state.set_status("The clipboard has no text. Copy some text first (Ctrl+C).")
            return
        state.text_out = text[:MAX_TEXT_BYTES]
        state.set_status("Clipboard text is now shown on the web page")
        refresh()

    def stop_text():
        state.text_out = ""
        refresh()

    # ----- left column: connect -----

    label(left, APP_NAME, 20, bold=True)
    label(left, "On the phone or tablet, open\nthe browser and go to:", 11, DIM)
    address_label = label(left, "", 17, ACCENT, bold=True)
    qr_canvas = tk.Canvas(left, width=1, height=1, bg=BG, highlightthickness=0)
    qr_canvas.pack(anchor="w", pady=(10, 10))
    pin_label = label(left, "", 12, wrap=280)
    left_buttons = row(left)
    server_button = button(left_buttons, "", toggle_server, primary=True)
    pin_button = button(left_buttons, "", toggle_pin)
    button(row(left), "Copy address", copy_address)
    label(left, "Both devices must be on\nthe same Wi-Fi.", 10, DIM)

    # ----- right column -----

    heading(right, "RECEIVE  ·  files sent to this laptop are saved in", first=True)
    folder_label = label(right, "", 11, wrap=600)
    folder_row = row(right)
    button(folder_row, "Change folder", change_folder)
    button(folder_row, "Open folder", open_folder)

    heading(right, "SEND  ·  files offered to the phone or tablet")
    send_row = row(right)
    button(send_row, "Add files…", add_files, primary=True)
    button(send_row, "Add folder…", add_folder)
    button(send_row, "Remove selected", remove_selected)
    button(send_row, "Clear", clear_shared)
    shared_box = tk.Listbox(right, height=5, bg=PANEL, fg=FG, font=(FONT, 10),
                            selectbackground=PRIMARY, selectforeground=FG, selectmode="extended",
                            relief="flat", highlightthickness=0, activestyle="none")
    shared_box.pack(fill="x")
    shown_ids = []                  # the ids of the files shown in the list, same order

    heading(right, "TEXT  ·  links, notes, codes")
    text_row = row(right)
    button(text_row, "Share clipboard text", share_clipboard)
    button(text_row, "Stop sharing text", stop_text)
    text_out_label = label(right, "", 10, DIM, wrap=600)
    text_in_label = label(right, "", 10, wrap=600)

    heading(right, "ACTIVITY")
    status_label = label(right, "", 13, ACCENT, wrap=600)
    label(right, "Last files received:", 10, DIM)
    recent_label = label(right, "", 10, wrap=600)

    # ----- drawing -----

    shown_qr = [None]

    def draw_qr(text):
        """Draws a QR code if the optional 'qrcode' package is installed."""
        if text == shown_qr[0]:
            return
        shown_qr[0] = text
        qr_canvas.delete("all")
        qr_canvas.config(width=1, height=1, bg=BG)
        if not text:
            return
        try:
            import qrcode
        except ImportError:
            return                                           # no package: simply no QR code
        code = qrcode.QRCode(border=2)
        code.add_data(text)
        code.make()
        matrix = code.get_matrix()                           # rows of True/False
        cell = max(4, 230 // len(matrix))
        size = cell * len(matrix)
        qr_canvas.config(width=size, height=size, bg="white")
        for y, line in enumerate(matrix):
            for x, dark in enumerate(line):
                if dark:
                    qr_canvas.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                                               fill="black", width=0)

    def one_line(text, limit=90):
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "…"

    def refresh():
        """Put the current state on the screen."""
        address = control.address()
        if not control.running:
            address_label.config(text="Server is stopped")
        else:
            address_label.config(text=address or "No network connection")
        draw_qr(address)
        pin_label.config(text=f"PIN:  {state.pin}" if state.pin_enabled
                         else "PIN is off: anyone on your Wi-Fi can connect")
        pin_button.config(text="Turn PIN off" if state.pin_enabled else "Turn PIN on")
        server_button.config(text="Stop server" if control.running else "Start server")
        folder_label.config(text=state.folder)

        # Redraw the list of shared files only when it really changed,
        # so the selection is not lost every few seconds.
        sharing = state.shared_list()
        ids = [e["id"] for e in sharing]
        if ids != shown_ids:
            shown_ids[:] = ids
            shared_box.delete(0, "end")
            for e in sharing:
                shared_box.insert("end", f"  {e['name']}   ({readable_size(e['size'])})")

        text_out_label.config(text=("Sharing: " + one_line(state.text_out)) if state.text_out
                              else "Not sharing any text")
        text_in_label.config(text=("Received: " + one_line(state.text_in)) if state.text_in else "")
        status_label.config(text=state.status)
        recent_label.config(text="\n".join(state.recent[:5]) or "Nothing yet")

    seen = {"texts": 0, "ticks": 0}

    def tick():
        """Runs 4 times a second: redraw if a server thread reported a change."""
        changed = False
        while not state.changes.empty():
            state.changes.get_nowait()
            changed = True
        # A new text arrived: put it in the laptop clipboard, ready for Ctrl+V.
        if state.text_in_count != seen["texts"]:
            seen["texts"] = state.text_in_count
            root.clipboard_clear()
            root.clipboard_append(state.text_in)
        seen["ticks"] += 1
        if changed or seen["ticks"] % 20 == 0:               # also every 5 s (Wi-Fi may change)
            refresh()
        root.after(250, tick)

    def on_close():
        control.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    control.start()
    refresh()
    tick()
    root.mainloop()


def run_console(control):
    """Fallback without a window:  python laptop_file_drop.py --nogui [files to share]"""
    def say(*parts):
        text = " ".join(str(p) for p in parts)
        try:
            print(text, flush=True)
        except UnicodeEncodeError:               # old consoles cannot print every character
            print(text.encode("ascii", "replace").decode(), flush=True)

    state.share([a for a in sys.argv[1:] if os.path.isfile(a)])
    control.start()
    say(APP_NAME)
    say("Open in the browser:", control.address() or "(no network connection)")
    say("Saving to:          ", state.folder)
    say("PIN:                ", state.pin if state.pin_enabled else "off")
    say("Press Ctrl+C to stop.")
    try:
        while True:
            state.changes.get()
            say(state.status)
    except KeyboardInterrupt:
        control.stop()


if __name__ == "__main__":
    server_control = ServerControl()
    if "--nogui" in sys.argv:
        run_console(server_control)
    else:
        try:
            run_window(server_control)
        except ImportError:
            run_console(server_control)        # tkinter missing: use the console instead

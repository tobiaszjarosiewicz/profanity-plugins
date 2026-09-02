import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import uuid
 
import prof
 
SETTINGS_GROUP = "signal_bridge"
DEFAULT_SOCKET_PATH = os.path.expanduser("~/.local/state/signal-cli/socket")
DEFAULT_ATTACHMENTS_DIR = os.path.expanduser("~/.local/share/signal-cli/attachments")
WINDOW_PREFIX = "Signal "
 
# ASCII-art conversion settings for image attachments. Pure constant,
# safe to read from any thread. This is a maximum bound -- chafa fits
# the image within it preserving aspect ratio (and font cell aspect).
ASCII_SIZE = "100x80"
 
CHAFA = shutil.which("chafa")
 
_stop_event = threading.Event()
_receiver_thread = None
_listener_sock = None
_display_queue = queue.Queue()
 
# Cached config, read by background threads. Only ever WRITTEN from the
# main thread (prof_init, and the command handlers below) -- never
# written from a background thread. Plain attribute reads/writes of a
# str are atomic under the GIL, so no lock is needed for this.
_socket_path = DEFAULT_SOCKET_PATH
_account = ""
_attachments_dir = DEFAULT_ATTACHMENTS_DIR
 
 
# ---------------------------------------------------------------------------
# Settings helpers -- ONLY call these from the main thread.
# ---------------------------------------------------------------------------
 
def _load_settings():
    global _socket_path, _account, _attachments_dir
    _socket_path = prof.settings_string_get(SETTINGS_GROUP, "socket_path", DEFAULT_SOCKET_PATH)
    _account = prof.settings_string_get(SETTINGS_GROUP, "account", "")
    _attachments_dir = prof.settings_string_get(
        SETTINGS_GROUP, "attachments_dir", DEFAULT_ATTACHMENTS_DIR
    )
 
 
def _set_socket_path(path):
    global _socket_path
    prof.settings_string_set(SETTINGS_GROUP, "socket_path", path)
    _socket_path = path
 
 
def _set_account(account):
    global _account
    prof.settings_string_set(SETTINGS_GROUP, "account", account)
    _account = account
 
 
def _set_attachments_dir(path):
    global _attachments_dir
    prof.settings_string_set(SETTINGS_GROUP, "attachments_dir", path)
    _attachments_dir = path
 
 
# ---------------------------------------------------------------------------
# Window helpers -- ONLY call these from the main thread.
# ---------------------------------------------------------------------------
 
def _tag_for(number):
    return WINDOW_PREFIX + number
 
 
def _number_from_tag(tag):
    return tag[len(WINDOW_PREFIX):]
 
 
def _ensure_window(number):
    tag = _tag_for(number)
    if not prof.win_exists(tag):
        prof.win_create(tag, _on_window_input)
    return tag
 
 
def _on_window_input(tag, line):
    """Called by Profanity (main thread) when the user types into one of
    our windows."""
    if not line:
        return
    text = line.strip()
    if not text:
        return
    number = _number_from_tag(tag)
    prof.win_show(tag, "me: " + text)
    _send_async(number, text, tag)
 
 
# ---------------------------------------------------------------------------
# Sending. Runs in a background thread so it never blocks the UI. Reads
# only the cached _socket_path / _account globals -- never calls prof.*.
# All results are handed to the main thread via _display_queue.
# ---------------------------------------------------------------------------
 
def _send_async(number, text, tag=None):
    t = threading.Thread(target=_do_send, args=(number, text, tag), daemon=True)
    t.start()
 
 
def _do_send(number, text, tag):
    req_id = "send-" + uuid.uuid4().hex[:8]
    params = {"recipient": [number], "message": text}
    if _account:
        params["account"] = _account
    request = {"jsonrpc": "2.0", "method": "send", "id": req_id, "params": params}
 
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(_socket_path)
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
 
        sock_file = sock.makefile("r", encoding="utf-8")
        response = None
        for raw_line in sock_file:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except ValueError:
                continue
            # Skip any "receive" notifications that arrive on this
            # short-lived connection too -- we only care about our reply.
            if data.get("id") == req_id:
                response = data
                break
        sock.close()
 
        if response is None:
            _display_queue.put((tag, "*** signal-cli did not respond to send ***"))
        elif response.get("error"):
            err = response["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            _display_queue.put((tag, "*** send failed: %s ***" % msg))
        # else: success, the optimistic "me: ..." echo already shown
 
    except OSError as exc:
        _display_queue.put((tag, "*** could not reach signal-cli socket: %s ***" % exc))
 
 
# ---------------------------------------------------------------------------
# Image attachment -> ASCII art.
#
# _image_to_ascii() is a pure function: no prof.* calls, no globals
# written, safe to call from the background receiver thread. It returns
# None on any failure (missing file, corrupt image, chafa not installed)
# rather than raising, so callers can fall back to a plain-text notice.
# ---------------------------------------------------------------------------
 
def _image_to_ascii(path):
    if not CHAFA:
        return None
 
    try:
        command = [
            CHAFA,
            "--format=symbols",
            "--symbols=block+border+space",
            "--colors=none",
            "-s", ASCII_SIZE,
            path,
        ]
 
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
 
        if result.returncode != 0:
            return None
 
        text = result.stdout.strip("\r\n")
        return text or None
 
    except Exception:
        return None
 
 
def _attachment_blocks(attachments, label):
    """Runs on the background receiver thread. Builds display-ready text
    blocks for any image attachments, and queues a log entry for ones
    that fail to convert. Never touches prof.* directly."""
    blocks = []
 
    for att in attachments or []:
        content_type = att.get("contentType") or ""
        if not content_type.startswith("image/"):
            continue
 
        att_id = att.get("id")
        if not att_id:
            continue
 
        path = os.path.join(_attachments_dir, att_id)
        art = _image_to_ascii(path)
 
        if art:
            blocks.append("%s sent a photo:\n%s" % (label, art))
        else:
            _display_queue.put((
                "__log_error__",
                "signal_bridge: could not convert attachment to ASCII: %s" % path,
            ))
            blocks.append("%s sent a photo (preview unavailable: %s)" % (label, path))
 
    return blocks
 
 
# ---------------------------------------------------------------------------
# Receiving: one persistent connection, read in a background thread,
# reconnect with backoff on failure. This thread never calls prof.*
# directly -- it only reads the cached _socket_path global and pushes
# work onto _display_queue for the main-thread poller to handle.
# ---------------------------------------------------------------------------
 
def _receiver_loop():
    global _listener_sock
    backoff = 1
    while not _stop_event.is_set():
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(_socket_path)
            _listener_sock = sock
            sock_file = sock.makefile("r", encoding="utf-8")
            _display_queue.put(("__log_info__", "connected to " + _socket_path))
            backoff = 1
 
            for raw_line in sock_file:
                if _stop_event.is_set():
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except ValueError:
                    continue
                _handle_notification(data)
 
            sock.close()
        except OSError as exc:
            _display_queue.put(("__log_error__", "socket error: %s" % exc))
        finally:
            _listener_sock = None
 
        if _stop_event.is_set():
            break
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
 
 
def _tag_and_label(source_number, source_name, group_info):
    if group_info:
        group_id = group_info.get("groupId", "unknown-group")
        tag = _tag_for("group:" + group_id)
        label = "%s (group %s)" % (source_name, group_id[:8])
    else:
        tag = _tag_for(source_number)
        label = source_name
    return tag, label
 
 
def _handle_notification(data):
    """Runs on the background receiver thread. Only ever touches
    _display_queue -- never calls prof.* directly."""
    if data.get("method") != "receive":
        return
    params = data.get("params") or {}
    envelope = params.get("envelope")
    if envelope is None and isinstance(params.get("result"), dict):
        # subscribeReceive-style wrapping (--receive-mode=manual)
        envelope = params["result"].get("envelope")
    if not envelope:
        return
    source_number = (
        envelope.get("sourceNumber")
        or envelope.get("source")
        or "unknown"
    )
    source_name = envelope.get("sourceName") or source_number
    data_message = envelope.get("dataMessage")
    # -----------------------------------------------------------------------
    # Signal reaction
    # -----------------------------------------------------------------------
    if data_message:
        reaction = data_message.get("reaction")
        if reaction:
            emoji = reaction.get("emoji", "")
            target_author = reaction.get("targetAuthor")
            is_remove = reaction.get("isRemove", False)
            # We are interested in reactions to our own messages.
            #
            # When "account" is configured, compare targetAuthor with it.
            # When running a single-account daemon without an explicit
            # account setting, accept the reaction as belonging to us.
            is_our_message = (
                not target_author
                or not _account
                or target_author == _account
            )
            if emoji and not is_remove and is_our_message:
                tag, label = _tag_and_label(source_number, source_name, data_message.get("groupInfo"))
                message = "%s zareagował na Twoją wiadomość %s" % (
                    label,
                    emoji
                )
                _display_queue.put(("__ensure__", tag))
                _display_queue.put((tag, message))
                _display_queue.put(("__notify__", message))
                return
    # -----------------------------------------------------------------------
    # Normal incoming message: text and/or image attachments
    # -----------------------------------------------------------------------
    if data_message:
        text = data_message.get("message")
        attachments = data_message.get("attachments")
        if text or attachments:
            tag, label = _tag_and_label(source_number, source_name, data_message.get("groupInfo"))
            _display_queue.put(("__ensure__", tag))
 
            if text:
                _display_queue.put((tag, "%s: %s" % (label, text)))
            for block in _attachment_blocks(attachments, label):
                _display_queue.put((tag, block))
 
            notify_text = text if text else "(photo)"
            _display_queue.put(("__notify__", "%s: %s" % (label, notify_text)))
            return
    # -----------------------------------------------------------------------
    # Messages sent from another linked Signal device
    # -----------------------------------------------------------------------
    sync_message = envelope.get("syncMessage")
    if sync_message and sync_message.get("sentMessage"):
        sent = sync_message["sentMessage"]
        dest = sent.get("destinationNumber") or sent.get("destination")
        text = sent.get("message")
        attachments = sent.get("attachments")
        if dest and (text or attachments):
            tag = _tag_for(dest)
            _display_queue.put(("__ensure__", tag))
 
            if text:
                _display_queue.put((tag, "me (linked device): %s" % text))
            for block in _attachment_blocks(attachments, "me (linked device)"):
                _display_queue.put((tag, block))
 
 
def _poll_queue():
    """Registered via prof.register_timed -- runs on Profanity's main
    thread. This is the ONLY place (besides command/window callbacks and
    prof_init/prof_on_unload) that is allowed to call prof.*."""
    while True:
        try:
            tag, message = _display_queue.get_nowait()
        except queue.Empty:
            break
 
        if tag == "__ensure__":
            if not prof.win_exists(message):
                prof.win_create(message, _on_window_input)
            continue
 
        if tag == "__notify__":
            prof.notify(message, 5000, "Signal")
            prof.cons_show("[Signal] " + message)
            continue
 
        if tag == "__log_info__":
            prof.log_info("signal_bridge: " + message)
            continue
 
        if tag == "__log_error__":
            prof.log_error("signal_bridge: " + message)
            continue
 
        if not prof.win_exists(tag):
            prof.win_create(tag, _on_window_input)
        prof.win_show(tag, message)
 
 
def _start_receiver():
    global _receiver_thread
    if _receiver_thread and _receiver_thread.is_alive():
        return
    _stop_event.clear()
    _receiver_thread = threading.Thread(target=_receiver_loop, daemon=True)
    _receiver_thread.start()
 
 
def _restart_receiver():
    global _listener_sock
    try:
        if _listener_sock:
            _listener_sock.close()
    except OSError:
        pass
    _start_receiver()
 
 
# ---------------------------------------------------------------------------
# /signal command -- runs on the main thread (Profanity command callback).
# ---------------------------------------------------------------------------
 
def cmd_signal(*args):
    args = [a for a in args if a is not None]
    if not args:
        prof.cons_bad_cmd_usage("/signal")
        return
 
    sub = args[0].lower()
    rest = args[1:]
 
    if sub == "open" and len(rest) >= 1:
        tag = _ensure_window(rest[0])
        prof.win_focus(tag)
 
    elif sub == "send" and len(rest) >= 2:
        number, text = rest[0], rest[1]
        tag = _ensure_window(number)
        prof.win_show(tag, "me: " + text)
        _send_async(number, text, tag)
        prof.win_focus(tag)
 
    elif sub == "socket" and len(rest) >= 1:
        _set_socket_path(rest[0])
        prof.cons_show("signal_bridge: socket path set to %s, reconnecting..." % rest[0])
        _restart_receiver()
 
    elif sub == "account" and len(rest) >= 1:
        _set_account(rest[0])
        prof.cons_show("signal_bridge: account set to %s" % rest[0])
 
    elif sub == "attachments" and len(rest) >= 1:
        _set_attachments_dir(rest[0])
        prof.cons_show("signal_bridge: attachments dir set to %s" % rest[0])
 
    elif sub == "status":
        prof.cons_show(
            "signal_bridge: socket=%s account=%s attachments=%s chafa=%s"
            % (_socket_path, _account or "(single-account daemon)", _attachments_dir, CHAFA or "NOT FOUND")
        )
 
    elif sub == "reconnect":
        _restart_receiver()
        prof.cons_show("signal_bridge: reconnecting...")
 
    else:
        prof.cons_bad_cmd_usage("/signal")
 
 
# ---------------------------------------------------------------------------
# Plugin lifecycle hooks (main thread)
# ---------------------------------------------------------------------------
 
def prof_init(version, status, account_name, fulljid):
    _load_settings()
 
    synopsis = [
        "/signal open <number>",
        "/signal send <number> <message>",
        "/signal socket <path>",
        "/signal account <account>",
        "/signal attachments <path>",
        "/signal status",
        "/signal reconnect",
    ]
    description = (
        "Bridge to a signal-cli JSON-RPC daemon socket, for sending and "
        "receiving Signal messages from Profanity. Incoming image "
        "attachments are shown as ASCII art previews."
    )
    arguments = [
        ["open <number>", "Open (or focus) a chat window for a Signal number, e.g. +15551234567"],
        ["send <number> <message>", "Send a message without opening a window (quote it if it has spaces)"],
        ["socket <path>", "Set the signal-cli daemon socket path and reconnect"],
        ["account <account>", "Set the account param for a multi-account daemon"],
        ["attachments <path>", "Set the signal-cli attachments directory (for image previews)"],
        ["status", "Show the current socket path, account and attachments dir"],
        ["reconnect", "Force a reconnect to the signal-cli socket"],
    ]
    examples = [
        "/signal open +15551234567",
        '/signal send +15551234567 "Hello from Profanity"',
        "/signal socket %s" % DEFAULT_SOCKET_PATH,
        "/signal account +15551234567",
        "/signal attachments %s" % DEFAULT_ATTACHMENTS_DIR,
    ]
 
    prof.register_command(
        "/signal", 1, 3, synopsis, description, arguments, examples, cmd_signal
    )
    prof.completer_add(
        "/signal", ["open", "send", "socket", "account", "attachments", "status", "reconnect"]
    )
    prof.register_timed(_poll_queue, 1)
 
    _start_receiver()
    prof.cons_show("signal_bridge loaded. Socket: %s" % _socket_path)
    prof.cons_show(
        "signal_bridge: image preview support (chafa): %s"
        % (CHAFA if CHAFA else "NOT INSTALLED - photos will show as a path only")
    )
 
 
def prof_on_unload():
    global _listener_sock
    _stop_event.set()
    try:
        if _listener_sock:
            _listener_sock.close()
    except OSError:
        pass

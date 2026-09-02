import os
import subprocess
 
import prof
 
STATE_FILE = os.path.expanduser("~/.cache/i3blocks-profanity-unread")
SIGNAL = 10  # SIGRTMIN+10 -- must match signal= in i3blocks config
 
_unread = set()
 
 
def _load():
    global _unread
    try:
        with open(STATE_FILE) as f:
            _unread = set(line.strip() for line in f if line.strip())
    except IOError:
        _unread = set()
 
 
def _save_and_signal():
    d = os.path.dirname(STATE_FILE)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(STATE_FILE, "w") as f:
        f.write("\n".join(sorted(_unread)))
    # -x = exact process name match, avoids hitting unrelated processes
    subprocess.Popen(["pkill", "-RTMIN+%d" % SIGNAL, "-x", "i3blocks"])
 
 
def _mark_unread(jid):
    if jid not in _unread:
        _unread.add(jid)
        _save_and_signal()
 
 
def _mark_read(jid):
    if jid in _unread:
        _unread.discard(jid)
        _save_and_signal()
 
 
# ---- Profanity plugin hooks -------------------------------------------
 
def prof_init(version, status, account_name, fulljid):
    _load()
    _save_and_signal()  # sync i3blocks with whatever state we loaded
 
 
def prof_post_chat_message_display(barejid, resource, message):
    # Fires after an incoming 1:1 message is displayed.
    if prof.get_current_recipient() != barejid:
        _mark_unread(barejid)
 
 
def prof_post_room_message_display(barejid, nick, message):
    # Fires after an incoming MUC message is displayed.
    if prof.get_current_muc() != barejid:
        _mark_unread(barejid)
 
 
def prof_on_chat_win_focus(barejid):
    _mark_read(barejid)
 
 
def prof_on_room_win_focus(barejid):
    _mark_read(barejid)
 
 
def prof_on_shutdown():
    _unread.clear()
    _save_and_signal()
 


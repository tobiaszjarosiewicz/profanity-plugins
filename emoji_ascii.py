#!/usr/bin/env python3

import os
import re
import shutil
import subprocess
import tempfile

import prof


PLUGIN_NAME = "emoji_ascii"

ENABLED = True

IMG2TXT = shutil.which("img2txt")
CONVERT = shutil.which("convert") or shutil.which("magick")
CHAFA = shutil.which("chafa")


# ---------------------------------------------------------------------------
# Emoji font
# ---------------------------------------------------------------------------

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Medium.ttf",
    "/usr/share/fonts/google-noto-emoji-fonts/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoEmoji-Medium.ttf",
]


def find_emoji_font():

    for filename in FONT_CANDIDATES:

        if os.path.isfile(filename):
            return filename

    return None


EMOJI_FONT = find_emoji_font()


# ---------------------------------------------------------------------------
# Emoji detection
# ---------------------------------------------------------------------------

# The trailing \uFE0F? consumes an optional "variation selector-16" that
# often follows a base emoji character (e.g. U+263A U+FE0F). Without it,
# the VS16 codepoint is left behind in the message after substitution.
EMOJI_RE = re.compile(
    "(?:"
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]"
    "\uFE0F?"
    ")"
)

# img2txt's "utf8" output format is "UTF-8 with ANSI escape codes", not
# plain text - it wraps the block characters in SGR colour sequences
# (\x1b[...m). Those raw escape bytes are meaningless (and look like
# garbage) once dropped into an XMPP message body, so we strip them and
# keep only the visible UTF-8 glyphs.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


# ---------------------------------------------------------------------------
# Render emoji
# ---------------------------------------------------------------------------

def render_emoji(emoji, output_file):

    if not CONVERT:

        prof.log_error(
            "emoji_ascii: ImageMagick not found"
        )

        return False

    if not EMOJI_FONT:

        prof.log_error(
            "emoji_ascii: monochrome Noto Emoji font not found"
        )

        return False

    try:

        command = [
            CONVERT,

            "-size",
            "160x160",

            "xc:black",

            "-font",
            EMOJI_FONT,

            "-pointsize",
            "110",

            "-fill",
            "white",

            "-gravity",
            "center",

            "-annotate",
            "+0+0",
            emoji,

            output_file,
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:

            prof.log_error(
                "emoji_ascii: convert error: %s"
                % result.stderr.strip()
            )

            return False

        return os.path.isfile(output_file)

    except Exception as exc:

        prof.log_error(
            "emoji_ascii: render exception: %s"
            % exc
        )

        return False


# ---------------------------------------------------------------------------
# img2txt
# ---------------------------------------------------------------------------

def image_to_text(filename):
    if not CHAFA:
        prof.log_error("emoji_ascii: chafa not found")
        return None

    try:
        command = [
            CHAFA,
            "--format=symbols",
            "--symbols=block+border+space",
            "--colors=none",        # ensures zero ANSI escape codes are emitted
            "-s", "30x15",          # width x height
            filename,
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

    except Exception as exc:
        prof.log_error("emoji_ascii: chafa exception: %s" % exc)
        return None


# ---------------------------------------------------------------------------
# Convert one emoji
# ---------------------------------------------------------------------------

def convert_one_emoji(emoji):

    filename = None

    try:

        fd, filename = tempfile.mkstemp(
            prefix="profanity-emoji-",
            suffix=".png",
        )

        os.close(fd)

        # Emoji -> PNG
        if not render_emoji(
            emoji,
            filename,
        ):

            return emoji

        # PNG -> UTF-8 art
        result = image_to_text(
            filename
        )

        if not result:

            return emoji

        return "\n" + result + "\n"

    except Exception as exc:

        prof.log_error(
            "emoji_ascii: conversion failed: %s"
            % exc
        )

        return emoji

    finally:

        if filename:

            try:
                os.unlink(filename)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Convert message
# ---------------------------------------------------------------------------

def convert_message(message):

    if not ENABLED:
        return message

    if not message:
        return message

    if not EMOJI_RE.search(message):
        return message

    return EMOJI_RE.sub(
        lambda match:
            convert_one_emoji(
                match.group(0)
            ),
        message,
    )


# ---------------------------------------------------------------------------
# Profanity hooks
#
# These fire on incoming messages just before Profanity displays them,
# not before anything is sent - so only what you *receive* gets
# converted, and outgoing messages (and the raw text actually put on
# the wire) are left untouched. Returning None here would mean "leave
# the message as-is" (unlike the *_send hooks, where None cancels
# sending), but convert_message() always returns a string so that
# distinction doesn't bite us.
# ---------------------------------------------------------------------------

def prof_pre_chat_message_display(
    barejid,
    resource,
    message,
):

    return convert_message(message)


def prof_pre_room_message_display(
    barejid,
    nick,
    message,
):

    return convert_message(message)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def cmd_emoji_ascii(*args):

    global ENABLED

    args = [
        arg
        for arg in args
        if arg is not None
    ]

    if not args:

        prof.cons_show(
            "[emoji_ascii] %s"
            % (
                "enabled"
                if ENABLED
                else "disabled"
            )
        )

        return

    command = args[0].lower()

    if command == "on":

        ENABLED = True

        prof.cons_show(
            "[emoji_ascii] enabled"
        )

    elif command == "off":

        ENABLED = False

        prof.cons_show(
            "[emoji_ascii] disabled"
        )

    elif command == "test":

        result = convert_one_emoji("😀")

        prof.cons_show(
            "[emoji_ascii] test result:"
        )

        for line in result.splitlines():

            prof.cons_show(
                line
            )

    else:

        prof.cons_bad_cmd_usage(
            "/emoji-ascii"
        )


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def prof_init(
    version,
    status,
    account_name,
    fulljid,
):

    synopsis = [
        "/emoji-ascii on|off|test"
    ]

    description = (
        "Convert emoji to UTF-8 text art "
        "using ImageMagick and img2txt."
    )

    arguments = [
        [
            "on|off|test",
            "Enable, disable or test emoji conversion.",
        ],
    ]

    examples = [
        "/emoji-ascii on",
        "/emoji-ascii off",
        "/emoji-ascii test",
    ]

    prof.register_command(
        "/emoji-ascii",
        0,
        1,
        synopsis,
        description,
        arguments,
        examples,
        cmd_emoji_ascii,
    )

    prof.completer_add(
        "/emoji-ascii",
        [
            "on",
            "off",
            "test",
        ],
    )

    prof.cons_show(
        "[emoji_ascii] loaded"
    )

    prof.cons_show(
        "[emoji_ascii] img2txt: %s"
        % (
            IMG2TXT or "NOT FOUND"
        )
    )

    prof.cons_show(
        "[emoji_ascii] ImageMagick: %s"
        % (
            CONVERT or "NOT FOUND"
        )
    )

    prof.cons_show(
        "[emoji_ascii] font: %s"
        % (
            EMOJI_FONT or "NOT FOUND"
        )
    )

# profanity-plugins

## Profanity notifications for i3blocks
Tracks which Profanity conversations have unread messages and shares that state with an i3blocks 
status-bar blocklet, so the bar can show an unread-message icon.

### Dependencies

* profanity-notifications i3blocklet

### Installation

In Profanity:  
`/plugins install prof-unread.py`

## Emoji ASCII converter
Turns emoji in incoming chat messages into small block-character "ASCII art" renderings.

### Dependencies

* chafa
* UTF-8 font (check default font directory)

### Installation

In Profanity:  
`/plugins install emoji_ascii.py`

## Signal client
Turns Profanity into a client for Signal, by talking to a locally-running signal-cli 
daemon over its JSON-RPC Unix socket. Each Signal contact/group gets its own Profanity 
window (Signal +1555...), and incoming photos are rendered as chafa-generated ASCII art previews.

### Dependencies

* signal-cli (with the device registered as a secondary device in the Signal app)
* chafa (for the images)

### Installation

In Profanity:  
`/plugins install signal_bridge.py`


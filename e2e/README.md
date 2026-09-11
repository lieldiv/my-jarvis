# Browser tests

Drives the real HUD in a real browser at real device sizes. Catches the class
of bug the node-based unit suites structurally cannot: JavaScript that only
fails once the page runs, layout that only breaks at 320px, and the
record → upload → transcript loop end to end.

## What it does NOT cover

This is Chromium, not iOS Safari. Apple's speech engine and iOS's single
shared audio session — the two things that actually broke on the iPhone — do
not exist here. **A green run means the code is sound, not that the iPhone is
happy.** Only a real device can tell you that; the in-app voice diagnostics
panel exists for exactly that reason.

A real iOS Simulator needs macOS and Xcode, so it cannot run on this machine
at all.

## Running

Once:

    cd e2e
    npm run setup

Then, in two terminals:

    npm run serve      # starts JARVIS on 127.0.0.1:5099, signed in
    npm test

`serve.py` writes a validly-signed session cookie so the browser is past the
Google gate without needing Google, and generates a WAV that Chromium feeds in
as a fake microphone. `/api/transcribe` and `/api/command` are intercepted in
the browser, so no API keys are needed and no request leaves the machine.

Screenshots land in `shots/`.

## Devices covered

iPhone SE (320px), iPhone 15, Pixel 7, iPad Pro, desktop.

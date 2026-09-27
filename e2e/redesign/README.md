# Redesign test suites

Regression checks for the September 2026 UX redesign (calm content pages, guide, Tasks / Inbox / Settings / Home
"your day" card, the calm orb) plus the `open_web_page` feature. They drive the real HUD in Chromium at a Pixel 7
size, right-to-left, in Hebrew.

## Run

    cd e2e
    npm run setup            # once
    npm run serve            # terminal 1: the HUD on 127.0.0.1:5099, signed in, throw-away database
    npm run test:redesign    # terminal 2: every suite below, one line each

`E2E_URL` points the browser suites at a different server; `PYTHON` picks the interpreter.

## What is in here

| File | Checks |
|---|---|
| `unit_events.py` | calendar data: all-day events, real dates, timezone (the old "Sun 03:00" bug) |
| `unit_inbox.py` | a mailbox / calendar that does not answer is reported as *not connected*, never as *empty* |
| `unit_web_page.py` | the `open_web_page` tool: URL building, refusal of unsafe targets, prompt wording |
| `test_step1_foundation.mjs` | one opaque dock, icon sprite, calm content surfaces, reply bubble, no emoji chrome |
| `test_step2_agenda.mjs` | the day-grouped Agenda, add sheet, row actions, Home card, all-day handling |
| `test_step3_guide.mjs` | help screen, first-run coach, tour, microphone notes |
| `test_step4_screens.mjs` | Tasks, Inbox, Home "your day", Settings, the orb states, the create button |
| `test_smoke_regression.mjs` | tabs open, a typed command answers, a task can be added through the UI |
| `test_web_page_card.mjs` | the confirmation card for opening a web page |
| `mockdata.mjs` | realistic API payloads shared by the suites (the browser answers `/api/...` itself) |

## What a green run does not prove

* It is Chromium, not iOS Safari or a real Android phone. Layout and behaviour are checked; Apple's speech engine and
  the phone's own permission dialogs are not.
* The Google / Gmail / Calendar APIs are not called. The payloads are the shapes the server sends, not live data.
  In particular Gmail's `internalDate` (the "2 hours ago" on each message) is read on the server but has only been
  checked against the shape of the API, not a real mailbox.
* Text is checked; taste is not. Screenshots go to `shots/` for a human to look at.

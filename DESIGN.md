# DESIGN.md — J.A.R.V.I.S. / Stark HUD

A holographic instrument panel, not a consumer app. Every surface should read as
*projected light on glass* — emissive, semi-transparent, technical. If a screen could
plausibly appear in a product-design tool instead of a helmet visor, it is wrong.

---

## 1. Visual Theme & Atmosphere

- **Mood:** cold, precise, alive. Machined instrumentation with a pulse.
- **Light model:** everything *emits*. There is no ambient light source and no cast
  shadows — depth comes from glow intensity and layered transparency.

### The three-layer model — the spine of this system

Every screen is built from exactly three stacked layers. Which layer an element belongs
to determines its color, its density, and whether it can be touched. Get this wrong and
nothing else in this document will save the design.

| Layer | What it is | Color | Interactive? | Density |
|---|---|---|---|---|
| **Substrate** | ambient technical texture — micro-text, leader lines, schematic rings, wireframes, drifting readouts | cyan at 8–20% opacity | **Never** | Maximal |
| **Structure** | panel frames, borders, labels, headings, real readable content | cyan, full strength | Sometimes | Moderate |
| **Live** | the orb, active processing, anything happening *right now* | gold | Sometimes | Sparse |

**The substrate is the answer to "how do I get the reference density onto a phone."**
All the cinematic noise — the illegible annotation blocks, the callout lines, the
concentric gauges — lives here, behind everything, non-interactive and unreadable *by
design*. It is texture, not information. It is what makes a 390px screen feel like a
Stark workstation without putting a single extra tap target on it.

Because the substrate carries the density, the Structure and Live layers are free to be
sparse and finger-sized. This is what resolves the density-vs-touch tension in §8 — the
two demands live on different layers and never compete.

- **Never** let a substrate element receive a tap, take focus, or be read by a screen
  reader. `pointer-events: none; aria-hidden="true"` on the whole layer.
- Substrate never exceeds 20% opacity. If you can comfortably read it, it's too loud.
- **Base canvas:** near-black navy `#020813` under all three, with two always-on fields:
  1. Centered radial bloom — `radial-gradient(circle at 50% 50%, rgba(0,100,180,.15) 0%, transparent 80%)`
  2. Technical grid — 30px × 30px cyan lines at 7% opacity
- **Motion:** continuous low-amplitude life. Scanlines, slow pulses, sweeping arcs.
  Nothing bounces, nothing eases playfully. Use `linear` and `ease-out`, never
  `ease-in-out` on entrances, never spring/overshoot.

---

## 2. Color Palette & Roles

```css
:root {
  /* Core */
  --primary:        #00f3ff;               /* system voice, borders, most text */
  --primary-glow:   rgba(0, 243, 255, .4);
  --bg-dark:        #020813;               /* canvas */
  --panel-bg:       rgba(4, 18, 38, .75);  /* all panels — never opaque */
  --border-color:   rgba(0, 243, 255, .25);
  --text-dim:       #82b6d1;               /* secondary text, metadata */

  /* Semantic — these carry MEANING, never decoration */
  --live-gold:      #ffc800;   /* the system ALIVE — orb, processing, active */
  --live-glow:      rgba(255, 200, 0, .45);
  --user-hot:       #dff6ff;   /* HUMAN origin — near-white, cyan-tinted */
  --red-alert:      #ff3b3b;   /* errors, destructive, offline */
  --green-active:   #00ff88;   /* healthy, connected, confirmed */

  /* Derived surfaces */
  --panel-bg-solid: rgba(2, 10, 25, .9);   /* headers, sticky bars */
  --border-strong:  rgba(0, 243, 255, .5); /* focus, active panel */
  --text-faint:     rgba(130, 182, 209, .5);
  --white-core:     #ffffff;               /* ONLY as glow core / text-shadow */
}
```

**Role discipline — this is the single most important rule in this file.**

Colors map to the three layers in §1, and the scheme is **three temperatures**:

| Color | Temperature | Means | Never use for |
|---|---|---|---|
| `--primary` cyan | cold | structure — frames, labels, chrome, grid, substrate | anything currently happening |
| `--live-gold` | warm | the system *alive* — the orb, processing, active telemetry | inert frames or static labels |
| `--user-hot` | hot | human origin — user turns, user input, the mic | system output |
| `--green-active` | — | healthy / connected / done | primary actions |
| `--red-alert` | — | fault / destructive / offline | emphasis |

Read it as heat: **cold structure holds the frame, warm gold shows the machine
thinking, hot white is the human.** The human is the brightest thing on screen because
the human is what the machine is for.

Why white rather than a fourth hue: gold now belongs to the live layer, so the human
needed a signal that reads instantly against both cold cyan and warm gold. Near-white
`--user-hot` with a white-core glow wins on luminance rather than hue, so it stays
unambiguous even where gold and cyan sit side by side.

> **Renamed:** `--user-gold` is now `--live-gold`. Same value `#ffc800`, different job.
> Every existing use of `--user-gold` must be re-examined, not blindly renamed — if it
> marked *user* content it becomes `--user-hot`; if it marked *activity* it becomes
> `--live-gold`. Corner brackets (§4) stay gold; they frame instrument readouts, which
> are the live layer.

A user message is white-hot with a white-core glow; a system response is gold-framed;
the panel holding both is cyan. That three-way contrast carries the whole conversation
hierarchy — do not dilute it with a further accent.

---

## 3. Typography Rules

Two families only. Loaded from Google Fonts.

```css
--font-display: 'Orbitron', sans-serif;      /* 400 / 700 / 900 */
--font-mono:    'Share Tech Mono', monospace; /* everything else */
```

- **Orbitron** — headings, the wordmark, numeric displays, section titles. Always
  `letter-spacing: 2px–4px`, weight 700 or 900. Never below 16px (it becomes illegible).
- **Share Tech Mono** — body, labels, telemetry, buttons, input. This is the default
  on `*`.

**Type scale — use these seven, nothing between them:**

```css
--fs-micro:   10px;  /* axis labels, corner refs, unit suffixes */
--fs-caption: 11px;  /* metadata, timestamps, chip text */
--fs-body:    13px;  /* default reading size */
--fs-lg:      16px;  /* emphasized body, list titles */
--fs-h3:      20px;  /* panel titles */
--fs-h2:      24px;  /* screen titles, wordmark */
--fs-display: 40px;  /* hero numerics only */
```

Uppercase every label under 13px, with `letter-spacing: 1px`. Body copy stays
sentence case.

---

## 4. Component Stylings

### Panel (the base primitive)
```css
background: var(--panel-bg);
border: 1px solid var(--border-color);
border-radius: var(--r-base);
backdrop-filter: blur(5px);
box-shadow: var(--glow-inset);
```
Panels are translucent so the grid shows through. **Never** give a panel an opaque
background.

### Corner brackets
The signature framing device. Instead of a heavier border, mark the two opposing
corners with 12px L-shaped gold rules (`--live-gold`, 1px, no radius) offset 4px
outside the panel edge. Use on cards that represent instrument readouts.

### Technical reference labels
Every significant panel carries a small monospace ID in a corner —
`REF_CAL.03`, `SYS_NET.11` — at `--fs-micro`, `--text-faint`, `letter-spacing: 1px`.
Invent plausible, *stable* codes per panel type. This is what sells the schematic
language.

### Substrate motifs — the reference vocabulary

These six live on the **substrate layer** (§1): cyan, 8–20% opacity, `pointer-events:
none`, `aria-hidden`. They are the difference between "a dark app" and "a Stark
workstation." Use 2–4 per screen, never all six.

1. **Leader-line callouts.** A 1px line from a point on the subject out to a small text
   block, with a 3px dot at the origin and a short right-angle elbow. The label is
   `--fs-micro` monospace. Lines run horizontal or 45° only — never arbitrary angles.
   This is the single most characteristic motif in the references; if you use one thing,
   use this.
2. **Micro-text blocks.** Paragraphs of 5–6px monospace, deliberately below the
   legibility threshold. Texture, not content. Never put real information here, and
   never let it be selectable.
3. **Concentric schematic rings.** Arc-reactor cross-sections: 3–5 nested circles, radial
   tick marks around the circumference, one or two arc segments at differing radii, a
   couple of ring gaps. Slowly counter-rotating via `jarvis-spin`.
4. **Reticles and target brackets.** Crosshairs, four-corner focus brackets, boxes with
   an X through them, small `+` registration marks at panel intersections.
5. **Wireframe / contour overlay.** A mesh, topographic contour, or exploded technical
   diagram behind the content. Thin cyan lines, no fill.
6. **Segmented bar meters.** Rows of 8–16 small rectangles, partially filled — the
   `F9 - TRANS` readouts from the references. Some static, some drifting.

**Density budget so this stays a phone and not a poster:** at most **one** dominant
motif (rings or wireframe) plus **two** supporting ones per screen. Substrate must never
occupy more than roughly a third of the visible area, and it must never sit directly
behind body text — push it to margins, corners, and the space around the orb.

### Buttons
- Base: transparent fill, 1px `--border-color`, `--r-base`, `--fs-body`, uppercase.
- Hover: border → `--border-strong`, add `--glow-md`, text → `--white-core`.
- Active/pressed: background `rgba(0,243,255,.12)`, no translate.
- Disabled: 35% opacity, no glow, `cursor: not-allowed`.
- Destructive: swap cyan for `--red-alert` throughout.

### Chips / quick actions
Pill-shaped (`--r-pill`), `--fs-caption`, 1px border, icon + label, ~34px tall.
Hover lifts the border and adds `--glow-sm`. These are the row of shortcuts at the
top of a screen.

### Status dot
8px circle, `--r-round`, filled with its semantic color, `box-shadow: var(--glow-sm)`
in that same color. Slow 2s opacity pulse when live.

### Input
Transparent, bottom border only (1px `--border-color`), `--font-mono`, caret in
`--primary`. On focus: bottom border → `--border-strong` plus `--glow-md` beneath.
Never a filled input box.

### The orb — the centrepiece

The single most important object in the product. It is **gold** (`--live-gold`), not
cyan — it is the live layer made visible. Build it from three stacked parts:

1. **Filament core.** Not a flat radial gradient. A turbulent mesh of fine gold
   filaments — many thin arcs of varying opacity woven into a sphere, denser toward the
   centre, wispy at the rim. The texture should read as contained energy, never as a
   smooth ball. SVG paths or canvas; a plain `radial-gradient` is a failed orb.
2. **Orbital rings.** Two or three ellipses circling the core at differing tilts,
   counter-rotating via `jarvis-spin`. Rotation speed encodes state — fast while
   processing, slow at idle (§6).
3. **Bloom.** `--glow-xl` in gold. The orb is the only element permitted `--glow-xl`.

A white-hot centre (`--white-core`) sits at the very middle, tightest and brightest.

**State is carried by three channels at once** — color, ring speed, and filament
turbulence — so it stays readable at a glance and survives reduced-motion:

| State | Color | Rings | Filaments |
|---|---|---|---|
| Idle | `--live-gold`, dimmed | 16s / 24s | calm, low amplitude |
| Listening | `--user-hot` | 8s / 12s | drawing inward |
| Processing | `--live-gold`, full | 4s / 7s | high turbulence |
| Confirmed | `--green-active` | slowing to idle | settling |
| Fault | `--red-alert` | stalled, juddering | collapsed, sparse |

On mobile the orb is ~40vw and sits in the **upper-middle** of the screen — it is a
display object, not a control. Keep it clear of the thumb zone (§8). If the orb itself
must be tappable, give it a separate 44px control nearby rather than making a 40vw
target.

---

## 5. Layout Principles

4px base unit. Use only these:

```css
--sp-1: 4px;   --sp-2: 8px;   --sp-3: 12px;  --sp-4: 16px;
--sp-5: 24px;  --sp-6: 32px;  --sp-7: 48px;
```

- Screen padding: `--sp-4`. Panel inner padding: `--sp-3`. Gap between panels: `--sp-3`.
- Align everything to the 30px background grid where practical — panel edges landing
  on grid lines is what makes it feel engineered.
- Radii — five values, no others:

```css
--r-sharp: 2px;   /* tags, tiny markers */
--r-base:  4px;   /* panels, buttons, inputs — the default */
--r-lg:    8px;   /* modals, major cards */
--r-pill:  999px; /* chips */
--r-round: 50%;   /* orbs, dots */
```

Delete 3px, 5px, 6px, and 20px on sight — they are historical accidents.

---

## 6. Depth, Elevation & Motion

Depth is **glow intensity**, never a dark drop shadow. Five tokens:

```css
--glow-sm:    0 0 8px;                                    /* dots, small markers */
--glow-md:    0 0 14px rgba(0,243,255,.25);               /* hover, focus */
--glow-lg:    0 0 35px var(--primary-glow);               /* active panel */
--glow-xl:    0 0 90px rgba(0,243,255,.4),
              inset 0 0 32px rgba(0,243,255,.08);         /* orb, modals only */
--glow-inset: inset 0 0 20px rgba(0,243,255,.03);         /* every panel, always */
```

Recolor by substituting the semantic hue — an alert panel uses the same geometry with
`--red-alert`. Never invent a new blur radius; pick the nearest token.

### Motion tokens

```css
--dur-fast:   150ms;   /* state changes: press, focus, hover */
--dur-med:    400ms;   /* panels entering, sheets, disclosure */
--dur-slow:   2000ms;  /* ambient sweep periods */
--ease-out:   ease-out;  /* anything entering or responding */
--ease-linear: linear;   /* anything looping, always */
```

Looping ambience is `--ease-linear` without exception — a pulse or scan that eases
reads as organic, and this system is machined. Discrete interactions are `--ease-out`.
There is no `ease-in-out` and no spring anywhere in this system.

**Named animations.** Three ambient loops are part of the language, not ad-hoc:

| Keyframe | Purpose | Duration / easing |
|---|---|---|
| `jarvis-pulse` | live status dots, active telemetry | 2s `--ease-linear` infinite |
| `jarvis-spin` | orb rings, loading, the glyph mark | **variable** `--ease-linear` infinite |
| `jarvis-scan` | scanline sweep across a panel | 3s `--ease-linear` infinite |

**`jarvis-spin` is deliberately variable, and this is a feature.** Rotation *speed*
encodes state: roughly 4s/7s for the counter-rotating orb rings while processing,
16s/24s while idle. The user reads "it's working" from tempo before reading any label.
Never flatten this to a single duration. Any other spinner in the system inherits the
same principle — if it can express state through speed, it should.

At least one element per screen must carry an ambient loop — a fully static screen
reads as a dead instrument. Never run more than three at once; the panel should feel
alive, not frantic.

Respect `prefers-reduced-motion: reduce` — drop all three ambient loops and collapse
durations to `0ms`. State must remain readable from color and glow alone.

---

## 7. Do's and Don'ts

**Do**
- Keep every panel translucent so the grid reads through it.
- Label everything. Unlabelled numbers are wasted opportunity.
- Use amber exclusively for human-origin content.
- Add scanline / sweep motion to at least one element per screen.
- Let text glow (`text-shadow: 0 0 12px var(--primary)`) on headings.

**Don't**
- No pure-white fills. White exists only as a glow core or `text-shadow`.
- No drop shadows, no `filter: drop-shadow`, no dark elevation.
- No border-radius above 8px except pills and circles.
- No third accent color. Four semantic hues is the entire vocabulary.
- No emoji as UI iconography — use geometric/SVG glyphs. (Emoji in *content* is fine.)
- No `ease-in-out` entrances, no spring, no bounce, no scale-up-on-hover.
- No opaque modals — dim the backdrop with `rgba(2,8,19,.8)` + blur instead.
- Never center long body text. Left-align (or right-align in RTL).

---

## 8. Responsive Behavior — Mobile First, Non-Negotiable

**This is a phone product.** Design every screen at **390×844** first and get it right
there. Desktop must work, but it is the adaptation, never the origin. Never design wide
and shrink down — that is how the density collapses and the thumb ergonomics break.

- Breakpoints: `480px`, `768px`, `1200px`. Write mobile styles as the base and use
  `min-width` queries only. No `max-width` queries.
- Desktop is **not** a wider phone. Above 1200px, cap the content column at ~900px and
  use the reclaimed space for persistent side telemetry — do not stretch panels.

### Touch

- Minimum target **44×44px**, always. Chips may *look* smaller than 44px but their hit
  area must not be — pad them out with transparent space.
- Minimum **8px** between adjacent targets. Two glowing chips touching each other is a
  mis-tap generator.
- **There is no hover on a phone.** Every affordance must be legible from its resting
  state. Hover glow is a desktop enhancement layered on top, never the thing that tells
  the user something is tappable.
- Give every tap an immediate `:active` response within `--dur-fast`. On touch, the
  press state *is* the feedback.
- Put primary actions in the **bottom third** of the screen — that is thumb reach. The
  top of a 844px screen is for display, not for controls.

### The density tension — read this carefully

§1 demands dense telemetry. Touch demands large, spaced targets. On a 390px screen
these fight, and the resolution is a hard split:

- **Read-only telemetry** — dense, small, tightly packed. `--fs-micro` and
  `--fs-caption` are fine. This is where the HUD character lives.
- **Anything interactive** — sparse and large. Never below 44px, never crowded.

If a readout needs to be tappable, do not shrink the target — promote it into its own
row. Never compromise the touch minimum for density.

### Viewport mechanics

- Use `100dvh`, never `100vh` — mobile browser chrome collapses and `100vh` will clip
  the orb or push the input off-screen.
- Respect safe areas: `padding: env(safe-area-inset-top) env(safe-area-inset-right)
  env(safe-area-inset-bottom) env(safe-area-inset-left)`. The notch and the home
  indicator will otherwise eat the header and the input bar.
- **Inputs must be `16px` at minimum on mobile.** Below that, iOS Safari auto-zooms the
  page on focus and the user is stranded. This overrides the type scale — it is the one
  sanctioned exception in this document.
- Only the content column scrolls; header, orb and input bar stay fixed. Add
  `overscroll-behavior: contain` so scrolling a panel doesn't drag the whole page.
- The grid background stays 30px at every size — do not scale it.

### Layout at ≤480px

- Panels go full-bleed edge-to-edge with `--sp-3` inner padding.
- Multi-column telemetry collapses to a single column.
- The orb shrinks to ~40vw and must never overlap the input bar when the keyboard opens.
- Landscape: the orb drops out entirely and telemetry goes two-column. Do not try to
  preserve the portrait composition sideways.

### RTL — mandatory
The product ships in **Hebrew**. Every layout must work in both directions.

- Set `dir="rtl"` on the container; use logical properties (`margin-inline-start`,
  `padding-inline`, `inset-inline-start`) rather than `left`/`right`.
- Mirror: corner brackets, chevrons, progress direction, drawer slide-in.
- Do **not** mirror: the orb, clocks, media controls, chart axes, or Latin technical
  refs like `REF_CAL.03`.
- Numerals and times stay LTR inside RTL text — wrap them in `<bdi>`.
- Orbitron has no Hebrew coverage. For Hebrew headings use Share Tech Mono's fallback
  or a Hebrew display face; never let Orbitron silently fall back mid-heading.

---

## 9. Agent Prompt Guide

Reusable prompts for generating new screens in this system.

**New screen**
> Design a `<screen name>` screen for the JARVIS HUD, mobile 390×844, in Hebrew RTL.
> Follow DESIGN.md exactly: translucent panels on the 30px grid, cyan for system and
> amber for user-origin content, the seven-step type scale, glow-based depth only.
> Include a technical reference label on each panel. Dense telemetry over empty space.

**New component**
> Add a `<component>` to the JARVIS design system. Show all states — default, hover,
> focus, active, disabled, loading, error — as separate variants side by side. Use only
> existing tokens from DESIGN.md; if you need a value that isn't tokenized, propose the
> token rather than hardcoding.

**Critique pass**
> Audit this screen against DESIGN.md. List every hardcoded value that should be a
> token, every color used outside its semantic role, every radius or font-size off the
> scale, and every RTL break. Output as a table — do not fix anything yet.

**Raising the ceiling**
> This is technically correct but visually safe. Push it: add a signature detail that
> makes this screen memorable — an unexpected data visualization, a sweeping animation,
> a layered depth effect. Stay strictly inside the token system. Show me three distinct
> directions, not three variations of one.

---

## 10. Dimensionality — the HUD is a Volume, Not a Page

The references are not flat screens. They are **projections floating in space**: panels
tilted away from the viewer, wireframes with visible depth, annotation lines running
*behind* and *in front of* the subject. A flat stack of rectangles will never look like
this, no matter how good the colors are.

### Perspective

```css
/* On the screen root */
perspective: 1200px;
perspective-origin: 50% 40%;
transform-style: preserve-3d;
```

Each layer from §1 sits at a real depth:

| Layer | `translateZ` | Effect |
|---|---|---|
| Substrate | `-120px` | recedes, softens, parallaxes least |
| Structure | `0` | the reading plane |
| Live / Orb | `+60px` | floats forward, catches the eye |

### Tilted planes

Secondary panels — telemetry blocks, side readouts, anything not being read right now —
sit at a slight angle: `rotateY(±4deg)` or `rotateX(3deg)`. Never more than 6°; past
that text degrades and it reads as a gimmick. **The panel the user is actually reading
is always flat at 0°.** Tilt is for context, never for content.

Give tilted panels a faint edge highlight on the leading side (1px, `--primary` at 40%)
so the plane catches light like glass.

### Parallax

Two sources, both cheap:

1. **Scroll** — substrate translates at 0.3× the content's rate, live layer at 1.15×.
2. **Device tilt** — on mobile, `deviceorientation` shifts the layers by up to ±12px in
   opposite directions. This is the single most effective "it's alive" cue on a phone,
   and almost nobody does it. Request permission on first interaction, never on load,
   and degrade silently if refused.

Clamp both. Parallax that overshoots reads as broken, not immersive.

### The orb in 3D

The orb's rings are genuine 3D ellipses on differing axes (`rotateX(70deg)`,
`rotateY(20deg)`), counter-rotating in space rather than being drawn as flat ovals. If
performance allows, build the filament core in WebGL; otherwise layered SVG with
`transform-style: preserve-3d` is acceptable.

### Performance — non-negotiable on a phone

- Animate **only** `transform` and `opacity`. Never `top`, `left`, `width`, or
  `box-shadow` in a loop.
- `will-change: transform` on parallax layers only, and remove it when idle.
- Cap the substrate at ~200 rendered elements. Beyond that, bake it to a single SVG.
- Budget: 60fps on a mid-range Android. If a 3D effect can't hold that, cut it — a
  smooth flat screen beats a stuttering deep one every time.
- Honour `prefers-reduced-motion`: parallax and tilt off, static Z-depth retained.

---

## 11. Aliveness — the System Is Always Doing Something

Tony Stark's interfaces are never idle-looking. They boot, they scan, they report on
themselves. A static screen is a dead machine.

### The boot sequence

Loading is not a spinner — it is the system **coming online in front of you**. On first
paint, run a diagnostic readout: sequential monospace lines, each appearing with a short
delay, each with a status dot that resolves from cyan to green.

```
מאתחל ליבת JARVIS…            ●
טוען פרוטוקולי אבטחה…          ●
מסנכרן יומן ודואר…             ●
המערכת מוכנה.                  ●
```

Rules: total duration **under 1.4s** — theatre must never become a wait. Lines resolve
in order, 180–260ms apart. The orb ignites on the final line. If real loading finishes
early, let the sequence complete anyway; if it runs long, hold on the last incomplete
line rather than faking completion. **Never show a fake progress bar.**

The sequence runs on cold start only. Returning within the session gets a 200ms fade —
respect the user's time.

### Idle behaviour

No screen is ever fully still. At minimum, at all times:

- The orb breathes — filament turbulence at low amplitude.
- One substrate element drifts, sweeps, or re-scans on a slow cycle.
- Live telemetry values tick, even if only the last digit.

Keep it below conscious notice. If the user *watches* the animation, it's too strong.

### Reaction

The system should feel like it noticed you:

- **Touch** — a 1px ring expands from the touch point and fades in `--dur-fast`.
- **Tilt** — the parallax from §10.
- **Focus** — the active panel brightens its border to `--border-strong` and its
  neighbours dim by 15%. Attention is visible.
- **Voice** — the orb's filaments respond to input amplitude in real time. Not a
  decorative waveform — actual reactivity, or nothing.

### Self-report

Somewhere on the home screen, persistently: a small system-status block showing what
JARVIS is *actually* doing — services connected, last sync, current latency. Real values
only. This is the difference between a costume and a machine. **Never invent telemetry
to look impressive** — a readout the user learns is fake poisons every other readout on
the screen.

---

## 12. Build Protocol — How to Work on This System

This section is for the agent building the design, not for the design itself. Follow it
literally.

### Definition of done

A task is done when the result has been **rendered and looked at**, at 390×844, in
Hebrew RTL. Not when the files are written. Not when the code "should work."

Before reporting completion, state explicitly:
1. What you rendered and what you saw.
2. Which requirements you verified, one line each.
3. Anything you **could not** verify, and why.

If you could not render it, say so in the first sentence of your reply. Do not bury a
verification failure under a success summary.

### Never silently skip

If a request has numbered parts, address every part or explicitly say which you are
deferring and why. Skipping item 1 and delivering items 2–4 without comment is a failure
even if items 2–4 are perfect.

### Self-check before shipping

Run this list against your own output and report the result — every item, pass or fail:

- [ ] Every value comes from a token. No hardcoded hex, px, or ms.
- [ ] Every color is inside its temperature role (§2). Gold is never user content.
- [ ] Substrate is `pointer-events: none` and `aria-hidden`.
- [ ] Every touch target ≥44px with ≥8px clearance.
- [ ] Nothing depends on hover to signal that it is interactive.
- [ ] `100dvh` not `100vh`; safe-area insets present; inputs ≥16px.
- [ ] Layout works in RTL, with numerals in `<bdi>`.
- [ ] `prefers-reduced-motion` honoured; all states still distinguishable.
- [ ] Loops use `--ease-linear`; interactions use `--ease-out`.
- [ ] Substrate ≤⅓ of visible area; ≤1 dominant motif + 2 supporting.
- [ ] Animates only `transform`/`opacity`.

### Use parallel agents

Independent components (Button, Chip, Input, StatusDot) have no shared state — build
them concurrently rather than sequentially. Reserve sequential work for things that
genuinely depend on each other: tokens → components → screens.

After a build pass, run a **separate critic pass** with fresh eyes: re-read this document
and audit the output against it as if someone else had written the code. The builder and
the critic should not be the same reasoning pass — self-review immediately after
building reliably misses what building just normalized.

### When to stop and ask

Stop and ask rather than guessing when:
- A change would rename or repurpose a token already in use.
- This document contradicts itself, or contradicts something already built.
- A requirement is impossible as specified — say so plainly instead of shipping a
  degraded version and calling it done.
- You are about to build something large that has never been visually verified.

### Report honestly

If quota, tooling, or sandbox limits block you, say it immediately and stop at a clean
checkpoint. A truthful "I could not verify this" is worth more than a confident summary
of work nobody has seen. Every unverified claim in this project has cost more time to
undo than it saved.

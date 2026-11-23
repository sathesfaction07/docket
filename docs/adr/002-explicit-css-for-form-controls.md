# ADR 002: Style form controls with explicit CSS instead of utility opacity classes

## Status

Accepted

## Context

The dashboard is a React 18 SPA loaded from `frontend/index.html` with Tailwind pulled in from `https://cdn.tailwindcss.com`. No build step, no bundler — open the file and it runs.

The surface styling follows the project's dark theme: near-black background, cards at a few percent white over it. Expressed as Tailwind utilities, a card is `bg-white/3 border border-white/6`, which is concise and reads well.

Applying the same idea to form controls produced a bug that cost real time to find. Text inputs rendered with a white or near-white background and near-white text. The field was there, focusable, and accepting input — the input was simply invisible while typing. Nothing in the console, nothing that looked wrong in the markup.

Two things were happening at once:

1. **User-agent styles on form controls are unusually stubborn.** Browsers ship their own `background-color` and `color` for `input`, `textarea` and `select`, and on some platforms those defaults do not lose to a low-specificity utility class the way an ordinary `div` would.
2. **The CDN build generates utilities at runtime.** Tailwind's browser script scans the DOM and produces CSS on the fly. For markup React mounts after that scan, the utility can be missing entirely — so `bg-white/3` resolves to nothing and the control keeps its default background. The failure is intermittent and depends on mount timing, which is what made it look like a rendering glitch rather than a CSS problem.

The combination is nasty because a translucent utility that fails silently and a browser default that is nearly white land on the same visual result: an invisible field.

## Decision

Do not style form controls with utility classes. Declare them once in a literal `<style>` block, with `!important` on the three properties that were being overridden:

```css
input, textarea, select {
    background: #111827 !important;
    border: 1px solid #1f2937 !important;
    color: #e5e7eb !important;
    border-radius: 12px;
    padding: 10px 14px;
    font-size: 13px;
    outline: none;
}
```

Three related cases need their own rules, because they do not inherit from the block above:

```css
input:focus, textarea:focus, select:focus { border-color: rgba(0,212,170,0.5) !important; }
input::placeholder, textarea::placeholder { color: #4b5563 !important; }
select option { background: #111827; color: #e5e7eb; }
```

`select option` is the one most often missed. Native dropdown items are rendered by the OS and do not pick up the parent `select` colours, so on a dark theme the open dropdown reverts to light styling unless it is targeted directly.

Opaque hex values are used rather than translucent ones. Card surfaces still use translucency, but written as literal `rgba(255,255,255,0.03)` in the same stylesheet rather than as a generated utility — so the surface treatment does not depend on the CDN's runtime scan either.

## Consequences

**Good**

- Form fields are always legible. The failure mode is gone rather than mitigated
- The rule is one block in one place, so a theme change is a single edit
- It holds regardless of when React mounts a control, because it does not depend on utility generation
- Opaque input backgrounds are the honest choice anyway. A translucent input over a card over a background stacks three layers of alpha, and the resulting contrast ratio is hard to reason about

**Bad**

- `!important` is normally a smell, and there are six of them here. Anything that legitimately needs to override an input colour later has to escalate further
- Form styling and everything-else styling now live in two different systems, which is inconsistent for anyone reading the file expecting Tailwind throughout
- The colours are duplicated between this block and the Tailwind config, so a palette change has two places to touch

**Neutral**

- This is treated as a standing rule for every UI in the portfolio, not a one-off patch. The bug is a property of Tailwind-via-CDN plus form controls, not of this project
- The proper fix is a real Tailwind build step with a form plugin, which would remove both the runtime-generation problem and the need for `!important`. That is a deliberate deferral: a build step costs the "open the file and it runs" property, which is worth more here than stylistic purity

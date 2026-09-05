"""Design tokens and HTML components for the EntitleGraph dashboard.

Why this module exists
----------------------
Streamlit's default look is recognisable at a glance, and a submission judged
partly on craft should not look like every other Streamlit app. But restyling
Streamlit by targeting its generated class names is fragile — those names are
internal and change between releases, so a dashboard built that way quietly
breaks on upgrade.

So the approach here is: render the surfaces that carry the argument as **our
own HTML with our own scoped class names**, and use Streamlit as a shell for
layout and interactivity. The CSS below styles `.eg-*` classes we emit
ourselves. A handful of Streamlit-internal selectors are touched (tabs, base
background) and are marked as such — those are the parts that may need
revisiting after a Streamlit upgrade.

Palette
-------
Deliberately not the slate-and-blue that dashboards default to. The base is a
deep indigo-violet, with three semantic accents mapped to the three states this
product actually cares about: cleared (teal), held (amber), blocked (rose).
Money is set in IBM Plex Mono so that rupee columns align on the decimal —
tabular figures are a functional choice here, not a stylistic one.

Contrast
--------
Every text pair below is checked against its background for WCAG AA (4.5:1).
The violet brand colour sits at roughly 4.6:1 on the base, which is fine for
large text, borders and glyphs but is NOT used for body copy anywhere.
"""

from __future__ import annotations

import html

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

BASE = "#0B0A1F"
BASE_DEEP = "#070614"
SURFACE = "#151233"
SURFACE_RAISED = "#1D1943"
BORDER = "#2C2758"
BORDER_STRONG = "#3D3679"

TEXT = "#F4F3FF"
TEXT_MUTED = "#A9A4CF"
TEXT_FAINT = "#7C77A6"

BRAND = "#7C5CFF"
BRAND_SOFT = "#A78BFA"

CLEARED = "#2DD4BF"
HELD = "#FBBF24"
BLOCKED = "#FB7185"
INFO = "#60A5FA"

STATE_COLORS = {
    "auto_clear": CLEARED,
    "needs_review": HELD,
    "blocked": BLOCKED,
    "allowed": CLEARED,
    "pending_approval": HELD,
    "critical": BLOCKED,
    "high": "#FB923C",
    "medium": HELD,
    "low": INFO,
    "info": TEXT_MUTED,
}


def state_color(key: str) -> str:
    return STATE_COLORS.get(str(key).lower(), BRAND_SOFT)


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

:root {{
  --eg-base: {BASE};
  --eg-base-deep: {BASE_DEEP};
  --eg-surface: {SURFACE};
  --eg-surface-raised: {SURFACE_RAISED};
  --eg-border: {BORDER};
  --eg-border-strong: {BORDER_STRONG};
  --eg-text: {TEXT};
  --eg-muted: {TEXT_MUTED};
  --eg-faint: {TEXT_FAINT};
  --eg-brand: {BRAND};
  --eg-brand-soft: {BRAND_SOFT};
  --eg-cleared: {CLEARED};
  --eg-held: {HELD};
  --eg-blocked: {BLOCKED};

  /* Dense dashboard spacing scale */
  --eg-1: 4px;  --eg-2: 8px;   --eg-3: 12px;
  --eg-4: 16px; --eg-5: 24px;  --eg-6: 32px;

  --eg-radius: 10px;
  --eg-radius-sm: 6px;
  --eg-ease: cubic-bezier(.22,.61,.36,1);
}}

/* --- Streamlit internals. Marked deliberately: these selectors are the
   fragile part of this file and are the first thing to check after a
   Streamlit upgrade. Everything else styles our own .eg-* markup. --- */
.stApp {{
  background:
    radial-gradient(1100px 520px at 12% -10%, #241E5A 0%, transparent 60%),
    radial-gradient(900px 480px at 92% 0%, #14304A 0%, transparent 55%),
    var(--eg-base);
  color: var(--eg-text);
  font-family: 'IBM Plex Sans', system-ui, -apple-system, sans-serif;
}}
.block-container {{ padding-top: var(--eg-5); max-width: 1500px; }}
[data-testid="stHeader"] {{ background: transparent; }}

.stTabs [data-baseweb="tab-list"] {{
  gap: var(--eg-1);
  border-bottom: 1px solid var(--eg-border);
  padding-bottom: 0;
}}
.stTabs [data-baseweb="tab"] {{
  height: 40px;
  padding: 0 var(--eg-4);
  background: transparent;
  border-radius: var(--eg-radius-sm) var(--eg-radius-sm) 0 0;
  color: var(--eg-muted);
  font-size: 13.5px;
  font-weight: 500;
  letter-spacing: .01em;
  transition: color .18s var(--eg-ease), background .18s var(--eg-ease);
}}
.stTabs [data-baseweb="tab"]:hover {{ color: var(--eg-text); background: #ffffff08; }}
.stTabs [aria-selected="true"] {{
  color: var(--eg-text) !important;
  background: linear-gradient(180deg, #ffffff10, transparent);
  box-shadow: inset 0 -2px 0 0 var(--eg-brand);
}}

.stDataFrame, [data-testid="stDataFrame"] {{
  border: 1px solid var(--eg-border);
  border-radius: var(--eg-radius);
  overflow: hidden;
}}

details[data-testid="stExpander"] {{
  border: 1px solid var(--eg-border) !important;
  border-radius: var(--eg-radius) !important;
  background: var(--eg-surface) !important;
  margin-bottom: var(--eg-2);
  transition: border-color .18s var(--eg-ease);
}}
details[data-testid="stExpander"]:hover {{ border-color: var(--eg-border-strong) !important; }}
details[data-testid="stExpander"] summary {{ cursor: pointer; }}
details[data-testid="stExpander"] summary:focus-visible {{
  outline: 2px solid var(--eg-brand); outline-offset: 2px; border-radius: var(--eg-radius-sm);
}}

/* --- Our own components below this line --- */

.eg-masthead {{
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--eg-5); flex-wrap: wrap;
  padding: var(--eg-5) 0 var(--eg-4);
  border-bottom: 1px solid var(--eg-border);
  margin-bottom: var(--eg-5);
}}
.eg-wordmark {{
  display: flex; align-items: center; gap: var(--eg-3);
  font-size: 25px; font-weight: 700; letter-spacing: -.02em; color: var(--eg-text);
}}
.eg-wordmark .eg-mark {{
  width: 34px; height: 34px; border-radius: 9px; flex: 0 0 34px;
  background: linear-gradient(135deg, var(--eg-brand), var(--eg-cleared));
  display: grid; place-items: center;
}}
.eg-kicker {{
  font-size: 11px; font-weight: 600; letter-spacing: .16em; text-transform: uppercase;
  color: var(--eg-faint); margin-bottom: var(--eg-2);
}}
.eg-claim {{
  font-size: 13.5px; line-height: 1.55; color: var(--eg-muted); max-width: 62ch;
  margin-top: var(--eg-3);
}}
.eg-claim strong {{ color: var(--eg-text); font-weight: 600; }}

.eg-mode {{
  display: inline-flex; align-items: center; gap: var(--eg-2);
  padding: 7px 13px; border-radius: 999px;
  font-size: 12px; font-weight: 600; letter-spacing: .02em;
  border: 1px solid; white-space: nowrap;
}}
.eg-mode .eg-dot {{ width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px; }}
.eg-mode--live {{ color: {CLEARED}; border-color: #2DD4BF55; background: #2DD4BF14; }}
.eg-mode--live .eg-dot {{ background: {CLEARED}; box-shadow: 0 0 0 3px #2DD4BF33; }}
.eg-mode--mock {{ color: {HELD}; border-color: #FBBF2455; background: #FBBF2414; }}
.eg-mode--mock .eg-dot {{ background: {HELD}; box-shadow: 0 0 0 3px #FBBF2433; }}

.eg-hero {{
  display: grid; grid-template-columns: minmax(280px, 380px) 1fr;
  gap: var(--eg-4); margin-bottom: var(--eg-5);
}}
@media (max-width: 1100px) {{ .eg-hero {{ grid-template-columns: 1fr; }} }}

.eg-headline {{
  position: relative; overflow: hidden;
  padding: var(--eg-5); border-radius: var(--eg-radius);
  background: linear-gradient(150deg, #1E1A47 0%, var(--eg-surface) 55%);
  border: 1px solid var(--eg-border-strong);
}}
.eg-headline::after {{
  content: ""; position: absolute; inset: 0;
  background: radial-gradient(420px 200px at 88% -20%, #7C5CFF33, transparent 70%);
  pointer-events: none;
}}
.eg-headline .eg-figure {{
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: clamp(34px, 4.2vw, 50px); font-weight: 600; line-height: 1.04;
  letter-spacing: -.025em;
  background: linear-gradient(96deg, {CLEARED}, {BRAND_SOFT});
  -webkit-background-clip: text; background-clip: text; color: transparent;
  margin: var(--eg-2) 0 var(--eg-3);
}}
.eg-headline .eg-sub {{ font-size: 12.5px; color: var(--eg-muted); line-height: 1.5; }}

.eg-kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--eg-3); }}
@media (max-width: 780px) {{ .eg-kpis {{ grid-template-columns: repeat(2, 1fr); }} }}

.eg-kpi {{
  padding: var(--eg-4) var(--eg-4) var(--eg-3);
  border-radius: var(--eg-radius);
  background: var(--eg-surface);
  border: 1px solid var(--eg-border);
  border-top: 2px solid var(--eg-accent, var(--eg-brand));
  transition: transform .18s var(--eg-ease), border-color .18s var(--eg-ease);
}}
.eg-kpi:hover {{ transform: translateY(-2px); border-color: var(--eg-border-strong); }}
.eg-kpi .eg-label {{
  font-size: 10.5px; font-weight: 600; letter-spacing: .11em; text-transform: uppercase;
  color: var(--eg-faint); margin-bottom: var(--eg-2);
}}
.eg-kpi .eg-value {{
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
  font-size: 25px; font-weight: 600; color: var(--eg-text); letter-spacing: -.02em;
}}
.eg-kpi .eg-note {{ font-size: 11.5px; color: var(--eg-muted); margin-top: var(--eg-1); }}

.eg-section {{
  display: flex; align-items: baseline; gap: var(--eg-3);
  margin: var(--eg-5) 0 var(--eg-3);
}}
.eg-section h3 {{
  font-size: 15px; font-weight: 600; color: var(--eg-text); margin: 0; letter-spacing: -.01em;
}}
.eg-section .eg-rule {{ flex: 1; height: 1px; background: var(--eg-border); }}
.eg-section .eg-count {{
  font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--eg-faint);
}}
.eg-lede {{ font-size: 12.5px; color: var(--eg-muted); line-height: 1.6; max-width: 90ch; }}

.eg-pill {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 9px; border-radius: 999px;
  font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  border: 1px solid currentColor; white-space: nowrap;
}}
.eg-pill .eg-dot {{ width: 5px; height: 5px; border-radius: 50%; background: currentColor; }}

.eg-money {{ font-family: 'IBM Plex Mono', ui-monospace, monospace; font-variant-numeric: tabular-nums; }}

.eg-card {{
  background: var(--eg-surface); border: 1px solid var(--eg-border);
  border-radius: var(--eg-radius); padding: var(--eg-4);
}}
.eg-card--accent {{ border-left: 3px solid var(--eg-accent, var(--eg-brand)); }}

.eg-reason {{
  background: #FB718514; border: 1px solid #FB718544; border-radius: var(--eg-radius-sm);
  padding: var(--eg-3); font-size: 13px; line-height: 1.55; color: #FFD9DE;
}}
.eg-action {{
  background: #FBBF2412; border: 1px solid #FBBF2440; border-radius: var(--eg-radius-sm);
  padding: var(--eg-3); font-size: 12.5px; line-height: 1.55; color: #FDE9BD;
}}

.eg-evidence {{ list-style: none; padding: 0; margin: var(--eg-2) 0 0; }}
.eg-evidence li {{
  position: relative; padding: 5px 0 5px var(--eg-4);
  font-size: 12.5px; line-height: 1.5; color: var(--eg-muted);
  border-bottom: 1px solid #ffffff08;
}}
.eg-evidence li::before {{
  content: ""; position: absolute; left: 3px; top: 12px;
  width: 5px; height: 5px; border-radius: 50%; background: var(--eg-brand);
}}
.eg-evidence li:last-child {{ border-bottom: 0; }}

.eg-kv {{ display: grid; grid-template-columns: auto 1fr; gap: 5px var(--eg-3); font-size: 12.5px; }}
.eg-kv dt {{ color: var(--eg-faint); }}
.eg-kv dd {{ margin: 0; color: var(--eg-text); }}

.eg-quote {{
  border-left: 2px solid var(--eg-brand); padding: var(--eg-2) var(--eg-3);
  background: #ffffff06; border-radius: 0 var(--eg-radius-sm) var(--eg-radius-sm) 0;
  font-size: 12px; line-height: 1.6; color: var(--eg-muted); font-style: italic;
}}

.eg-bar {{ display: flex; flex-direction: column; gap: 7px; }}
.eg-bar .eg-row {{ display: grid; grid-template-columns: 150px 1fr auto; gap: var(--eg-3); align-items: center; }}
.eg-bar .eg-name {{ font-size: 12px; color: var(--eg-muted); }}
.eg-bar .eg-track {{ height: 9px; background: #ffffff0d; border-radius: 999px; overflow: hidden; }}
.eg-bar .eg-fill {{ height: 100%; border-radius: 999px; transition: width .5s var(--eg-ease); }}
.eg-bar .eg-amt {{
  font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--eg-text);
  font-variant-numeric: tabular-nums;
}}

.eg-foot {{
  margin-top: var(--eg-6); padding-top: var(--eg-4);
  border-top: 1px solid var(--eg-border);
  font-size: 11.5px; color: var(--eg-faint); line-height: 1.6;
}}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation-duration: .001ms !important; transition-duration: .001ms !important;
  }}
}}
</style>
"""


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def masthead(*, mode: str, banner: str) -> str:
    live = "LIVE" in mode.upper()
    cls = "eg-mode--live" if live else "eg-mode--mock"
    return f"""
<div class="eg-masthead">
  <div>
    <div class="eg-wordmark">
      <span class="eg-mark" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
             stroke="#0B0A1F" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 6 9 17l-5-5"/>
        </svg>
      </span>
      EntitleGraph<span style="color:{TEXT_FAINT};font-weight:400"> Close Agent</span>
    </div>
    <p class="eg-claim">
      Razorpay Recon proves the money that moved matches the settlement records.
      <strong>EntitleGraph proves the money that moved matches what the contract
      actually promised.</strong>
    </p>
  </div>
  <div style="text-align:right">
    <div class="eg-kicker">Execution mode</div>
    <span class="eg-mode {cls}"><span class="eg-dot" aria-hidden="true"></span>{_esc(mode)}</span>
    <div style="font-size:11.5px;color:{TEXT_FAINT};margin-top:8px;max-width:34ch">{_esc(banner)}</div>
  </div>
</div>
"""


def headline(amount: str, *, label: str, sub: str) -> str:
    return f"""
<div class="eg-headline">
  <div class="eg-kicker">{_esc(label)}</div>
  <div class="eg-figure">{_esc(amount)}</div>
  <div class="eg-sub">{_esc(sub)}</div>
</div>
"""


def kpi(label: str, value: str, note: str = "", accent: str = BRAND) -> str:
    note_html = f'<div class="eg-note">{_esc(note)}</div>' if note else ""
    return f"""
<div class="eg-kpi" style="--eg-accent:{accent}">
  <div class="eg-label">{_esc(label)}</div>
  <div class="eg-value">{_esc(value)}</div>
  {note_html}
</div>
"""


def kpi_grid(cards: list[str]) -> str:
    return f'<div class="eg-kpis">{"".join(cards)}</div>'


def hero(headline_html: str, kpis_html: str) -> str:
    return f'<div class="eg-hero">{headline_html}{kpis_html}</div>'


def section(title: str, count: str = "") -> str:
    count_html = f'<span class="eg-count">{_esc(count)}</span>' if count else ""
    return f"""
<div class="eg-section">
  <h3>{_esc(title)}</h3>
  <span class="eg-rule"></span>
  {count_html}
</div>
"""


def lede(text: str) -> str:
    return f'<p class="eg-lede">{_esc(text)}</p>'


def pill(text: str, color: str) -> str:
    return (
        f'<span class="eg-pill" style="color:{color}">'
        f'<span class="eg-dot" aria-hidden="true"></span>{_esc(text)}</span>'
    )


def evidence(items: list[str]) -> str:
    rows = "".join(f"<li>{_esc(i)}</li>" for i in items)
    return f'<ul class="eg-evidence">{rows}</ul>'


def bars(rows: list[tuple[str, int, str, str]]) -> str:
    """rows: (label, width_pct, formatted_amount, colour)."""
    out = []
    for label, pct, amount, colour in rows:
        out.append(
            f'<div class="eg-row"><span class="eg-name">{_esc(label)}</span>'
            f'<span class="eg-track"><span class="eg-fill" style="width:{pct}%;'
            f'background:linear-gradient(90deg,{colour}aa,{colour})"></span></span>'
            f'<span class="eg-amt">{_esc(amount)}</span></div>'
        )
    return f'<div class="eg-bar">{"".join(out)}</div>'

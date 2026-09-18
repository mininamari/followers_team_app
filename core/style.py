from __future__ import annotations

import streamlit as st


MARK_COLORS = ["#3157FF", "#F04A36", "#FFD84A", "#47B881", "#A36BFF", "#FF8A35", "#1B9AAA", "#E06C9F"]


MARK_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700;800&family=Unbounded:wght@500;600;700;800&display=swap');

:root {
    --mark-paper: #F2EEE5;
    --mark-paper-light: #FAF8F2;
    --mark-ink: #171715;
    --mark-muted: #68665F;
    --mark-blue: #3157FF;
    --mark-red: #F04A36;
    --mark-yellow: #FFD84A;
    --mark-green: #47B881;
    --mark-line: rgba(23, 23, 21, .20);
}

html, body, [class*="css"], .stApp {
    font-family: "Onest", system-ui, sans-serif;
}
.stApp {
    background:
        linear-gradient(rgba(23,23,21,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(23,23,21,.025) 1px, transparent 1px),
        var(--mark-paper);
    background-size: 32px 32px;
    color: var(--mark-ink);
}
.block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1280px; }
h1, h2, h3 {
    font-family: "Unbounded", "Arial Black", sans-serif !important;
    color: var(--mark-ink);
    letter-spacing: -.045em;
}
p, label, input, button, textarea, select { font-family: "Onest", system-ui, sans-serif !important; }

[data-testid="stSidebar"] {
    background: var(--mark-ink);
    border-right: 0;
}
[data-testid="stSidebar"] * { color: var(--mark-paper-light) !important; }
[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,.15); }
[data-testid="stSidebar"] [role="radiogroup"] label {
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 9px 11px;
    margin-bottom: 4px;
    transition: background .15s ease, border-color .15s ease, transform .15s ease;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {
    background: rgba(255,255,255,.08);
    border-color: rgba(255,255,255,.14);
    transform: translateX(2px);
}
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
    border-color: rgba(255,255,255,.34) !important;
    background: transparent !important;
    color: #fff !important;
    box-shadow: none !important;
}
.mark-lockup {
    position: relative;
    margin: 4px 0 22px;
    padding: 17px 15px 15px;
    background: var(--mark-yellow);
    color: var(--mark-ink) !important;
    border: 2px solid var(--mark-paper-light);
    box-shadow: 5px 5px 0 var(--mark-blue);
    transform: rotate(-1deg);
}
.mark-lockup * { color: var(--mark-ink) !important; }
.mark-lockup__name {
    font-family: "Unbounded", "Arial Black", sans-serif;
    font-size: 1.55rem;
    font-weight: 800;
    line-height: 1;
    letter-spacing: -.08em;
}
.mark-lockup__tag {
    margin-top: 8px;
    font-size: .64rem;
    font-weight: 800;
    letter-spacing: .13em;
}
.mark-lockup:after {
    content: "///";
    position: absolute;
    right: 11px;
    top: 7px;
    color: var(--mark-red) !important;
    font: 800 18px/1 "Unbounded", sans-serif;
    transform: rotate(9deg);
}

.nk-hero {
    position: relative;
    overflow: hidden;
    margin-bottom: 24px;
    padding: 25px 30px 28px;
    background: var(--mark-paper-light);
    color: var(--mark-ink);
    border: 2px solid var(--mark-ink);
    border-radius: 12px;
    box-shadow: 8px 8px 0 var(--mark-ink);
}
.nk-hero:before {
    content: "";
    position: absolute;
    width: 120px;
    height: 8px;
    right: 30px;
    top: 31px;
    background: var(--mark-blue);
    transform: rotate(-4deg);
}
.nk-hero:after {
    content: "×  ×  ×";
    position: absolute;
    right: 36px;
    bottom: 25px;
    color: var(--mark-red);
    font: 800 22px/1 "Unbounded", sans-serif;
    letter-spacing: .10em;
    transform: rotate(4deg);
}
.mark-hero-kicker {
    margin-bottom: 20px;
    font-size: .68rem;
    font-weight: 800;
    letter-spacing: .16em;
    text-transform: uppercase;
}
.mark-hero-kicker span {
    display: inline-block;
    margin-left: 8px;
    padding: 3px 7px;
    background: var(--mark-yellow);
    border: 1px solid var(--mark-ink);
    transform: rotate(-1deg);
}
.nk-hero h1 {
    max-width: 850px;
    margin: 0;
    color: var(--mark-ink);
    font-size: clamp(2rem, 4vw, 3.25rem);
    line-height: .98;
    text-transform: uppercase;
}
.nk-hero p {
    max-width: 760px;
    margin: 15px 0 0;
    color: var(--mark-muted);
    font-size: 1rem;
    line-height: 1.55;
}
.nk-chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 20px; padding-right: 130px; }
.nk-chip {
    padding: 6px 10px;
    background: transparent;
    color: var(--mark-ink);
    border: 1px solid var(--mark-ink);
    border-radius: 4px;
    font-size: .71rem;
    font-weight: 800;
    letter-spacing: .04em;
    text-transform: uppercase;
}
.nk-chip:nth-child(2n) { background: var(--mark-yellow); transform: rotate(-1deg); }

.nk-card, .nk-small-card {
    background: var(--mark-paper-light);
    border: 1px solid var(--mark-ink);
    border-radius: 9px;
    box-shadow: 4px 4px 0 rgba(23,23,21,.13);
}
.nk-card { padding: 20px; margin-bottom: 16px; }
.nk-small-card { padding: 16px 18px; }
.nk-label {
    color: var(--mark-ink);
    font-size: .72rem;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .11em;
}
.nk-value { margin-top: 5px; color: var(--mark-ink); font: 700 1.8rem/1.15 "Unbounded", sans-serif; }
.nk-help { margin-top: 7px; color: var(--mark-muted); font-size: .9rem; }

[data-testid="stMetric"] {
    position: relative;
    overflow: hidden;
    min-height: 112px;
    padding: 16px 17px;
    background: var(--mark-paper-light);
    border: 1px solid var(--mark-ink);
    border-radius: 9px;
    box-shadow: 4px 4px 0 rgba(23,23,21,.14);
}
[data-testid="stMetric"]:before {
    content: "";
    position: absolute;
    width: 42px;
    height: 5px;
    right: 11px;
    top: 12px;
    background: var(--mark-blue);
    transform: rotate(-7deg);
}
[data-testid="stMetricLabel"] { color: var(--mark-muted); font-weight: 700; }
[data-testid="stMetricValue"] { color: var(--mark-ink); font-family: "Unbounded", sans-serif; font-weight: 700; letter-spacing: -.05em; }

div.stButton > button, div.stDownloadButton > button, [data-testid="stFormSubmitButton"] button {
    min-height: 42px;
    border: 1px solid var(--mark-ink) !important;
    border-radius: 6px !important;
    background: var(--mark-blue) !important;
    color: white !important;
    font-weight: 800 !important;
    box-shadow: 4px 4px 0 var(--mark-ink) !important;
    transition: transform .12s ease, box-shadow .12s ease !important;
}
div.stButton > button:hover, div.stDownloadButton > button:hover, [data-testid="stFormSubmitButton"] button:hover {
    transform: translate(2px, 2px);
    box-shadow: 2px 2px 0 var(--mark-ink) !important;
}
div.stButton > button:disabled, div.stDownloadButton > button:disabled, [data-testid="stFormSubmitButton"] button:disabled {
    cursor: not-allowed !important;
    opacity: .5 !important;
    transform: none !important;
    box-shadow: none !important;
}
[data-baseweb="input"] > div, [data-baseweb="select"] > div, [data-baseweb="textarea"] > div {
    background: var(--mark-paper-light) !important;
    border-color: var(--mark-ink) !important;
    border-radius: 6px !important;
}
[data-testid="stForm"] {
    background: rgba(250,248,242,.64);
    border: 1px solid var(--mark-line);
    border-radius: 10px;
}
[data-baseweb="tab-list"] { gap: 6px; }
[data-baseweb="tab"] {
    border-radius: 5px 5px 0 0;
    font-weight: 700;
}
[data-baseweb="tab-highlight"] { background-color: var(--mark-blue); }
.stDataFrame, [data-testid="stDataFrame"], [data-testid="stPlotlyChart"] {
    overflow: hidden;
    background: var(--mark-paper-light);
    border: 1px solid var(--mark-line);
    border-radius: 9px;
}
.nk-status-ok { color: #167342; font-weight: 800; }
.nk-status-warn { color: #A84231; font-weight: 800; }

@media (max-width: 700px) {
    .block-container { padding-top: .75rem; }
    .nk-hero { padding: 22px 20px 25px; box-shadow: 5px 5px 0 var(--mark-ink); }
    .nk-hero:before { width: 68px; right: 18px; top: 25px; }
    .nk-hero:after { display: none; }
    .nk-chip-row { padding-right: 0; }
}
</style>
"""


def apply_novakid_style() -> None:
    """Apply the MARK/01 identity. Kept under the old name for import compatibility."""
    st.markdown(MARK_CSS, unsafe_allow_html=True)


def brand_lockup() -> None:
    st.markdown(
        """
        <div class="mark-lockup">
            <div class="mark-lockup__name">MARK/01</div>
            <div class="mark-lockup__tag">SOCIAL GROWTH SYSTEM</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def hero(title: str, subtitle: str, chips: list[str] | None = None) -> None:
    chip_html = "" if not chips else "<div class='nk-chip-row'>" + "".join([f"<span class='nk-chip'>{c}</span>" for c in chips]) + "</div>"
    st.markdown(
        f"""
        <div class="nk-hero">
            <div class="mark-hero-kicker">MARK/01 <span>SOCIAL GROWTH SYSTEM</span></div>
            <h1>{title}</h1>
            <p>{subtitle}</p>
            {chip_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_card(title: str, text: str = "") -> None:
    st.markdown(
        f"""
        <div class="nk-card">
            <div class="nk-label">{title}</div>
            {f'<div class="nk-help">{text}</div>' if text else ''}
        </div>
        """,
        unsafe_allow_html=True,
    )

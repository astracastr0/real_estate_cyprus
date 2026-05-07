"""Real Estate Dashboard — Bazaraki + Dom.cy"""

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Paths ────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
BAZARAKI_DB = BASE / "bazaraki" / "bazaraki.db"
DOMCY_DB    = BASE / "dom_cy"   / "dom_cy.db"

st.set_page_config(
    page_title="🏠 Real Estate CY",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styles ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background: #0f1117; }
    [data-testid="stSidebar"] { background: #161b27; }

    /* ── KPI card ── */
    .kpi-wrap {
        background: linear-gradient(135deg, #1a2235 0%, #1e2840 100%);
        border: 1px solid #2a3550;
        border-left: 3px solid #3b82f6;
        border-radius: 10px;
        padding: 14px 16px 12px;
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
    }
    .kpi-wrap .kpi-icon {
        font-size: 1rem;
        line-height: 1;
        margin-bottom: 4px;
    }
    .kpi-wrap .kpi-val {
        font-size: clamp(1.1rem, 1.6vw, 1.55rem);
        font-weight: 700;
        color: #e2e8f0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        line-height: 1.15;
    }
    .kpi-wrap .kpi-lbl {
        font-size: 0.68rem;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: .07em;
        white-space: nowrap;
    }
    .kpi-wrap .kpi-delta {
        font-size: 0.72rem;
        color: #34d399;
        margin-top: 1px;
        white-space: nowrap;
    }
    /* accent variants */
    .kpi-green  { border-left-color: #10b981 !important; }
    .kpi-purple { border-left-color: #8b5cf6 !important; }
    .kpi-amber  { border-left-color: #f59e0b !important; }
    .kpi-rose   { border-left-color: #f43f5e !important; }
    .kpi-cyan   { border-left-color: #06b6d4 !important; }

    a { color: #60a5fa !important; text-decoration: none !important; }
    a:hover { text-decoration: underline !important; }
    [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }

    /* table styling */
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { background: #1e2636; color: #94a3b8; padding: 8px 10px;
         text-align: left; border-bottom: 1px solid #2d3748;
         font-weight: 600; font-size: 0.72rem; text-transform: uppercase;
         letter-spacing: .05em; white-space: nowrap; }
    td { padding: 7px 10px; border-bottom: 1px solid #1a2030; color: #e2e8f0;
         vertical-align: middle; }
    tr:hover td { background: #1c2538; }
</style>
""", unsafe_allow_html=True)


# ── Data loading ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_db(path: Path, source_name: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(path)
    df = pd.read_sql("SELECT * FROM listings", conn)
    conn.close()
    df["source"] = source_name
    df["scraped_at"] = pd.to_datetime(df["scraped_at"], utc=True, errors="coerce")
    df["scraped_date"] = df["scraped_at"].dt.date
    return df


def load_all(sources: list[str]) -> pd.DataFrame:
    frames = []
    if "Bazaraki" in sources:
        frames.append(load_db(BAZARAKI_DB, "Bazaraki"))
    if "Dom.cy" in sources:
        frames.append(load_db(DOMCY_DB, "Dom.cy"))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🏠 Real Estate CY")
    st.markdown("---")

    sources = st.multiselect(
        "Источник",
        ["Bazaraki", "Dom.cy"],
        default=["Bazaraki", "Dom.cy"],
    )

    all_data = load_all(sources)

    if all_data.empty:
        st.warning("Нет данных. Запустите парсер.")
        st.stop()

    st.markdown("### Фильтры")

    districts = sorted(all_data["district"].dropna().unique().tolist())
    sel_districts = st.multiselect("Город / район", districts, default=districts)

    prop_types = sorted(all_data["property_type"].dropna().unique().tolist())
    sel_types = st.multiselect("Тип", prop_types, default=prop_types)

    price_min = int(all_data["price_eur"].dropna().min())
    price_max = int(all_data["price_eur"].dropna().max())
    sel_price = st.slider("Цена (€)", price_min, price_max, (price_min, min(price_max, 2_000_000)), step=10_000)

    beds = sorted(all_data["bedrooms"].dropna().astype(int).unique().tolist())
    sel_beds = st.multiselect("Спален", beds, default=beds)

    has_pool = st.checkbox("Только с бассейном", value=False)

    # ── Date filter ────────────────────────────────────────────────────────
    st.markdown("**Дата запуска парсера**")
    avail_dates = sorted(all_data["scraped_date"].dropna().unique().tolist(), reverse=True)
    date_labels = {d: d.strftime("%d.%m.%Y") for d in avail_dates}
    sel_dates = st.multiselect(
        "Сессии",
        options=avail_dates,
        default=avail_dates,
        format_func=lambda d: date_labels[d],
        label_visibility="collapsed",
    )

    st.markdown("---")
    st.caption(f"Обновлено: {all_data['scraped_at'].max().strftime('%d %b %Y %H:%M') if not all_data.empty else '—'}")


# ── Apply filters ─────────────────────────────────────────────────────────────
df = all_data.copy()
if sel_dates:
    df = df[df["scraped_date"].isin(sel_dates)]
if sel_districts:
    df = df[df["district"].isin(sel_districts)]
if sel_types:
    df = df[df["property_type"].isin(sel_types)]
df = df[df["price_eur"].between(*sel_price)]
if sel_beds:
    df = df[df["bedrooms"].isin(sel_beds)]
if has_pool:
    df = df[df["has_pool"] == 1]


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("## 🏠 Real Estate Dashboard — Кипр")

# ── KPI cards ─────────────────────────────────────────────────────────────────
def fmt_price(v: int) -> str:
    """Format integer price compactly: 1 234 567 → €1.23M, 239 664 → €240K."""
    if v >= 1_000_000:
        return f"€{v/1_000_000:.2f}M".rstrip("0").rstrip(".")
    if v >= 1_000:
        return f"€{round(v/1000)}K"
    return f"€{v}"

total      = len(df)
avg_price  = int(df["price_eur"].mean()) if total else 0
med_price  = int(df["price_eur"].median()) if total else 0
avg_sqm    = int(df["price_per_sqm"].dropna().mean()) if total else 0
with_pool  = int(df["has_pool"].sum()) if total else 0

last_date  = df["scraped_date"].max() if total else None
new_count  = int((df["scraped_date"] == last_date).sum()) if last_date else 0

def kpi(col, icon, value, label, delta="", accent=""):
    col.markdown(
        f'<div class="kpi-wrap {accent}">'
        f'<div class="kpi-icon">{icon}</div>'
        f'<div class="kpi-val">{value}</div>'
        f'<div class="kpi-lbl">{label}</div>'
        + (f'<div class="kpi-delta">{delta}</div>' if delta else "")
        + "</div>",
        unsafe_allow_html=True,
    )

c1, c2, c3, c4, c5, c6 = st.columns(6)
kpi(c1, "🏘️", f"{total:,}",          "Объявлений",   accent="")
kpi(c2, "💰", fmt_price(avg_price),  "Средняя цена", accent="kpi-green")
kpi(c3, "📊", fmt_price(med_price),  "Медиана",      accent="kpi-cyan")
kpi(c4, "📐", f"€{avg_sqm:,}/м²",   "Цена за м²",   accent="kpi-purple")
kpi(c5, "🏊", f"{with_pool}",        "С бассейном",  accent="kpi-amber")
kpi(c6, "🆕", f"{new_count}",        "Новых (посл.)",
    delta=f"от {last_date}" if last_date else "", accent="kpi-rose")

st.markdown("<br>", unsafe_allow_html=True)


# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_overview, tab_history, tab_listings = st.tabs(["📊 Обзор", "📅 История", "📋 Объявления"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════
with tab_overview:

    col_left, col_right = st.columns(2, gap="medium")

    with col_left:
        # Avg price by district & type
        grp = (
            df.groupby(["district", "property_type"])["price_eur"]
            .mean()
            .reset_index()
            .rename(columns={"price_eur": "avg_price"})
        )
        fig = px.bar(
            grp, x="district", y="avg_price", color="property_type",
            barmode="group",
            title="Средняя цена по городу и типу",
            labels={"avg_price": "Цена (€)", "district": "Город", "property_type": "Тип"},
            color_discrete_sequence=["#60a5fa", "#34d399"],
            template="plotly_dark",
        )
        fig.update_layout(
            paper_bgcolor="#1e2636", plot_bgcolor="#1e2636",
            font_color="#e2e8f0", title_font_size=14,
            legend_title_text="", margin=dict(l=0, r=0, t=40, b=0),
        )
        fig.update_yaxes(tickprefix="€", separatethousands=True)
        st.plotly_chart(fig, width="stretch")

    with col_right:
        # Price histogram
        fig2 = px.histogram(
            df[df["price_eur"] <= 3_000_000],
            x="price_eur", nbins=50, color="property_type",
            title="Распределение цен",
            labels={"price_eur": "Цена (€)", "property_type": "Тип"},
            color_discrete_sequence=["#60a5fa", "#34d399"],
            template="plotly_dark",
            opacity=0.85,
        )
        fig2.update_layout(
            paper_bgcolor="#1e2636", plot_bgcolor="#1e2636",
            font_color="#e2e8f0", title_font_size=14,
            legend_title_text="", barmode="overlay",
            margin=dict(l=0, r=0, t=40, b=0),
        )
        fig2.update_xaxes(tickprefix="€", separatethousands=True)
        st.plotly_chart(fig2, width="stretch")

    col_l2, col_r2 = st.columns(2, gap="medium")

    with col_l2:
        # Listings count by district (pie)
        cnt = df.groupby("district").size().reset_index(name="count")
        fig3 = px.pie(
            cnt, names="district", values="count",
            title="Доля объявлений по городу",
            color_discrete_sequence=px.colors.qualitative.Set2,
            template="plotly_dark", hole=0.4,
        )
        fig3.update_layout(
            paper_bgcolor="#1e2636", font_color="#e2e8f0", title_font_size=14,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig3, width="stretch")

    with col_r2:
        # Bedrooms distribution
        if df["bedrooms"].notna().any():
            beds_cnt = (
                df["bedrooms"].dropna().astype(int)
                .value_counts().sort_index().reset_index()
            )
            beds_cnt.columns = ["bedrooms", "count"]
            fig4 = px.bar(
                beds_cnt, x="bedrooms", y="count",
                title="Количество спален",
                labels={"bedrooms": "Спален", "count": "Объявлений"},
                color="count",
                color_continuous_scale="Blues",
                template="plotly_dark",
            )
            fig4.update_layout(
                paper_bgcolor="#1e2636", plot_bgcolor="#1e2636",
                font_color="#e2e8f0", title_font_size=14,
                coloraxis_showscale=False,
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig4, width="stretch")

    # Price vs area scatter — click to open listing
    scatter_df = df[
        df["area_sqm"].between(20, 1000) & df["price_eur"].between(50_000, 5_000_000)
    ].copy()
    if not scatter_df.empty:
        fig5 = px.scatter(
            scatter_df,
            x="area_sqm", y="price_eur",
            color="district", symbol="property_type",
            size_max=8, opacity=0.7,
            title="Цена vs Площадь — кликните на точку, чтобы открыть объявление",
            labels={"area_sqm": "Площадь (м²)", "price_eur": "Цена (€)", "district": "Город"},
            color_discrete_sequence=px.colors.qualitative.Set2,
            template="plotly_dark",
            custom_data=["url", "title", "price_eur", "area_sqm", "district",
                         "bedrooms", "property_type", "source"],
        )
        fig5.update_traces(
            hovertemplate=(
                "<b>%{customdata[1]}</b><br>"
                "Цена: €%{y:,.0f}<br>"
                "Площадь: %{x:.0f} м²<br>"
                "%{customdata[4]} · %{customdata[6]}<br>"
                "<i>%{customdata[7]}</i>"
                "<extra></extra>"
            ),
            marker=dict(size=7, line=dict(width=0.5, color="#0f1117")),
        )
        fig5.update_layout(
            paper_bgcolor="#1e2636", plot_bgcolor="#1e2636",
            font_color="#e2e8f0", title_font_size=14,
            margin=dict(l=0, r=0, t=40, b=0),
            clickmode="event+select",
        )
        fig5.update_yaxes(tickprefix="€", separatethousands=True)

        sel = st.plotly_chart(
            fig5, width="stretch",
            on_select="rerun", key="scatter_price_area",
        )

        # ── Show card when a point is clicked ──────────────────────────────
        pts = (sel.selection.points if sel and sel.selection else [])
        if pts:
            pt = pts[0]
            cd = pt.get("customdata", [])
            p_url    = cd[0] if len(cd) > 0 else ""
            p_title  = cd[1] if len(cd) > 1 else "—"
            p_price  = int(cd[2]) if len(cd) > 2 and cd[2] else 0
            p_area   = cd[3] if len(cd) > 3 else "—"
            p_dist   = cd[4] if len(cd) > 4 else "—"
            p_beds   = int(cd[5]) if len(cd) > 5 and cd[5] else "—"
            p_type   = cd[6] if len(cd) > 6 else "—"
            p_src    = cd[7] if len(cd) > 7 else "—"
            link_html = (
                f'<a href="{p_url}" target="_blank">Открыть объявление ↗</a>'
                if p_url else "нет ссылки"
            )
            st.markdown(
                f"""
                <div style="
                    background: linear-gradient(135deg,#1a2235,#1e2840);
                    border: 1px solid #2a3550; border-left: 3px solid #10b981;
                    border-radius: 10px; padding: 14px 18px; margin-top: 8px;
                    display: flex; gap: 24px; align-items: center; flex-wrap: wrap;
                ">
                  <div style="flex:1; min-width:200px;">
                    <div style="font-size:.7rem;color:#64748b;text-transform:uppercase;
                                letter-spacing:.07em;margin-bottom:3px;">Выбрано</div>
                    <div style="font-size:1rem;font-weight:600;color:#e2e8f0;
                                margin-bottom:6px;">{p_title}</div>
                    <div style="display:flex;gap:16px;flex-wrap:wrap;">
                      <span style="color:#94a3b8;font-size:.82rem;">
                        💰 <b style="color:#e2e8f0;">€{p_price:,}</b>
                      </span>
                      <span style="color:#94a3b8;font-size:.82rem;">
                        📐 <b style="color:#e2e8f0;">{p_area} м²</b>
                      </span>
                      <span style="color:#94a3b8;font-size:.82rem;">
                        🛏 <b style="color:#e2e8f0;">{p_beds}</b>
                      </span>
                      <span style="color:#94a3b8;font-size:.82rem;">
                        📍 <b style="color:#e2e8f0;">{p_dist}</b>
                      </span>
                      <span style="color:#94a3b8;font-size:.82rem;">
                        🏷 <b style="color:#e2e8f0;">{p_type} · {p_src}</b>
                      </span>
                    </div>
                  </div>
                  <div style="font-size:1rem;font-weight:600;">
                    {link_html}
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # Source comparison
    if len(sources) > 1:
        st.markdown("#### Сравнение источников")
        comp = df.groupby("source").agg(
            Объявлений=("id", "count"),
            Средняя_цена=("price_eur", "mean"),
            Медиана_цены=("price_eur", "median"),
            Ср_цена_за_м2=("price_per_sqm", "mean"),
        ).round(0).reset_index()
        comp["Средняя_цена"] = comp["Средняя_цена"].apply(lambda x: f"€{int(x):,}")
        comp["Медиана_цены"] = comp["Медиана_цены"].apply(lambda x: f"€{int(x):,}")
        comp["Ср_цена_за_м2"] = comp["Ср_цена_за_м2"].apply(lambda x: f"€{int(x):,}" if pd.notna(x) else "—")
        st.dataframe(comp, width="stretch", hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — HISTORY
# ══════════════════════════════════════════════════════════════════════════════
with tab_history:
    st.markdown("### Динамика парсинга")

    hist = (
        all_data.groupby(["scraped_date", "source"])
        .size()
        .reset_index(name="count")
    )
    hist["scraped_date"] = pd.to_datetime(hist["scraped_date"])

    fig_h = px.bar(
        hist, x="scraped_date", y="count", color="source",
        barmode="group",
        title="Объявлений собрано по датам",
        labels={"scraped_date": "Дата", "count": "Объявлений", "source": "Источник"},
        color_discrete_map={"Bazaraki": "#60a5fa", "Dom.cy": "#34d399"},
        template="plotly_dark",
    )
    fig_h.update_layout(
        paper_bgcolor="#1e2636", plot_bgcolor="#1e2636",
        font_color="#e2e8f0", title_font_size=14,
        margin=dict(l=0, r=0, t=40, b=0),
    )
    st.plotly_chart(fig_h, width="stretch")

    # Price trend over sessions
    price_hist = (
        all_data.groupby(["scraped_date", "source"])["price_eur"]
        .median()
        .reset_index()
        .rename(columns={"price_eur": "median_price"})
    )
    price_hist["scraped_date"] = pd.to_datetime(price_hist["scraped_date"])

    if len(price_hist) > 1:
        fig_ph = px.line(
            price_hist, x="scraped_date", y="median_price", color="source",
            markers=True,
            title="Медианная цена по сессиям парсинга",
            labels={"scraped_date": "Дата", "median_price": "Медиана (€)", "source": "Источник"},
            color_discrete_map={"Bazaraki": "#60a5fa", "Dom.cy": "#34d399"},
            template="plotly_dark",
        )
        fig_ph.update_layout(
            paper_bgcolor="#1e2636", plot_bgcolor="#1e2636",
            font_color="#e2e8f0", title_font_size=14,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        fig_ph.update_yaxes(tickprefix="€", separatethousands=True)
        st.plotly_chart(fig_ph, width="stretch")

    # Sessions table
    st.markdown("#### Сессии парсинга")
    sessions = (
        all_data.groupby(["scraped_date", "source"])
        .agg(
            Объявлений=("id", "count"),
            Новых_уник=("id", "nunique"),
            Мин_цена=("price_eur", "min"),
            Макс_цена=("price_eur", "max"),
            Ср_цена=("price_eur", "mean"),
        )
        .round(0)
        .reset_index()
        .sort_values("scraped_date", ascending=False)
    )
    sessions["scraped_date"] = sessions["scraped_date"].astype(str)
    sessions.rename(columns={"scraped_date": "Дата", "source": "Источник"}, inplace=True)
    for col in ["Мин_цена", "Макс_цена", "Ср_цена"]:
        sessions[col] = sessions[col].apply(lambda x: f"€{int(x):,}")
    st.dataframe(sessions, width="stretch", hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — LISTINGS TABLE
# ══════════════════════════════════════════════════════════════════════════════
with tab_listings:
    st.markdown(f"### Найдено: **{len(df):,}** объявлений")

    sort_col = st.selectbox(
        "Сортировка",
        ["price_eur ↑", "price_eur ↓", "price_per_sqm ↑", "price_per_sqm ↓",
         "area_sqm ↓", "scraped_at ↓"],
        index=1,
    )

    col_map = {
        "price_eur ↑": ("price_eur", True),
        "price_eur ↓": ("price_eur", False),
        "price_per_sqm ↑": ("price_per_sqm", True),
        "price_per_sqm ↓": ("price_per_sqm", False),
        "area_sqm ↓": ("area_sqm", False),
        "scraped_at ↓": ("scraped_at", False),
    }
    sort_key, asc = col_map[sort_col]
    display = df.sort_values(sort_key, ascending=asc).head(500).copy()

    # Format link column
    display["Ссылка"] = display["url"].apply(
        lambda u: f'<a href="{u}" target="_blank">открыть ↗</a>' if pd.notna(u) and u else ""
    )

    # Build clean display table
    cols_show = {
        "source": "Источник",
        "title": "Название",
        "district": "Город",
        "area": "Район",
        "property_type": "Тип",
        "price_eur": "Цена €",
        "price_per_sqm": "€/м²",
        "area_sqm": "Площадь м²",
        "bedrooms": "Спален",
        "bathrooms": "Ванных",
        "has_pool": "Бассейн",
        "furnishing": "Мебель",
        "condition": "Состояние",
        "posted_date": "Размещено",
        "Ссылка": "Ссылка",
    }

    table = display[[c for c in cols_show if c in display.columns]].rename(columns=cols_show)
    if "Цена €" in table.columns:
        table["Цена €"] = table["Цена €"].apply(lambda x: f"€{int(x):,}" if pd.notna(x) else "—")
    if "€/м²" in table.columns:
        table["€/м²"] = table["€/м²"].apply(lambda x: f"€{int(x):,}" if pd.notna(x) and x > 0 else "—")
    if "Площадь м²" in table.columns:
        table["Площадь м²"] = table["Площадь м²"].apply(lambda x: f"{int(x)}" if pd.notna(x) else "—")
    if "Бассейн" in table.columns:
        table["Бассейн"] = table["Бассейн"].apply(lambda x: "✅" if x == 1 else "")
    if "Размещено" in table.columns:
        table["Размещено"] = table["Размещено"].fillna("—")

    st.write(
        table.to_html(escape=False, index=False),
        unsafe_allow_html=True,
    )

    if len(df) > 500:
        st.caption(f"Показаны первые 500 из {len(df):,}. Уточните фильтры для более узкой выборки.")

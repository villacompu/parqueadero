from __future__ import annotations

import os
import streamlit as st
import pandas as pd


def escape_html(s: str) -> str:
    s = s or ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def st_image_safe(path_or_bytes, caption=None):
    try:
        st.image(path_or_bytes, caption=caption, width="stretch")
    except TypeError:
        try:
            st.image(path_or_bytes, caption=caption, width="stretch")
        except TypeError:
            try:
                st.image(path_or_bytes, caption=caption, use_column_width=True)
            except TypeError:
                st.image(path_or_bytes, caption=caption)


# -----------------------------
# ✅ Fecha/hora corta en tablas
# -----------------------------
def _looks_like_dt_value(v: object) -> bool:
    if v is None:
        return False
    s = str(v)
    return ("-" in s and ":" in s) or ("T" in s and ":" in s)


def _fmt_dt_series(s: pd.Series) -> pd.Series:
    """Format only datetime-like series to YYYY-MM-DDTHH:MM.

    Avoids converting numeric counters like 'Ingresos'/'Salidas' to 1970-01-01.
    """
    if pd.api.types.is_numeric_dtype(s):
        return s

    sample = s.dropna().astype(str).head(20).tolist()
    if sample and not any(_looks_like_dt_value(x) for x in sample):
        return s

    dt = pd.to_datetime(s, errors="coerce", utc=False)
    # if most values could not be parsed, keep original
    if dt.notna().sum() < max(1, int(0.4 * len(dt))):
        return s
    out = dt.dt.strftime("%Y-%m-%dT%H:%M")
    return out.where(dt.notna(), s.astype(str))


def st_df(df: pd.DataFrame):
    """Dataframe helper:
    - Width stretch (Streamlit >=1.54)
    - Hide index
    - Format datetime-like columns to YYYY-MM-DDTHH:MM (safe)
    """
    if df is None:
        df2 = df
    else:
        df2 = df.copy()

        if len(df2) > 0:
            # 1) If already datetime dtype
            for col in df2.columns:
                if pd.api.types.is_datetime64_any_dtype(df2[col]):
                    df2[col] = df2[col].dt.strftime("%Y-%m-%dT%H:%M")

            # 2) If text but looks like datetime (by name + content)
            date_like_names = {
                "fecha",
                "hora",
                "datetime",
                "date",
                "time",
                "created",
                "updated",
                "entry",
                "exit",
                "ingreso",
                "salida",
                "entrada",
            }
            for col in df2.columns:
                col_low = str(col).lower()
                if any(k in col_low for k in date_like_names):
                    df2[col] = _fmt_dt_series(df2[col])

    try:
        st.dataframe(df2, width="stretch", hide_index=True)
    except TypeError:
        try:
            st.dataframe(df2, use_container_width=True, hide_index=True)
        except TypeError:
            st.dataframe(df2)


def inject_css():

    st.markdown(
        """
        <style>
        .ppn-row{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0 2px 0;}
        .ppn-card{
            border:1px solid #e8e8e8;
            border-radius:16px;
            padding:14px 14px;
            background:#ffffff;
            box-shadow:0 1px 0 rgba(0,0,0,.02);
            min-width:240px;
            flex:1;
        }
        .ppn-title{font-size:14px;color:#4a4a4a;font-weight:700;margin-bottom:6px;}
        .ppn-big{font-size:34px;font-weight:900;line-height:1;}
        .ppn-sub{margin-top:6px;color:#666;font-size:13px;}
        .ppn-zone{
            border:1px solid #ededed;border-radius:14px;padding:12px;background:#fff;
            margin-bottom:10px;
        }
        .ppn-zone-h{display:flex;justify-content:space-between;align-items:center;gap:10px;}
        .ppn-zone-name{font-weight:800;font-size:15px;}
        .ppn-zone-meta{color:#666;font-size:13px;margin-top:6px;}
        .ppn-tag{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid #e5e5e5;font-size:12px;color:#555;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def card_metric(title: str, big: str, sub: str = ""):
    st.markdown(
        f"""
        <div class="ppn-card">
            <div class="ppn-title">{escape_html(title)}</div>
            <div class="ppn-big">{escape_html(str(big))}</div>
            <div class="ppn-sub">{escape_html(sub)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def zone_card(name: str, avail: int, used: int, badge: str, tag: str = "", highlight: bool = False):
    border = "2px solid #1f77b4" if highlight else "1px solid #ededed"
    st.markdown(
        f"""
        <div class="ppn-zone" style="border:{border};">
            <div class="ppn-zone-h">
                <div class="ppn-zone-name">{escape_html(badge)} {escape_html(name)}</div>
                <div class="ppn-tag">{escape_html(tag)}</div>
            </div>
            <div class="ppn-zone-meta">
                Disponibles: <b>{avail}</b> · Ocupados: <b>{used}</b>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
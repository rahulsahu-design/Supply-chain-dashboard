import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime, timedelta
import os
import base64
import json
import threading

SHEET_ID = "1ixmX8rsx9jiGzvSgG8dwXAdQGINGYMprJexgiV4MOwk"
TAB_NAME = "Shipment Tracker AWB wise"
CREDS_FILE = r"D:\Claude Code\Shipment tracker dashobard\clean-algebra-496218-q4-dcb8941fbf42.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

STATUS_COL = "Current Status - Ship partner portal"
DELIVERED_STATUSES = {"Delivered"}
OVERDUE_BUCKETS = {"21-30", "30+", "31-40", "40+"}
EXCLUDED_UNDELIVERED_STATUSES = {"Delivered", "RTO", "Abandon"}
XINDUS = "Xindus Air + Sea"
PROM_TAT_BY_TRANSPORTER = {XINDUS: 40, "DHL": 7}
PROM_TAT_DEFAULT = 12

_cache = {"df": None, "fetched_at": None}
CACHE_TTL_SECONDS = 1800  # 30 minutes

# Cached worksheet — avoids re-authenticating + re-opening the sheet every refresh
_ws_cache = {"sheet": None, "last_connect": None}
WS_CACHE_TTL_SECONDS = 3600  # reconnect once per hour

# Prevents concurrent fetches
_fetch_lock = threading.Lock()


def _connect():
    b64 = os.environ.get("GOOGLE_CREDS_B64")
    if b64:
        try:
            b64 = "".join(b64.split())
            padded = b64 + "=" * (-len(b64) % 4)
            decoded = base64.b64decode(padded).decode("utf-8")
            info = json.loads(decoded)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            raise RuntimeError(f"GOOGLE_CREDS_B64 decode failed (len={len(b64)}): {e}") from e
    else:
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_worksheet():
    """Return a cached gspread worksheet, reconnecting only if stale or on error."""
    now = datetime.now()
    ws = _ws_cache["sheet"]
    last = _ws_cache["last_connect"]
    if ws is None or last is None or (now - last).total_seconds() > WS_CACHE_TTL_SECONDS:
        client = _connect()
        ws = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)
        _ws_cache["sheet"] = ws
        _ws_cache["last_connect"] = now
    return ws


def _do_fetch():
    """Fetch from Google Sheets and update the in-memory cache. Not thread-safe alone."""
    try:
        ws = _get_worksheet()
        raw = ws.get_all_values()
    except Exception:
        # Stale connection — force reconnect and retry once
        _ws_cache["sheet"] = None
        ws = _get_worksheet()
        raw = ws.get_all_values()

    if not raw:
        return

    HEADER_ROW_IDX = 3
    headers = [h.strip() for h in raw[HEADER_ROW_IDX]]
    data_rows = raw[HEADER_ROW_IDX + 1:]

    n = len(headers)
    data_rows = [row + [''] * (n - len(row)) if len(row) < n else row[:n] for row in data_rows]

    df = pd.DataFrame(data_rows, columns=headers)
    df = df.loc[:, df.columns != '']

    date_cols = ["Pick up Date", "Actual Delivery Date", "Expected Delivery Date"]
    for col in date_cols:
        if col in df.columns:
            raw = df[col].astype(str).str.strip()
            df[f"_raw_{col}"] = raw
            # Primary: explicit DD-MM-YYYY format (sheet's actual format)
            parsed = pd.to_datetime(raw, format="%d-%m-%Y", errors="coerce")
            # Fallback 1: general parser dayfirst=True (handles other separators)
            failed = parsed.isna() & raw.ne("") & raw.ne("nan")
            if failed.any():
                parsed2 = pd.to_datetime(raw[failed], dayfirst=True, errors="coerce")
                parsed[failed] = parsed2
            # Fallback 2: dayfirst=False for M/D/Y style
            failed = parsed.isna() & raw.ne("") & raw.ne("nan")
            if failed.any():
                parsed3 = pd.to_datetime(raw[failed], dayfirst=False, errors="coerce")
                parsed[failed] = parsed3
            df[col] = parsed

    for col in ["Actual TAT", "Prom TAT", "Delay Days", "Ageing", "Qty Sent", "Vol. Wt", "No. Of box"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "Prom TAT" in df.columns and "Transporter" in df.columns:
        prom_null = df["Prom TAT"].isna()
        if prom_null.any():
            for t, tat in PROM_TAT_BY_TRANSPORTER.items():
                df.loc[prom_null & (df["Transporter"].str.strip() == t), "Prom TAT"] = tat
            df.loc[prom_null & df["Prom TAT"].isna(), "Prom TAT"] = PROM_TAT_DEFAULT
            if "Actual TAT" in df.columns and "Delay Days" in df.columns:
                recalc = prom_null & df["Actual TAT"].notna()
                df.loc[recalc, "Delay Days"] = df.loc[recalc, "Actual TAT"] - df.loc[recalc, "Prom TAT"]

    if "Year" in df.columns:
        year_num = pd.to_numeric(
            df["Year"].astype(str).str.strip().str.replace(',', '', regex=False),
            errors='coerce'
        )
        df = df[year_num.isna() | (year_num >= 2025)].copy()

    df["is_delivered"] = df[STATUS_COL].str.strip().isin(DELIVERED_STATUSES)

    if "Transporter" in df.columns and "Ageing" in df.columns:
        xindus = df["Transporter"].str.strip() == XINDUS
        df["is_overdue"] = (
            (xindus & (df["Ageing"].fillna(0) > 40)) |
            (~xindus & df["Ageing Bucket"].str.strip().isin(OVERDUE_BUCKETS))
        )
    else:
        df["is_overdue"] = df["Ageing Bucket"].str.strip().isin(OVERDUE_BUCKETS)

    _cache["df"] = df
    _cache["fetched_at"] = datetime.now()


def fetch_data(force=False) -> pd.DataFrame:
    now = datetime.now()
    is_stale = (
        _cache["df"] is None
        or _cache["fetched_at"] is None
        or (now - _cache["fetched_at"]).total_seconds() >= CACHE_TTL_SECONDS
    )

    # Stale but data exists: serve immediately and refresh in background
    if not force and _cache["df"] is not None and is_stale:
        if _fetch_lock.acquire(blocking=False):
            def _bg():
                try:
                    _do_fetch()
                finally:
                    _fetch_lock.release()
            threading.Thread(target=_bg, daemon=True).start()
        return _cache["df"]

    # Cold start or explicit force: must block until data is ready
    if _cache["df"] is None or force:
        with _fetch_lock:
            # Re-check under lock — another thread may have just fetched
            now2 = datetime.now()
            needs = (
                force
                or _cache["df"] is None
                or _cache["fetched_at"] is None
                or (now2 - _cache["fetched_at"]).total_seconds() >= CACHE_TTL_SECONDS
            )
            if needs:
                if force:
                    _ws_cache["sheet"] = None  # force fresh connection too
                _do_fetch()

    return _cache["df"]


# Pre-warm the cache as soon as the module loads so the first request is instant
threading.Thread(target=fetch_data, daemon=True).start()


# ── Global filter helpers ─────────────────────────────────────────────────────

def get_filter_options(df: pd.DataFrame) -> dict:
    years = sorted(df["Year"].dropna().astype(str).str.strip().unique().tolist())
    months_order = ["January","February","March","April","May","June",
                    "July","August","September","October","November","December"]
    months_raw = df["Month"].dropna().astype(str).str.strip().unique().tolist()
    months = [m for m in months_order if m in months_raw]
    weeks_raw = df["Week"].dropna().astype(str).str.strip().unique().tolist()
    weeks = sorted([w for w in weeks_raw if w.isdigit()], key=lambda x: int(x))
    del_weeks_raw = df["Delivery Date Week Num"].dropna().astype(str).str.strip().unique().tolist()
    del_weeks = sorted(
        [w for w in del_weeks_raw if w.isdigit()],
        key=lambda x: int(x)
    )
    # Expected delivery weeks/years (from Expected Delivery Date column)
    exp_del_weeks = []
    exp_del_years = []
    if "Expected Delivery Date" in df.columns:
        exp_dates = df["Expected Delivery Date"].dropna()
        if not exp_dates.empty:
            weeks_iso = exp_dates.dt.isocalendar().week.dropna().astype(int).unique().tolist()
            exp_del_weeks = sorted([str(w) for w in weeks_iso])
            exp_del_years = sorted(exp_dates.dt.year.dropna().astype(int).unique().tolist())
    max_del_date = None
    del_years = []
    if "Actual Delivery Date" in df.columns:
        mx = df["Actual Delivery Date"].dropna().max()
        if pd.notna(mx):
            max_del_date = mx.strftime("%Y-%m-%d")
        del_years = sorted(df["Actual Delivery Date"].dropna().dt.year.unique().astype(int).tolist())

    undel_statuses = []
    if STATUS_COL in df.columns:
        mask = _active_undelivered_mask(df)
        undel_statuses = sorted(df[mask][STATUS_COL].str.strip().dropna().replace("", pd.NA).dropna().unique().tolist())

    return {
        "years": years, "months": months, "weeks": weeks, "del_weeks": del_weeks,
        "del_years": [str(y) for y in del_years],
        "exp_del_weeks": exp_del_weeks,
        "exp_del_years": [str(y) for y in exp_del_years],
        "max_delivery_date": max_del_date,
        "undel_statuses": undel_statuses,
    }


def apply_filters(df: pd.DataFrame, year=None, month=None, week=None,
                  date_from=None, date_to=None) -> pd.DataFrame:
    if year and year != "All":
        try:
            target = int(float(str(year).strip()))
            cleaned = pd.to_numeric(
                df["Year"].astype(str).str.strip().str.replace(',', '', regex=False),
                errors='coerce'
            )
            df = df[cleaned == target]
        except (ValueError, TypeError):
            df = df[df["Year"].astype(str).str.strip() == str(year)]
    if month and month != "All":
        df = df[df["Month"].astype(str).str.strip() == month]
    if week and week != "All":
        try:
            target = int(float(str(week).strip()))
            cleaned = pd.to_numeric(df["Week"].astype(str).str.strip(), errors='coerce')
            df = df[cleaned == target]
        except (ValueError, TypeError):
            df = df[df["Week"].astype(str).str.strip() == str(week)]
    if date_from:
        df = df[df["Pick up Date"] >= pd.to_datetime(date_from)]
    if date_to:
        df = df[df["Pick up Date"] <= pd.to_datetime(date_to)]
    return df


# ── View helpers ──────────────────────────────────────────────────────────────

def daily_operations_summary(df: pd.DataFrame) -> dict:
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())

    total = len(df)
    delivered = int(df["is_delivered"].sum())
    undelivered = total - delivered
    overdue = int(df["is_overdue"].sum())

    # Today's pickups
    today_pickups = int((df["Pick up Date"].dt.date == today).sum()) if "Pick up Date" in df.columns else 0

    # This week's deliveries
    week_delivered = int(
        df[df["is_delivered"] & (df["Actual Delivery Date"].dt.date >= week_start)].shape[0]
    ) if "Actual Delivery Date" in df.columns else 0

    # Status breakdown
    status_counts = (
        df[STATUS_COL].str.strip().value_counts().to_dict()
        if STATUS_COL in df.columns else {}
    )

    # Channel breakdown
    channel_counts = df.groupby("Channel")["is_delivered"].agg(
        delivered="sum", total="count"
    ).reset_index()
    channel_data = channel_counts.to_dict(orient="records")

    return {
        "total_shipments": total,
        "delivered": delivered,
        "undelivered": undelivered,
        "overdue": overdue,
        "today_pickups": today_pickups,
        "week_delivered": week_delivered,
        "status_breakdown": status_counts,
        "channel_breakdown": channel_data,
    }


def _filter_channel(d, channel):
    if not channel:
        return d
    if isinstance(channel, list):
        return d[d["Channel"].isin(channel)] if "All" not in channel else d
    return d[d["Channel"] == channel] if channel != "All" else d


def _filter_del_week(d, del_week):
    if not del_week:
        return d
    if isinstance(del_week, list):
        if "All" in del_week:
            return d
        return d[d["Delivery Date Week Num"].astype(str).str.strip().isin([str(w) for w in del_week])]
    return d[d["Delivery Date Week Num"].astype(str).str.strip() == str(del_week)] if del_week != "All" else d


def _filter_exp_del_week(d, exp_del_week):
    if not exp_del_week or "Expected Delivery Date" not in d.columns:
        return d
    week_nums = d["Expected Delivery Date"].dt.isocalendar().week.astype(str)
    if isinstance(exp_del_week, list):
        if "All" in exp_del_week:
            return d
        return d[week_nums.isin([str(w) for w in exp_del_week])]
    return d[week_nums == str(exp_del_week)] if exp_del_week != "All" else d


def _active_undelivered_mask(df):
    status = df[STATUS_COL].str.strip()
    return ~status.isin(EXCLUDED_UNDELIVERED_STATUSES) & (status != "")


def _filter_del_year(d, year):
    if year and year != "All" and "Actual Delivery Date" in d.columns:
        try:
            d = d[d["Actual Delivery Date"].dt.year == int(float(str(year).strip()))]
        except (ValueError, TypeError):
            pass
    return d


def delivered_shipments(df: pd.DataFrame, channel=None, del_week=None, date_from=None, date_to=None, year=None) -> list:
    d = df[df["is_delivered"]].copy()
    d = _filter_del_year(d, year)
    d = _filter_channel(d, channel)
    d = _filter_del_week(d, del_week)
    if date_from:
        d = d[d["Actual Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to:
        d = d[d["Actual Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]

    cols = [
        "Shipment AWB", "Channel", "Transporter", "Pick up Date",
        "Actual Delivery Date", "Actual TAT", "Prom TAT", "Delay Days",
        "Product Name", "Qty Sent", STATUS_COL,
    ]
    cols = [c for c in cols if c in d.columns]
    out = d[cols].copy()
    for dc in ["Pick up Date", "Actual Delivery Date"]:
        if dc in out.columns:
            out[dc] = out[dc].apply(lambda x: x.strftime("%d/%m/%Y") if pd.notna(x) else "")
    return out.fillna("").to_dict(orient="records")


def delivered_by_date_channel(df: pd.DataFrame, channel=None, del_week=None, date_from=None, date_to=None, year=None) -> dict:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna()].copy()
    d = _filter_del_year(d, year)
    d = _filter_channel(d, channel)
    d = _filter_del_week(d, del_week)
    if date_from:
        d = d[d["Actual Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to:
        d = d[d["Actual Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]
    d["delivery_date"] = d["Actual Delivery Date"].dt.strftime("%d/%m/%Y")

    by_date = d.groupby("delivery_date").size().reset_index(name="count")
    by_channel = d.groupby("Channel").size().reset_index(name="count")

    return {
        "by_date": by_date.sort_values("delivery_date").to_dict(orient="records"),
        "by_channel": by_channel.to_dict(orient="records"),
    }


def delivered_pivot(df: pd.DataFrame, channels=None, del_weeks=None, date_from=None, date_to=None, days=14, year=None) -> dict:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna() & df["Qty Sent"].notna()].copy()
    d = _filter_del_year(d, year)
    d = _filter_channel(d, channels)
    d = _filter_del_week(d, del_weeks)
    if date_from:
        d = d[d["Actual Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to:
        d = d[d["Actual Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]
    if not date_from and not date_to:
        cutoff = (datetime.now() - timedelta(days=days)).date()
        d = d[d["Actual Delivery Date"].dt.date >= cutoff]

    if d.empty:
        return {"dates": [], "rows": [], "grand_totals": {}, "overall_total": 0}

    def fmt_date(dt):
        return f"{dt.day} {dt.strftime('%b %Y')}"

    d = d.copy()
    d["_date_obj"] = d["Actual Delivery Date"].dt.date
    d["_date_label"] = d["Actual Delivery Date"].apply(lambda x: fmt_date(x))

    unique_dates = sorted(d["_date_obj"].unique())
    date_labels = [fmt_date(dt) for dt in unique_dates]

    channels_in_data = d["Channel"].dropna().unique().tolist()
    channel_order = ["Amazon", "TikTok", "Shipbob"]
    ordered = [c for c in channel_order if c in channels_in_data]
    others = [c for c in channels_in_data if c not in channel_order]
    all_channels = ordered + others

    result_rows = []
    for ch in all_channels:
        ch_df = d[d["Channel"] == ch]
        if ch_df.empty:
            continue

        product_rows = []
        for prod in ch_df["Product Name"].dropna().unique():
            prod_df = ch_df[ch_df["Product Name"] == prod]
            values = {dl: int(qty) for dl, qty in prod_df.groupby("_date_label")["Qty Sent"].sum().items()}
            total = sum(values.values())
            if total > 0:
                product_rows.append({"product": str(prod), "values": values, "total": total})

        product_rows.sort(key=lambda x: x["total"], reverse=True)

        ch_totals = {}
        for dl in date_labels:
            val = int(ch_df[ch_df["_date_label"] == dl]["Qty Sent"].sum())
            if val > 0:
                ch_totals[dl] = val

        result_rows.append({
            "channel": ch,
            "products": product_rows,
            "subtotals": ch_totals,
            "channel_total": int(ch_df["Qty Sent"].sum()),
        })

    grand_totals = {}
    for dl in date_labels:
        val = int(d[d["_date_label"] == dl]["Qty Sent"].sum())
        if val > 0:
            grand_totals[dl] = val

    return {
        "dates": date_labels,
        "rows": result_rows,
        "grand_totals": grand_totals,
        "overall_total": int(d["Qty Sent"].sum()),
    }


def undelivered_shipments(df: pd.DataFrame, channel=None, exp_del_week=None,
                          date_from=None, date_to=None, year=None, statuses=None) -> list:
    d = df[_active_undelivered_mask(df)].copy()
    if year and year != "All":
        try:
            target = int(float(str(year).strip()))
            d = d[d["Expected Delivery Date"].dt.year == target]
        except (ValueError, TypeError):
            pass
    d = _filter_channel(d, channel)
    d = _filter_exp_del_week(d, exp_del_week)
    if date_from:
        d = d[d["Expected Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to:
        d = d[d["Expected Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]
    if statuses and STATUS_COL in d.columns:
        d = d[d[STATUS_COL].str.strip().isin(statuses)]
    cols = [
        "Shipment AWB", "Channel", "Transporter", "Pick up Date",
        "Expected Delivery Date", "Ageing", "Ageing Bucket",
        "Delay Days", STATUS_COL, "Product Name", "Qty Sent", "is_overdue",
    ]
    cols = [c for c in cols if c in d.columns]
    out = d[cols].copy()
    for dc in ["Pick up Date", "Expected Delivery Date"]:
        if dc in out.columns:
            out[dc] = out[dc].apply(lambda x: x.strftime("%d/%m/%Y") if pd.notna(x) else "")
    if "is_overdue" in out.columns:
        out["is_overdue"] = out["is_overdue"].astype(bool)
    return out.fillna("").to_dict(orient="records")


def undelivered_pivot(df: pd.DataFrame, channels=None, exp_del_weeks=None,
                      date_from=None, date_to=None, year=None, statuses=None) -> dict:
    mask = _active_undelivered_mask(df)
    d = df[mask & df["Expected Delivery Date"].notna() & df["Qty Sent"].notna()].copy()
    if year and year != "All":
        try:
            target = int(float(str(year).strip()))
            d = d[d["Expected Delivery Date"].dt.year == target]
        except (ValueError, TypeError):
            pass
    d = _filter_channel(d, channels)
    d = _filter_exp_del_week(d, exp_del_weeks)
    if date_from:
        d = d[d["Expected Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to:
        d = d[d["Expected Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]
    if statuses and STATUS_COL in d.columns:
        d = d[d[STATUS_COL].str.strip().isin(statuses)]

    if d.empty:
        return {"dates": [], "rows": [], "grand_totals": {}, "overall_total": 0}

    def fmt_date(dt):
        return f"{dt.day} {dt.strftime('%b %Y')}"

    d = d.copy()
    d["_date_obj"] = d["Expected Delivery Date"].dt.date
    d["_date_label"] = d["Expected Delivery Date"].apply(lambda x: fmt_date(x))

    unique_dates = sorted(d["_date_obj"].unique())
    date_labels = [fmt_date(dt) for dt in unique_dates]

    channels_in_data = d["Channel"].dropna().unique().tolist()
    channel_order = ["Amazon", "TikTok", "Shipbob"]
    ordered = [c for c in channel_order if c in channels_in_data]
    others = [c for c in channels_in_data if c not in channel_order]
    all_channels = ordered + others

    result_rows = []
    for ch in all_channels:
        ch_df = d[d["Channel"] == ch]
        if ch_df.empty:
            continue
        product_rows = []
        for prod in ch_df["Product Name"].dropna().unique():
            prod_df = ch_df[ch_df["Product Name"] == prod]
            values = {dl: int(qty) for dl, qty in prod_df.groupby("_date_label")["Qty Sent"].sum().items()}
            total = sum(values.values())
            if total > 0:
                product_rows.append({"product": str(prod), "values": values, "total": total})
        product_rows.sort(key=lambda x: x["total"], reverse=True)
        ch_totals = {dl: int(ch_df[ch_df["_date_label"] == dl]["Qty Sent"].sum())
                     for dl in date_labels if ch_df[ch_df["_date_label"] == dl]["Qty Sent"].sum() > 0}
        result_rows.append({
            "channel": ch,
            "products": product_rows,
            "subtotals": ch_totals,
            "channel_total": int(ch_df["Qty Sent"].sum()),
        })

    grand_totals = {dl: int(d[d["_date_label"] == dl]["Qty Sent"].sum())
                    for dl in date_labels if d[d["_date_label"] == dl]["Qty Sent"].sum() > 0}

    return {
        "dates": date_labels,
        "rows": result_rows,
        "grand_totals": grand_totals,
        "overall_total": int(d["Qty Sent"].sum()),
    }


def tat_pivot(df: pd.DataFrame, channels=None, del_weeks=None, date_from=None, date_to=None) -> dict:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna()].copy()
    d = _filter_channel(d, channels)
    d = _filter_del_week(d, del_weeks)
    if date_from:
        d = d[d["Actual Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to:
        d = d[d["Actual Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]

    if d.empty:
        return {"transporters": [], "rows": [], "totals": {}, "overall": {"count": 0, "avg_tat": None}}

    d = d[d["Transporter"].notna() & (d["Transporter"].str.strip() != "")]

    transporters = sorted(d["Transporter"].str.strip().unique().tolist())
    channels_in_data = d["Channel"].dropna().unique().tolist()
    channel_order = ["Amazon", "TikTok", "Shipbob"]
    ordered = [c for c in channel_order if c in channels_in_data]
    others = [c for c in channels_in_data if c not in channel_order]
    all_channels = ordered + others

    def safe_avg(series):
        vals = series.dropna()
        return round(float(vals.mean()), 1) if len(vals) > 0 else None

    result_rows = []
    for ch in all_channels:
        ch_df = d[d["Channel"] == ch]
        cells = {}
        for t in transporters:
            t_df = ch_df[ch_df["Transporter"].str.strip() == t]
            count = len(t_df)
            cells[t] = {
                "count": count if count > 0 else None,
                "avg_tat": safe_avg(t_df["Actual TAT"]) if count > 0 and "Actual TAT" in t_df.columns else None,
            }
        result_rows.append({
            "channel": ch,
            "cells": cells,
            "row_total": len(ch_df),
            "row_avg_tat": safe_avg(ch_df["Actual TAT"]) if "Actual TAT" in ch_df.columns else None,
        })

    totals = {}
    for t in transporters:
        t_df = d[d["Transporter"].str.strip() == t]
        totals[t] = {
            "count": len(t_df),
            "avg_tat": safe_avg(t_df["Actual TAT"]) if "Actual TAT" in t_df.columns else None,
        }

    return {
        "transporters": transporters,
        "rows": result_rows,
        "totals": totals,
        "overall": {
            "count": len(d),
            "avg_tat": safe_avg(d["Actual TAT"]) if "Actual TAT" in d.columns else None,
        },
    }


def raw_data_2026(df: pd.DataFrame) -> dict:
    RAW_COLS = [
        "Shipment AWB", "Channel", "Transporter", "Product Name",
        "Pick up Date", "Expected Delivery Date", "Actual Delivery Date",
        "Prom TAT", "Actual TAT", "Delay Days",
        "Ageing", "Ageing Bucket", STATUS_COL, "Qty Sent",
        "Year", "Month", "Week",
    ]
    try:
        cleaned = pd.to_numeric(df["Year"].astype(str).str.strip().str.replace(',', '', regex=False), errors='coerce')
        d = df[cleaned == 2026].copy()
    except Exception:
        d = df[df["Year"].astype(str).str.strip() == "2026"].copy()

    cols = [c for c in RAW_COLS if c in d.columns]
    out = d[cols].copy()
    for dc in ["Pick up Date", "Expected Delivery Date", "Actual Delivery Date"]:
        if dc in out.columns:
            raw_col = f"_raw_{dc}"
            if raw_col in d.columns:
                parsed = out[dc]
                raw_strs = d.loc[out.index, raw_col]
                out[dc] = [
                    v.strftime("%d/%m/%Y") if pd.notna(v)
                    else (r if r and r not in ('', 'nan', 'NaT', 'None') else "")
                    for v, r in zip(parsed, raw_strs)
                ]
            else:
                out[dc] = out[dc].apply(lambda x: x.strftime("%d/%m/%Y") if pd.notna(x) else "")
    out = out.fillna("").astype(str).replace("nan", "").replace("<NA>", "")
    return {"columns": cols, "rows": out.to_dict(orient="records"), "total": len(out)}


def tat_analysis(df: pd.DataFrame, days=7) -> list:
    cutoff = datetime.now() - timedelta(days=days)
    d = df[df["Pick up Date"] >= cutoff].copy()
    d = d[d["Actual TAT"].notna() & d["Prom TAT"].notna()]

    grouped = d.groupby("Transporter").agg(
        avg_actual_tat=("Actual TAT", "mean"),
        avg_prom_tat=("Prom TAT", "mean"),
        avg_delay=("Delay Days", "mean"),
        shipments=("Shipment AWB", "count"),
        on_time=("Delay Days", lambda x: (x <= 0).sum()),
    ).reset_index()

    grouped["on_time_pct"] = (grouped["on_time"] / grouped["shipments"] * 100).round(1)
    grouped = grouped.round(2)
    return grouped.to_dict(orient="records")


def shipment_ageing(df: pd.DataFrame, channels=None, exp_del_weeks=None,
                    date_from=None, date_to=None, year=None, statuses=None) -> dict:
    _all_buckets = ["0-5", "6-10", "11-20", "21-30", "30+", "31-40", "40+"]
    if "Ageing Bucket" in df.columns:
        _present = set(df["Ageing Bucket"].str.strip().dropna().unique())
        bucket_order = [b for b in _all_buckets if b in _present] or _all_buckets[:5]
    else:
        bucket_order = _all_buckets[:5]
    _EXCLUDE = {"Delivered", "RTO", "Abandon", "RTS"}
    status = df[STATUS_COL].str.strip()
    d = df[status.notna() & (status != "") & ~status.isin(_EXCLUDE)].copy()
    d = _filter_channel(d, channels)
    d = _filter_exp_del_week(d, exp_del_weeks)
    if year and year != "All":
        try:
            target = int(float(str(year).strip()))
            if "Expected Delivery Date" in d.columns:
                d = d[d["Expected Delivery Date"].dt.year == target]
        except (ValueError, TypeError):
            pass
    if date_from and "Expected Delivery Date" in d.columns:
        d = d[d["Expected Delivery Date"].dt.date >= pd.to_datetime(date_from).date()]
    if date_to and "Expected Delivery Date" in d.columns:
        d = d[d["Expected Delivery Date"].dt.date <= pd.to_datetime(date_to).date()]
    if statuses and STATUS_COL in d.columns:
        d = d[d[STATUS_COL].str.strip().isin(statuses)]

    def _units(sub):
        return int(sub["Qty Sent"].fillna(0).sum()) if "Qty Sent" in sub.columns else 0

    # Bucket totals (for charts / KPI cards)
    bucket_totals = []
    for b in bucket_order:
        bdf = d[d["Ageing Bucket"].str.strip() == b]
        bucket_totals.append({"bucket": b, "count": len(bdf), "units": _units(bdf)})

    # Transporter × bucket pivot
    transporters = sorted(
        d["Transporter"].dropna().str.strip().replace("", pd.NA).dropna().unique().tolist()
    )
    pivot_rows = []
    for t in transporters:
        tdf = d[d["Transporter"].str.strip() == t]
        cells = {}
        for b in bucket_order:
            bdf = tdf[tdf["Ageing Bucket"].str.strip() == b]
            cells[b] = {"count": len(bdf), "units": _units(bdf)}
        pivot_rows.append({
            "transporter": t,
            "cells": cells,
            "total_count": len(tdf),
            "total_units": _units(tdf),
        })

    return {
        "buckets": bucket_order,
        "rows": pivot_rows,
        "totals": bucket_totals,
        "grand_total_count": len(d),
        "grand_total_units": _units(d),
    }


def transporter_scorecard(df: pd.DataFrame) -> list:
    df = df[df["Transporter"].notna() & (df["Transporter"].str.strip() != "")].copy()
    grouped = df.groupby("Transporter").agg(
        total=("Shipment AWB", "count"),
        delivered=("is_delivered", "sum"),
        avg_actual_tat=("Actual TAT", "mean"),
        avg_prom_tat=("Prom TAT", "mean"),
        avg_delay=("Delay Days", "mean"),
    ).reset_index()

    del_df = df[df["is_delivered"] & df["Delay Days"].notna()].copy()
    on_time_grp = del_df.groupby("Transporter").agg(
        on_time=("Delay Days", lambda x: (x <= 0).sum()),
        tat_count=("Delay Days", "count"),
    ).reset_index()

    grouped = grouped.merge(on_time_grp, on="Transporter", how="left")
    grouped["on_time_pct"] = (
        grouped["on_time"] / grouped["tat_count"].replace(0, float("nan")) * 100
    ).round(1)
    grouped["delivery_rate"] = (grouped["delivered"] / grouped["total"] * 100).round(1)
    grouped = grouped.round(2).sort_values("delivery_rate", ascending=False)
    return grouped.fillna("").to_dict(orient="records")


def monthly_deliveries(df: pd.DataFrame) -> list:
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    d = df[df["is_delivered"]].copy()
    result = []
    for m in MONTHS:
        mdf = d[d["Month"].astype(str).str.strip() == m]
        if len(mdf):
            result.append({
                "month": m[:3],
                "shipments": len(mdf),
                "units": int(mdf["Qty Sent"].fillna(0).sum()) if "Qty Sent" in mdf.columns else 0,
            })
    return result


def tonnage_report(df: pd.DataFrame, transporters=None) -> dict:
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    d = df.copy()
    if transporters:
        d = d[d["Transporter"].str.strip().isin(transporters)]
    rows = []
    for m in MONTHS:
        mdf = d[d["Month"].astype(str).str.strip() == m]
        if len(mdf) == 0:
            continue
        delivered = int(mdf["is_delivered"].sum())
        del_df = mdf[mdf["is_delivered"] & mdf["Delay Days"].notna()]
        tat_count = len(del_df)
        on_time_pct = round(float((del_df["Delay Days"] <= 0).sum()) / tat_count * 100, 1) if tat_count > 0 else None
        rows.append({
            "month": m[:3],
            "shipments": len(mdf),
            "delivered": delivered,
            "on_time_pct": on_time_pct,
            "vol_wt": round(float(mdf["Vol. Wt"].fillna(0).sum()), 1) if "Vol. Wt" in mdf.columns else 0,
            "units": int(mdf["Qty Sent"].fillna(0).sum()) if "Qty Sent" in mdf.columns else 0,
            "boxes": int(mdf["No. Of box"].fillna(0).sum()) if "No. Of box" in mdf.columns else 0,
        })
    all_transporters = sorted(
        df["Transporter"].dropna().str.strip().replace("", pd.NA).dropna().unique().tolist()
    )
    return {"rows": rows, "transporters": all_transporters}


def channel_health(df: pd.DataFrame) -> list:
    channels = ["Amazon", "TikTok", "Shipbob"]
    result = []
    for ch in channels:
        c = df[df["Channel"] == ch]
        if c.empty:
            continue
        total = len(c)
        delivered = int(c["is_delivered"].sum())
        overdue = int(c["is_overdue"].sum())
        avg_tat = round(c["Actual TAT"].mean(), 1) if "Actual TAT" in c.columns else None
        avg_delay = round(c["Delay Days"].mean(), 1) if "Delay Days" in c.columns else None
        delivery_rate = round(delivered / total * 100, 1) if total else 0
        status_breakdown = c[STATUS_COL].str.strip().value_counts().to_dict()

        # Health color: green ≥80% delivered and <5% overdue, orange mid, red otherwise
        overdue_pct = overdue / total * 100 if total else 0
        if delivery_rate >= 80 and overdue_pct < 5:
            health = "green"
        elif delivery_rate >= 60 or overdue_pct < 15:
            health = "orange"
        else:
            health = "red"

        result.append({
            "channel": ch,
            "total": total,
            "delivered": delivered,
            "undelivered": total - delivered,
            "overdue": overdue,
            "delivery_rate": delivery_rate,
            "avg_actual_tat": avg_tat,
            "avg_delay": avg_delay,
            "health": health,
            "status_breakdown": status_breakdown,
        })
    return result


def weekly_trend(df: pd.DataFrame) -> list:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna()].copy()
    d["week"] = d["Actual Delivery Date"].dt.isocalendar().week.astype(str)
    d["year"] = d["Actual Delivery Date"].dt.year.astype(str)
    d["week_label"] = "W" + d["week"] + "-" + d["year"]

    grouped = d.groupby("week_label").size().reset_index(name="deliveries")
    return grouped.to_dict(orient="records")


def monthly_trend(df: pd.DataFrame) -> list:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna()].copy()
    d["month_label"] = d["Actual Delivery Date"].dt.strftime("%b %Y")
    d["sort_key"] = d["Actual Delivery Date"].dt.to_period("M")

    grouped = d.groupby(["month_label", "sort_key"]).size().reset_index(name="deliveries")
    grouped = grouped.sort_values("sort_key")
    return grouped[["month_label", "deliveries"]].to_dict(orient="records")

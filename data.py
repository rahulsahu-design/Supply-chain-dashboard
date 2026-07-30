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
EXCLUDED_UNDELIVERED_STATUSES = {"Delivered", "RTO", "RTS", "Abandon", "Cancelled", "Cancel", "Claims"}
SCORECARD_EXCLUDE_STATUSES = {"Abandon", "Cancelled", "Cancel", "RTO", "RTS", "Claims"}
TERMINAL_STATUS_GROUPS = [
    ("Abandon",   {"Abandon"}),
    ("Cancelled", {"Cancelled", "Cancel"}),
    ("RTO",       {"RTO"}),
    ("Claims",    {"Claims"}),
]
XINDUS = "Xindus Air + Sea"
PROM_TAT_BY_TRANSPORTER = {XINDUS: 40, "DHL": 7}
PROM_TAT_DEFAULT = 12

_CHARGEABLE_COL_CANDIDATES = [
    "Chargeable weight", "Chargeable Weight", "Chargeable Wt", "Chargeable Wt.",
    "chargeable weight", "chargeable_weight",
]
_MODE_COL_CANDIDATES = ["mode", "Mode", "MODE", "Transport Mode", "transport mode"]

def _chargeable_col(df):
    for name in _CHARGEABLE_COL_CANDIDATES:
        if name in df.columns:
            return name
    return None

def _mode_col(df):
    for name in _MODE_COL_CANDIDATES:
        if name in df.columns:
            return name
    return None


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

    for col in ["Actual TAT", "Prom TAT", "Delay Days", "Ageing", "Qty Sent", "No. Of box", "Shipment Value"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    cw = _chargeable_col(df)
    if cw:
        df[cw] = pd.to_numeric(df[cw], errors="coerce")

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


# Pre-warm the shipment cache as soon as the module loads so the first request is instant
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

    transporters = sorted(
        df["Transporter"].dropna().str.strip().replace("", pd.NA).dropna().unique().tolist()
    ) if "Transporter" in df.columns else []

    return {
        "years": years, "months": months, "weeks": weeks, "del_weeks": del_weeks,
        "del_years": [str(y) for y in del_years],
        "exp_del_weeks": exp_del_weeks,
        "exp_del_years": [str(y) for y in exp_del_years],
        "max_delivery_date": max_del_date,
        "undel_statuses": undel_statuses,
        "transporters": transporters,
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
    undelivered = int(_active_undelivered_mask(df).sum())
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
        "Shipment AWB", "Order ID", "Invoice No", "Channel", "Transporter",
        "Pick up Date", "Expected Delivery Date", "Ageing", "Ageing Bucket",
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


def _year_mask(df: pd.DataFrame, year_val: int):
    """Mask for rows belonging to year_val.
    Matches Year column == year_val OR (Year is blank AND Pick up Date year == year_val).
    """
    year_cleaned = pd.to_numeric(
        df["Year"].astype(str).str.strip().str.replace(',', '', regex=False),
        errors='coerce'
    )
    year_match = year_cleaned == year_val
    year_blank = year_cleaned.isna()
    if "Pick up Date" in df.columns:
        pickup_match = df["Pick up Date"].dt.year == year_val
    else:
        pickup_match = pd.Series(False, index=df.index)
    return year_match | (year_blank & pickup_match)


def raw_data_2026(df: pd.DataFrame) -> dict:
    _INTERNAL = {'is_delivered', 'is_overdue', '_month_eff'}
    d = df[_year_mask(df, 2026)].copy()
    # Use the sheet's natural column order; strip computed/internal columns
    cols = [c for c in d.columns if not c.startswith('_') and c not in _INTERNAL]
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
    status = df[STATUS_COL].str.strip()
    d = df[status.notna() & (status != "") & ~status.isin(EXCLUDED_UNDELIVERED_STATUSES)].copy()
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
    if STATUS_COL in df.columns:
        df = df[~df[STATUS_COL].str.strip().isin(SCORECARD_EXCLUDE_STATUSES)]
    grouped = df.groupby("Transporter").agg(
        total=("Shipment AWB", "count"),
        delivered=("is_delivered", "sum"),
        avg_prom_tat=("Prom TAT", "mean"),
    ).reset_index()

    # avg_delay and on_time_pct computed from delivered shipments only — undelivered rows
    # have Delay Days = 0 or negative (shipment not yet late) which falsely shows "On Time"
    # for transporters with no actual deliveries.
    del_df = df[df["is_delivered"] & df["Delay Days"].notna()].copy()
    on_time_grp = del_df.groupby("Transporter").agg(
        on_time=("Delay Days", lambda x: (x <= 0).sum()),
        tat_count=("Delay Days", "count"),
        avg_delay=("Delay Days", "mean"),
    ).reset_index()

    # Weighted average actual TAT of delivered shipments, weighted by units sent
    del_tat_df = df[df["is_delivered"] & df["Actual TAT"].notna()].copy()
    del_tat_df["_w"] = del_tat_df["Qty Sent"].fillna(1).clip(lower=1) if "Qty Sent" in del_tat_df.columns else 1
    wtd_tat_grp = del_tat_df.groupby("Transporter").apply(
        lambda g: round(float((g["Actual TAT"] * g["_w"]).sum() / g["_w"].sum()), 2)
    ).reset_index(name="avg_actual_tat")

    grouped = grouped.merge(on_time_grp, on="Transporter", how="left")
    grouped = grouped.merge(wtd_tat_grp, on="Transporter", how="left")
    grouped["on_time_pct"] = (
        grouped["on_time"] / grouped["tat_count"].replace(0, float("nan")) * 100
    ).round(1)
    grouped["delivery_rate"] = (grouped["delivered"] / grouped["total"] * 100).round(1)
    grouped = grouped.round(2).sort_values("delivery_rate", ascending=False)
    return grouped.fillna("").to_dict(orient="records")


def monthly_deliveries(df: pd.DataFrame, transporters=None) -> list:
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    if transporters and "Transporter" in df.columns:
        df = df[df["Transporter"].str.strip().isin(transporters)].copy()
    # Derive month from pickup date for rows where Month column is blank
    if "Pick up Date" in df.columns:
        derived = df["Pick up Date"].dt.month.map(
            lambda n: MONTHS[int(n)-1] if pd.notna(n) else None
        )
        month_col = df["Month"].astype(str).str.strip().replace("", pd.NA).replace("nan", pd.NA)
        df = df.copy()
        df["_month_eff"] = month_col.combine_first(derived)
    else:
        df = df.copy()
        df["_month_eff"] = df["Month"].astype(str).str.strip()
    _TRANSIT_EXCLUDE = DELIVERED_STATUSES | {"Abandon", "Claims", "RTO", "RTS", "Cancelled", "Cancel"}
    result = []
    for m in MONTHS:
        mdf = df[df["_month_eff"] == m]
        if not len(mdf):
            continue
        del_df = mdf[mdf["is_delivered"]]
        if STATUS_COL in mdf.columns:
            st = mdf[STATUS_COL].str.strip()
            abandon_df  = mdf[st.isin({"Abandon"})]
            claims_df   = mdf[st.isin({"Claims"})]
            rto_df      = mdf[st.isin({"RTO"})]
            transit_df  = mdf[~st.isin(_TRANSIT_EXCLUDE) & (st != "")]
        else:
            abandon_df = claims_df = rto_df = transit_df = mdf.iloc[0:0]

        def _qty(sub): return int(sub["Qty Sent"].fillna(0).sum()) if "Qty Sent" in df.columns else 0

        delivered_units   = _qty(del_df)
        abandon_units     = _qty(abandon_df)
        claims_units      = _qty(claims_df)
        rto_units         = _qty(rto_df)
        transit_units     = _qty(transit_df)
        undelivered_units = abandon_units + claims_units + rto_units + transit_units
        total_units       = _qty(mdf)
        by_status = {
            "Abandon":    abandon_units,
            "Claims":     claims_units,
            "RTO":        rto_units,
            "In-Transit": transit_units,
        }
        result.append({
            "month": m[:3],
            "month_full": m,
            "delivered_units": delivered_units,
            "undelivered_units": undelivered_units,
            "total_units": total_units,
            "undelivered_by_status": by_status,
        })
    return result


def monthly_shipment_value(df: pd.DataFrame) -> list:
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    if "Pick up Date" in df.columns:
        derived = df["Pick up Date"].dt.month.map(
            lambda n: MONTHS[int(n)-1] if pd.notna(n) else None
        )
        month_col = df["Month"].astype(str).str.strip().replace("", pd.NA).replace("nan", pd.NA)
        df = df.copy()
        df["_month_eff"] = month_col.combine_first(derived)
    else:
        df = df.copy()
        df["_month_eff"] = df["Month"].astype(str).str.strip()
    result = []
    for m in MONTHS:
        mdf = df[df["_month_eff"] == m]
        if not len(mdf):
            continue
        value = float(mdf["Shipment Value"].fillna(0).sum()) if "Shipment Value" in df.columns else 0
        result.append({
            "month": m[:3],
            "month_full": m,
            "shipment_value": round(value, 2),
        })
    return result


def tonnage_report(df: pd.DataFrame, transporters=None, months=None) -> dict:
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    d = df.copy()
    if transporters:
        d = d[d["Transporter"].str.strip().isin(transporters)]
    cw_col   = _chargeable_col(d)
    mode_col = _mode_col(d)
    all_months = [m for m in MONTHS if len(d[d["Month"].astype(str).str.strip() == m]) > 0]
    months_to_show = [m for m in MONTHS if m in months] if months else MONTHS
    rows = []
    for m in months_to_show:
        mdf = d[d["Month"].astype(str).str.strip() == m]
        if len(mdf) == 0:
            continue
        delivered = int(mdf["is_delivered"].sum())
        del_df = mdf[mdf["is_delivered"] & mdf["Delay Days"].notna()]
        tat_count = len(del_df)
        on_time_pct = round(float((del_df["Delay Days"] <= 0).sum()) / tat_count * 100, 1) if tat_count > 0 else None
        chargeable = round(float(mdf[cw_col].fillna(0).sum()), 1) if cw_col else 0
        shipment_value = round(float(mdf["Shipment Value"].fillna(0).sum()), 2) if "Shipment Value" in mdf.columns else 0

        mode_air_pct = mode_air_sea_pct = mode_sea_pct = None
        if mode_col and cw_col and chargeable > 0:
            mode_norm = mdf[mode_col].astype(str).str.strip().str.lower().str.replace(r'\s*\+\s*', '+', regex=True)
            air_wt     = float(mdf[mode_norm == 'air'][cw_col].fillna(0).sum())
            air_sea_wt = float(mdf[mode_norm == 'air+sea'][cw_col].fillna(0).sum())
            sea_wt     = float(mdf[mode_norm == 'sea'][cw_col].fillna(0).sum())
            mode_air_pct     = round(air_wt / chargeable * 100, 1)
            mode_air_sea_pct = round(air_sea_wt / chargeable * 100, 1)
            mode_sea_pct     = round(sea_wt / chargeable * 100, 1)

        rows.append({
            "month": m[:3],
            "shipments": len(mdf),
            "delivered": delivered,
            "on_time_pct": on_time_pct,
            "vol_wt": chargeable,
            "shipment_value": shipment_value,
            "units": int(mdf["Qty Sent"].fillna(0).sum()) if "Qty Sent" in mdf.columns else 0,
            "boxes": int(mdf["No. Of box"].fillna(0).sum()) if "No. Of box" in mdf.columns else 0,
            "mode_air_pct": mode_air_pct,
            "mode_air_sea_pct": mode_air_sea_pct,
            "mode_sea_pct": mode_sea_pct,
        })
    all_transporters = sorted(
        df["Transporter"].dropna().str.strip().replace("", pd.NA).dropna().unique().tolist()
    )
    return {"rows": rows, "transporters": all_transporters, "all_months": all_months}


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


INV_TAB_NAME = "Inventory Transfer"
INV_HEADER_ROW_IDX = 2  # row 3 = index 2
INV_ACTIVE_STATUSES = {"Packing", "In-Transit", "To be Picked Up"}

_inv_cache = {"df": None, "fetched_at": None}
_inv_ws_cache = {"sheet": None, "last_connect": None}
_inv_fetch_lock = threading.Lock()


def _get_inv_worksheet():
    now = datetime.now()
    ws = _inv_ws_cache["sheet"]
    last = _inv_ws_cache["last_connect"]
    if ws is None or last is None or (now - last).total_seconds() > WS_CACHE_TTL_SECONDS:
        client = _connect()
        ws = client.open_by_key(SHEET_ID).worksheet(INV_TAB_NAME)
        _inv_ws_cache["sheet"] = ws
        _inv_ws_cache["last_connect"] = now
    return ws


def _do_fetch_inv():
    try:
        ws = _get_inv_worksheet()
        raw = ws.get_all_values()
    except Exception:
        _inv_ws_cache["sheet"] = None
        ws = _get_inv_worksheet()
        raw = ws.get_all_values()

    if not raw:
        return

    headers = [h.strip() for h in raw[INV_HEADER_ROW_IDX]]
    data_rows = raw[INV_HEADER_ROW_IDX + 1:]

    n = len(headers)
    data_rows = [row + [''] * (n - len(row)) if len(row) < n else row[:n] for row in data_rows]

    df = pd.DataFrame(data_rows, columns=headers)
    df = df[df["SKU"].str.strip() != ""].copy()

    for date_col in ["Pickup Date", "ATD"]:
        if date_col in df.columns:
            raw_d = df[date_col].astype(str).str.strip()
            parsed = pd.to_datetime(raw_d, format="%d-%m-%Y", errors="coerce")
            failed = parsed.isna() & raw_d.ne("") & raw_d.ne("nan")
            if failed.any():
                parsed[failed] = pd.to_datetime(raw_d[failed], dayfirst=True, errors="coerce")
            df[date_col] = parsed

    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0).astype(int)
    if "Sum difference" in df.columns:
        df["Sum difference"] = pd.to_numeric(df["Sum difference"], errors="coerce")
    if "Inwarded by channel GRN" in df.columns:
        df["Inwarded by channel GRN"] = pd.to_numeric(df["Inwarded by channel GRN"], errors="coerce")

    _inv_cache["df"] = df
    _inv_cache["fetched_at"] = datetime.now()


def fetch_inv_data(force=False) -> pd.DataFrame:
    now = datetime.now()
    is_stale = (
        _inv_cache["df"] is None
        or _inv_cache["fetched_at"] is None
        or (now - _inv_cache["fetched_at"]).total_seconds() >= CACHE_TTL_SECONDS
    )
    if not force and _inv_cache["df"] is not None and is_stale:
        if _inv_fetch_lock.acquire(blocking=False):
            def _bg():
                try:
                    _do_fetch_inv()
                finally:
                    _inv_fetch_lock.release()
            threading.Thread(target=_bg, daemon=True).start()
        return _inv_cache["df"]
    if _inv_cache["df"] is None or force:
        with _inv_fetch_lock:
            now2 = datetime.now()
            needs = (
                force
                or _inv_cache["df"] is None
                or _inv_cache["fetched_at"] is None
                or (now2 - _inv_cache["fetched_at"]).total_seconds() >= CACHE_TTL_SECONDS
            )
            if needs:
                if force:
                    _inv_ws_cache["sheet"] = None
                _do_fetch_inv()
    return _inv_cache["df"]


def inventory_transfer_data(force=False) -> dict:
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    df = fetch_inv_data(force=force)
    if df is None or df.empty:
        return {"kpis": {}, "by_month": [], "by_3pl": [], "by_carrier": [],
                "by_channel_flow": [], "pending_rows": [], "sku_summary": []}

    status = df["Status"].str.strip()
    is_delivered = status == "Delivered"
    is_active = status.isin(INV_ACTIVE_STATUSES)

    kpis = {
        "total_shipments": len(df),
        "total_qty": int(df["Quantity"].sum()),
        "delivered_count": int(is_delivered.sum()),
        "delivered_qty": int(df[is_delivered]["Quantity"].sum()),
        "active_count": int(is_active.sum()),
        "active_qty": int(df[is_active]["Quantity"].sum()),
        "packing_count": int((status == "Packing").sum()),
        "in_transit_count": int((status == "In-Transit").sum()),
        "to_pickup_count": int((status == "To be Picked Up").sum()),
        "packing_qty": int(df[status == "Packing"]["Quantity"].sum()),
        "in_transit_qty": int(df[status == "In-Transit"]["Quantity"].sum()),
        "to_pickup_qty": int(df[status == "To be Picked Up"]["Quantity"].sum()),
    }

    by_month = []
    for m in MONTHS:
        mdf = df[df["Month"].astype(str).str.strip() == m]
        if mdf.empty:
            continue
        mst = mdf["Status"].str.strip()
        by_month.append({
            "month": m[:3],
            "total_qty": int(mdf["Quantity"].sum()),
            "delivered_qty": int(mdf[mst == "Delivered"]["Quantity"].sum()),
            "active_qty": int(mdf[mst.isin(INV_ACTIVE_STATUSES)]["Quantity"].sum()),
            "total_count": len(mdf),
            "delivered_count": int((mst == "Delivered").sum()),
        })

    def _pl_rows(gdf):
        tot = len(gdf)
        del_c = int((gdf["Status"].str.strip() == "Delivered").sum())
        return {
            "total": tot,
            "total_qty": int(gdf["Quantity"].sum()),
            "delivered": del_c,
            "active": int(gdf["Status"].str.strip().isin(INV_ACTIVE_STATUSES).sum()),
            "delivery_rate": round(del_c / tot * 100, 1) if tot else 0,
        }

    by_3pl = []
    for pl in sorted(df["3PL"].str.strip().replace("", pd.NA).dropna().unique().tolist()):
        r = {"name": pl}
        r.update(_pl_rows(df[df["3PL"].str.strip() == pl]))
        by_3pl.append(r)

    by_carrier = []
    for carr in sorted(df["Carrier"].str.strip().replace("", pd.NA).dropna().unique().tolist()):
        if not carr:
            continue
        r = {"carrier": carr}
        r.update(_pl_rows(df[df["Carrier"].str.strip() == carr]))
        by_carrier.append(r)

    flow_grp = df.groupby(["From Channel", "TO Channel"]).agg(
        count=("SKU", "count"), qty=("Quantity", "sum")
    ).reset_index()
    by_channel_flow = []
    for _, row in flow_grp.iterrows():
        by_channel_flow.append({
            "from": str(row["From Channel"]),
            "to": str(row["TO Channel"]),
            "count": int(row["count"]),
            "qty": int(row["qty"]),
        })

    pending_rows = []
    for _, row in df[is_active].iterrows():
        pickup_str = row["Pickup Date"].strftime("%d/%m/%Y") if pd.notna(row.get("Pickup Date")) else ""
        pending_rows.append({
            "month": str(row.get("Month", "")),
            "pickup_date": pickup_str,
            "from_channel": str(row.get("From Channel", "")),
            "to_channel": str(row.get("TO Channel", "")),
            "from_label": str(row.get("From (Label #)", "")),
            "to_label": str(row.get("To (Label #)", "")),
            "sku": str(row.get("SKU", "")),
            "qty": int(row.get("Quantity", 0)),
            "3pl": str(row.get("3PL", "")),
            "carrier": str(row.get("Carrier", "")),
            "tracking": str(row.get("Tracking & ETD", "")),
            "status": str(row.get("Status", "")),
        })

    pend_df = df[is_active]
    sku_grp = pend_df.groupby("SKU").apply(lambda g: pd.Series({
        "packing_qty": int(g[g["Status"].str.strip() == "Packing"]["Quantity"].sum()),
        "in_transit_qty": int(g[g["Status"].str.strip() == "In-Transit"]["Quantity"].sum()),
        "to_pickup_qty": int(g[g["Status"].str.strip() == "To be Picked Up"]["Quantity"].sum()),
        "total_pending_qty": int(g["Quantity"].sum()),
    })).reset_index()
    sku_summary = sku_grp.sort_values("total_pending_qty", ascending=False).to_dict(orient="records")

    return {
        "kpis": kpis,
        "by_month": by_month,
        "by_3pl": by_3pl,
        "by_carrier": by_carrier,
        "by_channel_flow": by_channel_flow,
        "pending_rows": pending_rows,
        "sku_summary": sku_summary,
    }


def terminal_status_counts(df: pd.DataFrame, year=None) -> dict:
    if STATUS_COL not in df.columns:
        return {"groups": [], "total": 0}
    if year and year != "All":
        try:
            df = df[_year_mask(df, int(float(str(year).strip())))]
        except (ValueError, TypeError):
            pass
    status_series = df[STATUS_COL].str.strip()
    result = []
    total = 0
    for label, statuses in TERMINAL_STATUS_GROUPS:
        count = int(status_series.isin(statuses).sum())
        result.append({"label": label, "statuses": sorted(statuses), "count": count})
        total += count
    return {"groups": result, "total": total}


XINDUS_TRANSPORTERS = {"Xindus Air + Sea", "Xindus Sea", "Xindus Air"}
XINDUS_MODES = {"Air + Sea", "Sea", "Air"}
AGEING_BUCKET_ORDER = ["0-5", "6-10", "11-20", "21-30", "30+", "31-40", "40+"]


def xindus_tracker(df: pd.DataFrame) -> dict:
    xdf = df[df["Transporter"].str.strip().isin(XINDUS_TRANSPORTERS)].copy()
    if STATUS_COL in xdf.columns:
        xdf = xdf[xdf[STATUS_COL].str.strip().isin({"Delivered", "In-Transit"})].copy()
    if xdf.empty:
        return {"kpis": {}, "modes": {}, "in_transit_shipments": [],
                "ageing_dist": [], "sku_summary": [], "delivered_summary": []}

    cw_col = _chargeable_col(xdf)

    def _safe_sum(sub, col):
        return round(float(sub[col].fillna(0).sum()), 1) if col and col in sub.columns else 0

    def _safe_mean(sub, col):
        vals = sub[col].dropna() if col and col in sub.columns else pd.Series([], dtype=float)
        return round(float(vals.mean()), 1) if len(vals) > 0 else None

    it_mask = ~xdf["is_delivered"]

    # ── Overall KPIs ──
    kpis = {
        "total_rows": len(xdf),
        "total_qty": int(xdf["Qty Sent"].fillna(0).sum()),
        "total_cw": _safe_sum(xdf, cw_col),
        "total_value": _safe_sum(xdf, "Shipment Value"),
        "delivered_count": int(xdf["is_delivered"].sum()),
        "in_transit_count": int(it_mask.sum()),
        "in_transit_qty": int(xdf[it_mask]["Qty Sent"].fillna(0).sum()),
        "in_transit_cw": _safe_sum(xdf[it_mask], cw_col),
        "in_transit_value": _safe_sum(xdf[it_mask], "Shipment Value"),
        "delivery_rate": round(float(xdf["is_delivered"].sum()) / len(xdf) * 100, 1),
    }

    # ── Per-mode breakdown ──
    modes = {}
    for mode_label, transporters in [
        ("Air + Sea", {"Xindus Air + Sea"}),
        ("Sea",       {"Xindus Sea"}),
        ("Air",       {"Xindus Air"}),
    ]:
        mdf = xdf[xdf["Transporter"].str.strip().isin(transporters)]
        if mdf.empty:
            continue
        m_it = mdf[~mdf["is_delivered"]]
        del_df = mdf[mdf["is_delivered"]]

        # Ageing bucket distribution for in-transit
        ageing_buckets = {}
        if "Ageing Bucket" in mdf.columns:
            for b in AGEING_BUCKET_ORDER:
                bdf = m_it[m_it["Ageing Bucket"].str.strip() == b]
                if len(bdf):
                    ageing_buckets[b] = {
                        "count": len(bdf),
                        "qty": int(bdf["Qty Sent"].fillna(0).sum()),
                    }

        # Intransit bucket (Within TAT / Outside TAT)
        intransit_within = 0
        intransit_outside = 0
        if "Intransit Bucket" in m_it.columns:
            intransit_within = int((m_it["Intransit Bucket"].str.strip() == "Within TAT").sum())
            intransit_outside = int((m_it["Intransit Bucket"].str.strip() == "Outside TAT").sum())

        modes[mode_label] = {
            "total": len(mdf),
            "delivered": int(mdf["is_delivered"].sum()),
            "in_transit": int((~mdf["is_delivered"]).sum()),
            "total_qty": int(mdf["Qty Sent"].fillna(0).sum()),
            "in_transit_qty": int(m_it["Qty Sent"].fillna(0).sum()),
            "delivered_qty": int(del_df["Qty Sent"].fillna(0).sum()),
            "total_cw": _safe_sum(mdf, cw_col),
            "in_transit_cw": _safe_sum(m_it, cw_col),
            "total_value": _safe_sum(mdf, "Shipment Value"),
            "in_transit_value": _safe_sum(m_it, "Shipment Value"),
            "delivery_rate": round(float(mdf["is_delivered"].sum()) / len(mdf) * 100, 1) if len(mdf) else 0,
            "avg_actual_tat": _safe_mean(del_df[del_df["Actual TAT"].notna()], "Actual TAT"),
            "avg_prom_tat": _safe_mean(mdf, "Prom TAT"),
            "avg_delay": _safe_mean(del_df[del_df["Delay Days"].notna()], "Delay Days"),
            "on_time_pct": round(float((del_df["Delay Days"].fillna(999) <= 0).sum()) / len(del_df) * 100, 1) if len(del_df) else None,
            "intransit_within_tat": intransit_within,
            "intransit_outside_tat": intransit_outside,
            "ageing_buckets": ageing_buckets,
        }

    # ── In-transit shipments grouped by AWB ──
    it_df = xdf[it_mask].copy()
    it_df["_awb_clean"] = it_df["Shipment AWB"].astype(str).str.strip()

    in_transit_shipments = []
    for awb_raw, grp in it_df.groupby("_awb_clean", sort=False):
        # Split AWB: first line = air waybill, second line = container number
        parts = [p.strip() for p in awb_raw.split("\n") if p.strip()]
        awb = parts[0] if parts else awb_raw
        container = parts[1] if len(parts) > 1 else ""

        pickup = grp["Pick up Date"].dropna().min()
        exp_del = grp["Expected Delivery Date"].dropna().min()
        ageing = grp["Ageing"].dropna().max()
        ageing_bucket = grp["Ageing Bucket"].dropna().iloc[0] if "Ageing Bucket" in grp.columns and not grp["Ageing Bucket"].dropna().empty else ""
        intransit_b = grp["Intransit Bucket"].dropna().iloc[0] if "Intransit Bucket" in grp.columns and not grp["Intransit Bucket"].dropna().empty else ""
        mode = grp["Mode"].dropna().iloc[0] if "Mode" in grp.columns and not grp["Mode"].dropna().empty else ""
        channels = sorted(grp["Channel"].dropna().str.strip().unique().tolist()) if "Channel" in grp.columns else []
        skus = grp["Product Name"].dropna().str.strip().unique().tolist()
        destinations = sorted(grp["Destination"].dropna().str.strip().replace("", pd.NA).dropna().unique().tolist()) if "Destination" in grp.columns else []

        in_transit_shipments.append({
            "awb": awb,
            "container": container,
            "mode": mode,
            "pickup_date": pickup.strftime("%d/%m/%Y") if pd.notna(pickup) else "",
            "exp_delivery": exp_del.strftime("%d/%m/%Y") if pd.notna(exp_del) else "",
            "ageing": int(ageing) if pd.notna(ageing) else None,
            "ageing_bucket": ageing_bucket,
            "intransit_bucket": intransit_b,
            "total_qty": int(grp["Qty Sent"].fillna(0).sum()),
            "total_cw": _safe_sum(grp, cw_col),
            "channels": channels,
            "sku_count": len(skus),
            "skus": skus,
            "destination_count": len(destinations),
        })

    # Sort by ageing descending (most overdue first)
    in_transit_shipments.sort(key=lambda r: r["ageing"] or 0, reverse=True)

    # ── Ageing distribution for chart (in-transit only) ──
    ageing_dist = []
    for b in AGEING_BUCKET_ORDER:
        row = {"bucket": b}
        for mode_label, transporters in [("Air + Sea", {"Xindus Air + Sea"}), ("Sea", {"Xindus Sea"})]:
            mdf_it = xdf[it_mask & xdf["Transporter"].str.strip().isin(transporters)]
            bdf = mdf_it[mdf_it["Ageing Bucket"].str.strip() == b] if "Ageing Bucket" in mdf_it.columns else mdf_it.iloc[0:0]
            row[mode_label] = {"count": len(bdf), "qty": int(bdf["Qty Sent"].fillna(0).sum())}
        if row["Air + Sea"]["count"] + row.get("Sea", {}).get("count", 0) > 0:
            ageing_dist.append(row)

    # ── SKU summary (in-transit) ──
    sku_rows = []
    for sku, sdf in it_df.groupby("Product Name"):
        r = {"sku": str(sku)}
        for mode_label, transporters in [("Air + Sea", {"Xindus Air + Sea"}), ("Sea", {"Xindus Sea"})]:
            mdf = sdf[sdf["Transporter"].str.strip().isin(transporters)]
            r[mode_label] = int(mdf["Qty Sent"].fillna(0).sum())
        r["total"] = int(sdf["Qty Sent"].fillna(0).sum())
        sku_rows.append(r)
    sku_rows.sort(key=lambda r: r["total"], reverse=True)

    # ── Delivered summary (by mode + channel) ──
    del_df_all = xdf[xdf["is_delivered"]].copy()
    delivered_summary = []
    if not del_df_all.empty:
        grp = del_df_all.groupby(["Mode", "Channel"]).agg(
            count=("Shipment AWB", "count"),
            qty=("Qty Sent", "sum"),
            avg_tat=("Actual TAT", "mean"),
            avg_delay=("Delay Days", "mean"),
        ).reset_index().round(1)
        for _, row in grp.iterrows():
            delivered_summary.append({
                "mode": str(row["Mode"]),
                "channel": str(row["Channel"]),
                "count": int(row["count"]),
                "qty": int(row["qty"]),
                "avg_tat": row["avg_tat"] if pd.notna(row["avg_tat"]) else None,
                "avg_delay": row["avg_delay"] if pd.notna(row["avg_delay"]) else None,
            })

    return {
        "kpis": kpis,
        "modes": modes,
        "in_transit_shipments": in_transit_shipments,
        "ageing_dist": ageing_dist,
        "sku_summary": sku_rows,
        "delivered_summary": delivered_summary,
    }


# Pre-warm the inventory transfer cache (defined after fetch_inv_data)
threading.Thread(target=fetch_inv_data, daemon=True).start()

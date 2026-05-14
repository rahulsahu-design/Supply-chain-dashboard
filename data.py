import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime, timedelta
import os
import base64
import json

SHEET_ID = "1ixmX8rsx9jiGzvSgG8dwXAdQGINGYMprJexgiV4MOwk"
TAB_NAME = "Shipment Tracker AWB wise"
CREDS_FILE = r"D:\Claude Code\Shipment tracker dashobard\clean-algebra-496218-q4-dcb8941fbf42.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

STATUS_COL = "Current Status - Ship partner portal"
DELIVERED_STATUSES = {"Delivered"}
OVERDUE_BUCKETS = {"21-30", "30+"}

_cache = {"df": None, "fetched_at": None}
CACHE_TTL_SECONDS = 300


def _connect():
    b64 = os.environ.get("GOOGLE_CREDS_B64")
    if b64:
        b64 = "".join(b64.split())  # remove ALL whitespace including internal newlines
        b64 += "=" * (-len(b64) % 4)  # fix padding if missing
        info = json.loads(base64.b64decode(b64).decode("utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def fetch_data(force=False) -> pd.DataFrame:
    now = datetime.now()
    if (
        not force
        and _cache["df"] is not None
        and _cache["fetched_at"]
        and (now - _cache["fetched_at"]).seconds < CACHE_TTL_SECONDS
    ):
        return _cache["df"]

    client = _connect()
    sheet = client.open_by_key(SHEET_ID).worksheet(TAB_NAME)
    raw = sheet.get_all_values()

    if not raw:
        return pd.DataFrame()

    # Row 4 (index 3) is the header row; data starts at row 5 (index 4)
    HEADER_ROW_IDX = 3
    headers = [h.strip() for h in raw[HEADER_ROW_IDX]]
    data_rows = raw[HEADER_ROW_IDX + 1:]

    # Pad short rows to match header length
    n = len(headers)
    data_rows = [row + [''] * (n - len(row)) if len(row) < n else row[:n] for row in data_rows]

    df = pd.DataFrame(data_rows, columns=headers)

    # Drop columns with empty headers (trailing blank columns)
    df = df.loc[:, df.columns != '']

    # Parse date columns — let pandas infer format (handles DD/MM/YYYY, DD-MM-YYYY, ISO, etc.)
    date_cols = ["Pick up Date", "Actual Delivery Date", "Expected Delivery Date"]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], dayfirst=True, errors="coerce")

    # Numeric coercions
    for col in ["Actual TAT", "Prom TAT", "Delay Days", "Ageing", "Qty Sent"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Derived flags
    df["is_delivered"] = df[STATUS_COL].str.strip().isin(DELIVERED_STATUSES)
    df["is_overdue"] = df["Ageing Bucket"].str.strip().isin(OVERDUE_BUCKETS)

    _cache["df"] = df
    _cache["fetched_at"] = now
    return df


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
    return {"years": years, "months": months, "weeks": weeks, "del_weeks": del_weeks}


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


def delivered_shipments(df: pd.DataFrame, channel=None, del_week=None, date_from=None, date_to=None) -> list:
    d = df[df["is_delivered"]].copy()
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
            out[dc] = out[dc].dt.strftime("%d/%m/%Y").fillna("")
    return out.fillna("").to_dict(orient="records")


def delivered_by_date_channel(df: pd.DataFrame, channel=None, del_week=None, date_from=None, date_to=None) -> dict:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna()].copy()
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


def delivered_pivot(df: pd.DataFrame, channels=None, del_weeks=None, date_from=None, date_to=None, days=14) -> dict:
    d = df[df["is_delivered"] & df["Actual Delivery Date"].notna() & df["Qty Sent"].notna()].copy()
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


def undelivered_shipments(df: pd.DataFrame) -> list:
    d = df[~df["is_delivered"]].copy()
    cols = [
        "Shipment AWB", "Channel", "Transporter", "Pick up Date",
        "Expected Delivery Date", "Ageing", "Ageing Bucket",
        "Delay Days", STATUS_COL, "Product Name", "Qty Sent", "is_overdue",
    ]
    cols = [c for c in cols if c in d.columns]
    out = d[cols].copy()
    for dc in ["Pick up Date", "Expected Delivery Date"]:
        if dc in out.columns:
            out[dc] = out[dc].dt.strftime("%d/%m/%Y").fillna("")
    out["is_overdue"] = out["is_overdue"].astype(bool)
    return out.fillna("").to_dict(orient="records")


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


def shipment_ageing(df: pd.DataFrame) -> list:
    bucket_order = ["0-5", "6-10", "11-20", "21-30", "30+"]
    d = df[~df["is_delivered"]].copy()
    counts = d["Ageing Bucket"].str.strip().value_counts().to_dict()
    return [{"bucket": b, "count": counts.get(b, 0)} for b in bucket_order]


def transporter_scorecard(df: pd.DataFrame) -> list:
    d = df[df["Actual TAT"].notna() & df["Prom TAT"].notna()].copy()
    grouped = d.groupby("Transporter").agg(
        total=("Shipment AWB", "count"),
        delivered=("is_delivered", "sum"),
        avg_actual_tat=("Actual TAT", "mean"),
        avg_prom_tat=("Prom TAT", "mean"),
        avg_delay=("Delay Days", "mean"),
        on_time=("Delay Days", lambda x: (x <= 0).sum()),
    ).reset_index()

    grouped["on_time_pct"] = (grouped["on_time"] / grouped["total"] * 100).round(1)
    grouped["delivery_rate"] = (grouped["delivered"] / grouped["total"] * 100).round(1)
    grouped = grouped.round(2).sort_values("on_time_pct", ascending=False)
    return grouped.to_dict(orient="records")


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

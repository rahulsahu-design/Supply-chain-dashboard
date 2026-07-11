from flask import Flask, jsonify, render_template, request
import os
import data as d

app = Flask(__name__)


def _df():
    force = request.args.get("refresh") == "1"
    return d.fetch_data(force=force)


def _filtered_df():
    df = _df()
    return d.apply_filters(
        df,
        year=request.args.get("year"),
        month=request.args.get("month"),
        week=request.args.get("week"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/filter-options")
def api_filter_options():
    return jsonify(d.get_filter_options(_df()))


@app.route("/api/daily-summary")
def api_daily_summary():
    return jsonify(d.daily_operations_summary(_filtered_df()))


@app.route("/api/delivered")
def api_delivered():
    df = _filtered_df()
    channel_raw = request.args.get("channel", "All")
    del_week_raw = request.args.get("del_week", "All")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    del_year = request.args.get("del_year")
    channels = [c.strip() for c in channel_raw.split(",")] if channel_raw and channel_raw != "All" else None
    del_weeks = [w.strip() for w in del_week_raw.split(",")] if del_week_raw and del_week_raw != "All" else None
    rows = d.delivered_shipments(df, channel=channels, del_week=del_weeks, date_from=date_from, date_to=date_to, year=del_year)
    charts = d.delivered_by_date_channel(df, channel=channels, del_week=del_weeks, date_from=date_from, date_to=date_to, year=del_year)
    return jsonify({"rows": rows, "charts": charts})


@app.route("/api/delivered-pivot")
def api_delivered_pivot():
    df = _filtered_df()
    channels_raw = request.args.get("channels", "All")
    del_weeks_raw = request.args.get("del_weeks", "All")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    del_year = request.args.get("del_year")
    channels = [c.strip() for c in channels_raw.split(",")] if channels_raw and channels_raw != "All" else None
    del_weeks = [w.strip() for w in del_weeks_raw.split(",")] if del_weeks_raw and del_weeks_raw != "All" else None
    return jsonify(d.delivered_pivot(df, channels=channels, del_weeks=del_weeks, date_from=date_from, date_to=date_to, year=del_year))


@app.route("/api/undelivered")
def api_undelivered():
    df = _filtered_df()
    channel_raw = request.args.get("channel", "All")
    exp_del_week_raw = request.args.get("exp_del_week", "All")
    statuses_raw = request.args.get("statuses", "All")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    year = request.args.get("undel_year")
    channels = [c.strip() for c in channel_raw.split(",")] if channel_raw and channel_raw != "All" else None
    exp_del_weeks = [w.strip() for w in exp_del_week_raw.split(",")] if exp_del_week_raw and exp_del_week_raw != "All" else None
    statuses = [s.strip() for s in statuses_raw.split(",")] if statuses_raw and statuses_raw != "All" else None
    return jsonify(d.undelivered_shipments(df, channel=channels, exp_del_week=exp_del_weeks,
                                           date_from=date_from, date_to=date_to, year=year, statuses=statuses))


@app.route("/api/undelivered-pivot")
def api_undelivered_pivot():
    df = _filtered_df()
    channels_raw = request.args.get("channels", "All")
    exp_del_weeks_raw = request.args.get("exp_del_weeks", "All")
    statuses_raw = request.args.get("statuses", "All")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    year = request.args.get("undel_year")
    channels = [c.strip() for c in channels_raw.split(",")] if channels_raw and channels_raw != "All" else None
    exp_del_weeks = [w.strip() for w in exp_del_weeks_raw.split(",")] if exp_del_weeks_raw and exp_del_weeks_raw != "All" else None
    statuses = [s.strip() for s in statuses_raw.split(",")] if statuses_raw and statuses_raw != "All" else None
    return jsonify(d.undelivered_pivot(df, channels=channels, exp_del_weeks=exp_del_weeks,
                                       date_from=date_from, date_to=date_to, year=year, statuses=statuses))


@app.route("/api/raw-data")
def api_raw_data():
    return jsonify(d.raw_data_2026(d.fetch_data()))


@app.route("/api/tat")
def api_tat():
    days = int(request.args.get("days", 7))
    return jsonify(d.tat_analysis(_filtered_df(), days=days))


@app.route("/api/tat-pivot")
def api_tat_pivot():
    df = _filtered_df()
    channels_raw = request.args.get("channels", "All")
    del_weeks_raw = request.args.get("del_weeks", "All")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    channels = [c.strip() for c in channels_raw.split(",")] if channels_raw and channels_raw != "All" else None
    del_weeks = [w.strip() for w in del_weeks_raw.split(",")] if del_weeks_raw and del_weeks_raw != "All" else None
    return jsonify(d.tat_pivot(df, channels=channels, del_weeks=del_weeks, date_from=date_from, date_to=date_to))


@app.route("/api/ageing")
def api_ageing():
    df = _filtered_df()
    channels_raw = request.args.get("channels", "All")
    exp_del_weeks_raw = request.args.get("exp_del_weeks", "All")
    statuses_raw = request.args.get("statuses", "All")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    year = request.args.get("undel_year")
    channels = [c.strip() for c in channels_raw.split(",")] if channels_raw and channels_raw != "All" else None
    exp_del_weeks = [w.strip() for w in exp_del_weeks_raw.split(",")] if exp_del_weeks_raw and exp_del_weeks_raw != "All" else None
    statuses = [s.strip() for s in statuses_raw.split(",")] if statuses_raw and statuses_raw != "All" else None
    return jsonify(d.shipment_ageing(df, channels=channels, exp_del_weeks=exp_del_weeks,
                                     date_from=date_from, date_to=date_to, year=year, statuses=statuses))


@app.route("/api/scorecard")
def api_scorecard():
    return jsonify(d.transporter_scorecard(_filtered_df()))


@app.route("/api/monthly-deliveries")
def api_monthly_deliveries():
    transporters_raw = request.args.get("transporters", "All")
    transporters = [t.strip() for t in transporters_raw.split(",")] if transporters_raw and transporters_raw != "All" else None
    return jsonify(d.monthly_deliveries(_filtered_df(), transporters=transporters))


@app.route("/api/monthly-shipment-value")
def api_monthly_shipment_value():
    return jsonify(d.monthly_shipment_value(_filtered_df()))


@app.route("/api/tonnage")
def api_tonnage():
    transporters_raw = request.args.get("transporters", "All")
    months_raw = request.args.get("months", "All")
    transporters = [t.strip() for t in transporters_raw.split(",")] if transporters_raw and transporters_raw != "All" else None
    months = [m.strip() for m in months_raw.split(",")] if months_raw and months_raw != "All" else None
    return jsonify(d.tonnage_report(_filtered_df(), transporters=transporters, months=months))


@app.route("/api/channel-health")
def api_channel_health():
    return jsonify(d.channel_health(_filtered_df()))


@app.route("/api/inventory-transfer")
def api_inventory_transfer():
    force = request.args.get("refresh") == "1"
    return jsonify(d.inventory_transfer_data(force=force))


@app.route("/api/terminal-status-counts")
def api_terminal_status_counts():
    year = request.args.get("year", "2026")
    return jsonify(d.terminal_status_counts(_df(), year=year))


@app.route("/api/trends")
def api_trends():
    df = _filtered_df()
    return jsonify({
        "weekly": d.weekly_trend(df),
        "monthly": d.monthly_trend(df),
    })


@app.route("/api/debug")
def api_debug():
    # Fetch raw sheet values before any parsing
    client = d._connect()
    sheet = client.open_by_key(d.SHEET_ID).worksheet(d.TAB_NAME)
    raw = sheet.get_all_values()
    HEADER_ROW_IDX = 3
    headers = [h.strip() for h in raw[HEADER_ROW_IDX]]
    data_rows = raw[HEADER_ROW_IDX + 1:]

    def raw_samples(col_name, n=5):
        if col_name not in headers:
            return f"column '{col_name}' not found"
        idx = headers.index(col_name)
        samples = []
        for row in data_rows:
            if idx < len(row) and row[idx].strip():
                samples.append(row[idx])
            if len(samples) >= n:
                break
        return samples

    df = d.fetch_data(force=True)
    return jsonify({
        "total_rows": len(df),
        "year_samples": df["Year"].dropna().astype(str).unique().tolist()[:10] if "Year" in df.columns else "col missing",
        "raw_pickup_date_samples": raw_samples("Pick up Date"),
        "raw_delivery_date_samples": raw_samples("Actual Delivery Date"),
        "parsed_pickup_date_samples": df["Pick up Date"].dropna().dt.strftime("%d/%m/%Y").unique().tolist()[:5] if "Pick up Date" in df.columns else "col missing",
        "parsed_delivery_date_samples": df["Actual Delivery Date"].dropna().dt.strftime("%d/%m/%Y").unique().tolist()[:5] if "Actual Delivery Date" in df.columns else "col missing",
        "pickup_date_nulls": int(df["Pick up Date"].isna().sum()) if "Pick up Date" in df.columns else "col missing",
        "delivery_date_nulls": int(df["Actual Delivery Date"].isna().sum()) if "Actual Delivery Date" in df.columns else "col missing",
        "delivered_count": int(df["is_delivered"].sum()),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)

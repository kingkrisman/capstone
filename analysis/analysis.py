"""
Logistics Operations Capstone: analysis pipeline.

Reads the raw tables from ../data, computes every figure shown in the
dashboard and report, writes analysis/output/data.json, and builds index.html.

Usage (from the repo root):
    pip install -r requirements.txt
    python analysis/analysis.py
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "analysis" / "output"
OUT.mkdir(parents=True, exist_ok=True)


def load():
    r = lambda name, **kw: pd.read_csv(DATA / f"{name}.csv", **kw)
    return dict(
        loads=r("loads", parse_dates=["load_date"]),
        trips=r("trips"),
        fuel=r("fuel_purchases", parse_dates=["purchase_date"]),
        maint=r("maintenance_records", parse_dates=["maintenance_date"]),
        events=r("delivery_events"),
        safety=r("safety_incidents", parse_dates=["incident_date"]),
        drivers=r("drivers"),
        customers=r("customers"),
        routes=r("routes"),
        trucks=r("trucks"),
        facilities=r("facilities"),
        util=r("truck_utilization_metrics"),
    )


def build_trip_table(t):
    """One row per trip, joined to its load, route, customer and stop events."""
    L, T, F, E = t["loads"], t["trips"].copy(), t["fuel"], t["events"]
    L = L.assign(total_rev=L.revenue + L.fuel_surcharge + L.accessorial_charges)

    pickup = E[E.event_type == "Pickup"].set_index("trip_id")
    deliv = E[E.event_type == "Delivery"].set_index("trip_id")
    T["ontime"] = T.trip_id.map(deliv.on_time_flag)
    T["pk_ontime"] = T.trip_id.map(pickup.on_time_flag)
    T["det_delivery"] = T.trip_id.map(deliv.detention_minutes)
    T["facility_type"] = T.trip_id.map(deliv.facility_id).map(
        t["facilities"].set_index("facility_id").facility_type)
    T["delay_min"] = (pd.to_datetime(T.trip_id.map(deliv.actual_datetime))
                      - pd.to_datetime(T.trip_id.map(deliv.scheduled_datetime))).dt.total_seconds() / 60
    T["gal_bought"] = T.trip_id.map(F.groupby("trip_id").gallons.sum()).fillna(0)

    X = (T.merge(L, on="load_id")
          .merge(t["routes"], on="route_id")
          .merge(t["customers"][["customer_id", "customer_name", "customer_type", "account_status"]], on="customer_id"))
    X["lane"] = X.origin_city + " → " + X.destination_city
    X["ym"] = X.load_date.dt.to_period("M")
    X["yr"] = X.load_date.dt.year

    # Fuel cost is estimated from gallons burned x that month's average pump price.
    # Fuel purchase records are NOT used for cost: they don't track trip distance (see data notes).
    price = F.groupby(F.purchase_date.dt.to_period("M")).price_per_gallon.mean()
    X["est_fuel"] = X.fuel_gallons_used * X.ym.map(price)
    return X, price


def metrics_for(X, M, S, key):
    x = X if key == "all" else X[X.yr == key]
    m = M if key == "all" else M[M.yr == key]
    s = S if key == "all" else S[S.yr == key]
    rev, miles = x.total_rev.sum(), x.actual_distance_miles.sum()

    k = dict(
        rev=rev, loads=len(x), miles=miles, rpm=rev / miles,
        fuel=x.est_fuel.sum(), fuelShare=x.est_fuel.sum() / rev,
        ot=x.ontime.mean(), pk=x.pk_ontime.mean(),
        mpg=miles / x.fuel_gallons_used.sum(),
        maint=m.total_cost.sum(), down=m.downtime_hours.sum(), emerg=(m.kind == "Emergency").mean(),
        inc=len(s), claims=s.claim_amount.sum(), prev=s.preventable_flag.mean(),
        prevCost=s[s.preventable_flag].claim_amount.sum(), incRate=len(s) / miles * 1e6,
        inactiveShare=x[x.account_status == "Inactive"].total_rev.sum() / rev,
        detDel=x.det_delivery.mean(), det2h=(x.det_delivery > 120).mean(),
        delayMed=x.delay_min.median(), lateAvg=x.delay_min[x.delay_min > 120].mean(),
    )

    ln = x.groupby("lane").agg(loads=("trip_id", "count"), rev=("total_rev", "sum"),
                               miles=("actual_distance_miles", "sum"), fuel=("est_fuel", "sum"),
                               dist=("typical_distance_miles", "first"), ot=("ontime", "mean"))
    ln["rpm"], ln["cpm"], ln["net"] = ln.rev / ln.miles, ln.fuel / ln.miles, ln.rev - ln.fuel
    lanes = [dict(lane=i, **{c: round(float(v), 4) for c, v in row.items()})
             for i, row in ln.sort_values("rpm").iterrows()]

    top = (x.groupby("customer_id").agg(rev=("total_rev", "sum"), loads=("trip_id", "count"), ot=("ontime", "mean"))
             .sort_values("rev", ascending=False).head(10)
             .join(X.drop_duplicates("customer_id").set_index("customer_id")[["customer_name", "customer_type", "account_status"]])
             .reset_index())

    g = lambda df, by, **agg: df.groupby(by).agg(**agg).reset_index()
    return dict(
        k={a: float(b) for a, b in k.items()},
        lanes=lanes,
        ctype=g(x, "customer_type", rev=("total_rev", "sum"), ot=("ontime", "mean"), loads=("trip_id", "count")).to_dict("records"),
        booking=g(x, "booking_type", rev=("total_rev", "sum"), loads=("trip_id", "count")).to_dict("records"),
        top=top.to_dict("records"),
        fac=x.groupby("facility_type").ontime.mean().round(4).to_dict(),
        mtype=g(m, "maintenance_type", n=("maintenance_id", "count"), cost=("total_cost", "sum"),
                down=("downtime_hours", "sum")).sort_values("cost", ascending=False).to_dict("records"),
        mkind=g(m, "kind", n=("maintenance_id", "count"), cost=("total_cost", "sum")).to_dict("records"),
        stype=g(s, "incident_type", n=("incident_id", "count"), cost=("claim_amount", "sum"),
                prev=("preventable_flag", "sum")).sort_values("cost", ascending=False).to_dict("records"),
    )


def main():
    t = load()
    X, price = build_trip_table(t)
    M, S, E = t["maint"].copy(), t["safety"].copy(), t["events"]
    M["yr"], M["kind"] = M.maintenance_date.dt.year, M.service_description.str.split().str[0]
    S["yr"] = S.incident_date.dt.year

    out = {str(k): metrics_for(X, M, S, k) for k in ["all", 2022, 2023, 2024]}

    mo = X.groupby("ym").agg(rev=("total_rev", "sum"), loads=("trip_id", "count"), fuel=("est_fuel", "sum"),
                             ot=("ontime", "mean"), pk=("pk_ontime", "mean"))
    out["monthly"] = dict(labels=[str(p) for p in mo.index], rev=mo.rev.round(0).tolist(), loads=mo.loads.tolist(),
                          ot=mo.ot.round(4).tolist(), pk=mo.pk.round(4).tolist(),
                          fuelShare=(mo.fuel / mo.rev).round(4).tolist(),
                          price=[round(float(price[p]), 3) for p in mo.index])

    out["delay"] = dict(labels=["2–3h early", "1–2h early", "0–1h early", "0–1h late", "1–2h late",
                                "2–3h late", "3–4h late", "4–5h late", "5–6h late"],
                        n=pd.cut(X.delay_min, [-180, -120, -60, 0, 60, 120, 180, 240, 300, 360])
                          .value_counts().sort_index().tolist())
    bins = [-1, 0, 60, 120, 180, 240, 300, 1e9]
    out["det"] = dict(labels=["None", "Up to 1h", "1–2h", "2–3h", "3–4h", "4–5h", "5h+"],
                      pk=pd.cut(E[E.event_type == "Pickup"].detention_minutes, bins).value_counts().sort_index().tolist(),
                      dl=pd.cut(E[E.event_type == "Delivery"].detention_minutes, bins).value_counts().sort_index().tolist())

    si = (S.groupby("driver_id").agg(inc=("incident_id", "count"), cost=("claim_amount", "sum"),
                                     prev=("preventable_flag", "sum"))
            .sort_values(["inc", "cost"], ascending=False).head(6)
            .join(t["drivers"].set_index("driver_id")[["first_name", "last_name", "years_experience"]]).reset_index())
    si["trips"] = si.driver_id.map(X.driver_id.value_counts())
    out["drvinc"] = si.to_dict("records")

    TK, F, U, C = t["trucks"], t["fuel"], t["util"], t["customers"]
    out["fleet"] = dict(status=TK.status.value_counts().to_dict(),
                        years=TK.model_year.value_counts().sort_index().to_dict())
    out["dq"] = dict(
        galBought=float(F.gallons.sum()), galUsed=float(X.fuel_gallons_used.sum()),
        corr=float(np.corrcoef(X.actual_distance_miles, X.gal_bought)[0, 1]),
        missDrv=int(t["trips"].driver_id.isna().sum()), missTrk=int(t["trips"].truck_id.isna().sum()),
        trips=len(t["trips"]), utilOver=int((U.utilization_rate > 1).sum()), utilN=len(U),
        custIds=len(C), custNames=int(C.customer_name.nunique()),
        fuel2025=int((F.purchase_date.dt.year == 2025).sum()),
        excessDetentionHours=float((E.detention_minutes.clip(lower=120) - 120).sum() / 60),
    )

    data = json.dumps(out, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    (OUT / "data.json").write_text(data, encoding="utf-8")
    tpl = (ROOT / "analysis" / "dashboard_template.html").read_text(encoding="utf-8")
    (ROOT / "index.html").write_text(tpl.replace("__DATA__", data), encoding="utf-8")

    k = out["all"]["k"]
    print(f"Revenue ${k['rev']/1e6:.1f}M | {k['loads']:,.0f} loads | on-time {k['ot']:.1%} | "
          f"fuel share {k['fuelShare']:.1%} | wrote index.html and analysis/output/data.json")


if __name__ == "__main__":
    main()

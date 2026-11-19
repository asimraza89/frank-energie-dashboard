"""Frank Energie home dashboard (local-first).

Run with:  streamlit run app.py

Each person runs their own copy. Login tokens and the data cache live in
~/.frank-dashboard/<account>/ on that person's own computer.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from frankdash import analysis as an
from frankdash import charts, client, weather
from frankdash.config import DATA_ROOT, DEFAULT_DETAIL_DAYS, DEFAULT_MAX_HISTORY_YEARS, TZ, account_dir, account_id
from frankdash.storage import Store
from frankdash.sync import sync

st.set_page_config(page_title="Home energy", page_icon="⚡", layout="wide")

DEMO = "demo"
ENERGY_LABEL = {"electricity": "Electricity", "gas": "Gas", "feed_in": "Solar feed-in"}
UNIT = {"electricity": "kWh", "gas": "m³", "feed_in": "kWh"}


def eur(x: float, digits: int = 0) -> str:
    return f"€ {x:,.{digits}f}".replace(",", " ")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Accounts and login
# ---------------------------------------------------------------------------
def make_client(account: str, tokens: client.Tokens | None):
    if account == DEMO:
        from frankdash.mock import MockFrankEnergie

        return MockFrankEnergie(auth_token=tokens.auth_token if tokens else None, refresh_token=tokens.refresh_token if tokens else None)
    return client.real_client(tokens)


async def _with_client(account, tokens, fn):
    fe = make_client(account, tokens)
    try:
        result = await fn(fe)
        return result, client.current_tokens(fe)
    finally:
        await fe.close()


def remembered_accounts() -> list[tuple[str, str]]:
    out = []
    if DATA_ROOT.exists():
        for d in sorted(DATA_ROOT.iterdir()):
            if (d / "tokens.json").exists() and (d / "email.txt").exists():
                out.append((d.name, (d / "email.txt").read_text().strip()))
    return out


def sign_in(account: str, email: str, tokens: client.Tokens, remember: bool) -> None:
    folder = account_dir(account)
    (folder / "email.txt").write_text(email)
    if remember:
        tokens.save(folder / "tokens.json")
    else:
        (folder / "tokens.json").unlink(missing_ok=True)
    st.session_state.update(account=account, email=email, tokens=tokens, remember=remember, synced=False)


def sign_out(delete_data: bool) -> None:
    account = st.session_state.get("account")
    if account:
        folder = account_dir(account)
        (folder / "tokens.json").unlink(missing_ok=True)
        if delete_data:
            shutil.rmtree(folder, ignore_errors=True)
    for key in ("account", "email", "tokens", "remember", "synced", "site"):
        st.session_state.pop(key, None)


def login_screen() -> None:
    st.title("⚡ Home energy dashboard")
    st.write(
        "Sign in with your Frank Energie account. Your password is sent only to Frank Energie and is never "
        "saved. Your energy data is cached on this computer only, in "
        f"`{DATA_ROOT}`."
    )
    for acc, email in remembered_accounts():
        if st.button(f"Continue as {email}", key=f"cont-{acc}"):
            tokens = client.Tokens.load(account_dir(acc) / "tokens.json")
            sign_in(acc, email, tokens, remember=True)
            st.rerun()

    with st.form("login"):
        email = st.text_input("Frank Energie email")
        password = st.text_input("Password", type="password")
        remember = st.checkbox("Keep me signed in on this computer", value=True,
                               help="Stores a login token (not your password) in your local data folder.")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            tokens, _ = run(_with_client(None, None, lambda fe: client.login(fe, email, password)))
        except Exception as err:  # noqa: BLE001 - show any login problem to the user
            st.error(f"Sign-in failed: {err}")
        else:
            sign_in(account_id(email), email.strip(), tokens, remember)
            st.rerun()

    st.divider()
    if st.button("Try it with demo data"):
        tokens, _ = run(_with_client(DEMO, None, lambda fe: client.login(fe, "demo", "demo")))
        sign_in(DEMO, "Demo household", tokens, remember=False)
        st.rerun()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def do_sync(store: Store, site: dict, history_start: date, detail_days: int) -> None:
    bar = st.progress(0.0, text="Checking what is new…")

    def progress(done: int, total: int) -> None:
        bar.progress(done / total if total else 1.0, text=f"Fetching {done}/{total} missing days and months…" if total else "Up to date")

    account, tokens = st.session_state.account, st.session_state.tokens
    try:
        report, new_tokens = run(
            _with_client(account, tokens, lambda fe: sync(fe, store, site["reference"], history_start, detail_days, progress=progress))
        )
    except Exception as err:  # noqa: BLE001
        bar.empty()
        st.error(f"Could not reach Frank Energie: {err}")
        return
    bar.empty()
    if new_tokens and new_tokens != tokens:
        st.session_state.tokens = new_tokens
        if st.session_state.get("remember"):
            new_tokens.save(account_dir(account) / "tokens.json")
    st.session_state.synced = True
    if report.stopped_early:
        st.warning(f"Stopped early ({report.stopped_early}). What was fetched is saved; try again later or sign in again.")
    elif report.failed:
        st.warning(f"{report.failed} of {report.planned} requests failed and will be retried next time.")
        with st.expander("Details"):
            st.write(report.errors[:50])
    elif report.planned:
        st.toast(f"Fetched {report.done} new or updated items")


def load_sites(store: Store) -> list[dict]:
    sites = store.get_meta("sites")
    if not sites:
        sites, _ = run(_with_client(st.session_state.account, st.session_state.tokens, client.site_info))
        store.set_meta("sites", sites)
    return sites


@st.cache_data(show_spinner=False)
def load_frames(db_path: str, site: str, version: str, city: str | None):
    store = Store(db_path)
    try:
        hourly = an.to_hourly(store.hourly_usage(site))
        month_slots = store.daily_usage(site)
        prices = store.prices(site)
        wx = store.weather(weather.location_key(city)) if city else pd.DataFrame(columns=["day", "tmean"])
    finally:
        store.close()
    daily = an.daily_totals(month_slots, hourly)
    monthly = an.monthly_totals(month_slots, hourly)
    return hourly, daily, monthly, prices, wx


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------
def dashboard() -> None:
    account = st.session_state.account
    folder = account_dir(account)
    store = Store(folder / "data.sqlite")
    today = datetime.now(TZ).date()

    try:
        sites = load_sites(store)
    except Exception as err:  # noqa: BLE001
        st.error(f"Could not load your delivery address from Frank Energie: {err}")
        if st.button("Sign in again"):
            sign_out(delete_data=False)
            st.rerun()
        return

    with st.sidebar:
        st.markdown(f"**{st.session_state.email}**")
        site = sites[0]
        if len(sites) > 1:
            labels = [f"{s['address']} ({s['status'].lower()})" for s in sites]
            site = sites[st.selectbox("Address", range(len(sites)), format_func=lambda i: labels[i])]
        first = site.get("first_reading") or site.get("delivery_start")
        earliest = date.fromisoformat(first[:10]) if first else today - timedelta(365)
        earliest = max(earliest, today - timedelta(365 * DEFAULT_MAX_HISTORY_YEARS))
        with st.expander("Settings"):
            history_start = st.date_input("Fetch history from", value=earliest, min_value=earliest, max_value=today)
            detail_days = st.slider(
                "Days of hourly detail", 30, 730, DEFAULT_DETAIL_DAYS, step=10,
                help="Hourly data takes one request per day; monthly totals cover the whole history.",
            )
            city = st.selectbox("Nearest city (for gas vs temperature)", ["(off)"] + list(weather.CITIES), index=4)
            city = None if city == "(off)" else city
        sync_clicked = st.button("Fetch new data", type="primary", width="stretch")
        last_box = st.empty()
        st.divider()
        delete = st.checkbox("Also delete my cached data")
        if st.button("Sign out", width="stretch"):
            store.close()
            sign_out(delete)
            st.rerun()

    if sync_clicked or not st.session_state.get("synced"):
        do_sync(store, site, history_start, detail_days)
        if city:
            try:
                fetch = weather.fetch_archive
                if account == DEMO:
                    from frankdash.mock import demo_weather as fetch
                weather.sync_weather(store, city, history_start, today - timedelta(6), fetch=fetch)
            except Exception as err:  # noqa: BLE001 - weather is optional
                st.caption(f"Weather data unavailable right now ({err}).")

    last = store.get_meta("last_sync")
    last_box.caption(f"Last fetched: {last[:16].replace('T', ' ') if last else 'never'}. Only missing or still-changing days are fetched.")
    version = f"{last}|{sync_clicked}"
    db_path = store.path
    store.close()
    hourly, daily, monthly, prices, wx = load_frames(db_path, site["reference"], version, city)

    st.title("⚡ Home energy")
    if daily.empty:
        st.info("No usage data yet. Click **Fetch new data** in the sidebar.")
        return

    tabs = st.tabs(["Overview", "Year over year", "Top days", "When we use energy", "Best hours ahead", "Lower the bill"])
    with tabs[0]:
        overview(daily, monthly, hourly, prices, today)
    with tabs[1]:
        year_over_year(monthly)
    with tabs[2]:
        top_days_tab(daily, hourly, today)
    with tabs[3]:
        patterns_tab(hourly, daily, today)
    with tabs[4]:
        upcoming_tab(prices)
    with tabs[5]:
        savings_tab(hourly, daily, monthly, prices, wx, city, today)
    st.caption(
        "Costs are the variable energy costs Frank Energie reports per hour or day (energy, markup, energy tax, VAT). "
        "Fixed monthly charges such as grid operator costs and the yearly energy tax refund are not included."
    )


def _kpis(daily, energies, start: date, end: date) -> None:
    cols = st.columns(len(energies))
    for col, energy in zip(cols, energies):
        r = an.period_vs_last_year(daily, energy, start, end)
        if not r:
            continue
        feed = energy == "feed_in"
        has_last = r["usage_last"] is not None
        col.metric(
            f"{ENERGY_LABEL[energy]} {'earned' if feed else 'cost'}",
            eur(abs(r["costs"])),
            f"{abs(r['costs']) - abs(r['costs_last']):+,.0f} € vs last year" if has_last else None,
            delta_color="normal" if feed else "inverse",
        )
        was = f" (last year {r['usage_last']:,.0f})" if has_last else ""
        col.caption(f"{r['usage']:,.0f} {UNIT[energy]}{was}")


def overview(daily, monthly, hourly, prices, today) -> None:
    energies = [e for e in ("electricity", "gas", "feed_in") if e in set(daily["energy"])]
    end = min(today - timedelta(1), max(daily["day"]))
    st.subheader(f"This month: 1–{end.day} {end:%B}, compared with the same days last year")
    _kpis(daily, energies, end.replace(day=1), end)
    st.subheader(f"This year: 1 January – {end:%d %B}, compared with the same days last year")
    _kpis(daily, energies, date(end.year, 1, 1), end)

    st.subheader("Worth knowing")
    for tip in highlights(hourly, prices, monthly, today):
        st.markdown(f"- {tip}")


def highlights(hourly, prices, monthly, today) -> list[str]:
    tips = []
    up = an.usage_with_prices(an.recent(hourly, 90, today), prices)
    if not up.empty:
        t = an.timing_by_month(up)
        share = (up.loc[up["band"] == "expensive", "usage"].sum() / up["usage"].sum())
        tips.append(
            f"In the last 3 months **{share:.0%}** of your electricity was used in each day's 6 most expensive hours "
            f"(an even spread would be 25%)."
        )
        s = an.shift_savings(up, 0.3)
        if s["per_year"] > 1:
            tips.append(f"Moving 30% of that expensive-hour use to the cheapest hours would save roughly **{eur(s['per_year'])} a year**.")
    bl = an.baseload(an.recent(hourly, 60, today), prices)
    if not bl.empty:
        w = bl["watts"].iloc[-1]
        tips.append(f"Your always-on use at night is about **{w:.0f} W**, around **{eur(bl['cost_year'].iloc[-1])} a year**.")
    peaks = an.peak_hours(an.recent(hourly, 90, today))
    if not peaks.empty:
        hrs = ", ".join(f"{h:02d}:00" for h in sorted(peaks["hour_of_day"]))
        tips.append(f"Your busiest hours (last 3 months) start at {hrs}.")
    return tips or ["Fetch more data to see highlights."]


def year_over_year(monthly) -> None:
    energies = [e for e in ("electricity", "gas", "feed_in") if e in set(monthly["energy"])]
    c1, c2 = st.columns(2)

    energy = c1.radio(
        "Energy",
        energies,
        format_func=ENERGY_LABEL.get,
        horizontal=True,
        key="yoy_energy",
    )

    metric = c2.radio("Show", ["usage", "costs"], format_func={"usage": "Consumption", "costs": "Cost"}.get, horizontal=True)
    table = an.yoy_table(monthly, energy, metric)
    if energy == "feed_in" and metric == "costs":
        table = table.abs()
    if table.empty:
        st.info("No data for this selection.")
        return
    y_title = "€" if metric == "costs" else UNIT[energy]
    st.plotly_chart(charts.yoy_bars(table, y_title, ",.0f"), width="stretch")
    shown = table.copy()
    shown.index = [charts.MONTHS[m - 1] for m in shown.index]
    shown.loc["Total"] = shown.sum()
    shown.columns = [str(c) for c in shown.columns]
    with st.expander("Table"):
        st.dataframe(shown.style.format("{:,.0f}", na_rep="–"), width="stretch")
    st.caption("The current month is still in progress.")


def top_days_tab(daily, hourly, today) -> None:
    period = st.radio("Period", ["Last 12 months", "Last 3 months", "All data"], horizontal=True)
    since = {"Last 12 months": today - timedelta(365), "Last 3 months": today - timedelta(91), "All data": None}[period]
    d = daily if since is None else daily[daily["day"] >= since]

    use = d[d["energy"] != "feed_in"].groupby("day", as_index=False)["costs"].sum()
    top_cost = use.nlargest(5, "costs")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Most expensive days** (electricity + gas)")
        st.dataframe(top_cost.rename(columns={"day": "Day", "costs": "Cost (€)"}).style.format({"Cost (€)": "{:.2f}"}),
                     hide_index=True, width="stretch")
    for col, energy in ((c2, "electricity"), (c3, "gas")):
        with col:
            t = an.top_days(d, energy, "usage")
            if t.empty:
                continue
            st.markdown(f"**Highest {ENERGY_LABEL[energy].lower()} use**")
            st.dataframe(t.rename(columns={"day": "Day", "usage": UNIT[energy], "costs": "€"}).style.format({UNIT[energy]: "{:.1f}", "€": "{:.2f}"}),
                         hide_index=True, width="stretch")

    candidates = sorted(set(top_cost["day"]) | set(an.top_days(d, "electricity", "usage")["day"]), reverse=True)
    detail_days = set(hourly["hour"].dt.date) if not hourly.empty else set()
    candidates = [c for c in candidates if c in detail_days]
    if candidates:
        st.markdown("**What happened that day?**")
        day = st.selectbox("Pick a day", candidates, format_func=lambda x: x.strftime("%a %d %b %Y"))
        h = hourly[(hourly["hour"].dt.date == day) & (hourly["energy"] == "electricity")]
        st.plotly_chart(charts.day_profile(h, "kWh"), width="stretch")


def patterns_tab(hourly, daily, today) -> None:
    days = st.select_slider("Look back", [30, 60, 91, 182, 365], value=91, format_func=lambda d: f"{d} days")
    h = an.recent(hourly, days, today)
    if h.empty:
        st.info("No hourly data in this period.")
        return
    
    energy = st.radio(
        "Energy",
        [e for e in ("electricity", "gas") if e in set(h["energy"])],
        format_func=ENERGY_LABEL.get,
        horizontal=True,
        key="patterns_energy",
    )

    peaks = an.peak_hours(h, energy)
    st.markdown("Peak hours: " + ", ".join(f"**{r.hour_of_day:02d}:00–{r.hour_of_day + 1:02d}:00** ({r.usage:.2f} {UNIT[energy]})" for r in peaks.itertuples()))
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("**Average use by hour of day**")
        prof = an.hour_profile(h, energy)
        st.plotly_chart(charts.hour_profile(prof).update_yaxes(title_text=f"Average {UNIT[energy]} in that hour"), width="stretch")
    with c2:
        st.markdown("**By weekday and hour**")
        st.plotly_chart(charts.heatmap(an.weekday_hour_heatmap(h, energy)), width="stretch")
    ww = an.weekday_vs_weekend(daily, energy, today - timedelta(days))
    if len(ww) == 2:
        wd, we = ww.set_index("daytype").loc["Weekday"], ww.set_index("daytype").loc["Weekend day"]
        st.markdown(
            f"An average weekday uses **{wd.usage:.1f} {UNIT[energy]}** ({eur(wd.costs, 2)}), "
            f"a weekend day **{we.usage:.1f} {UNIT[energy]}** ({eur(we.costs, 2)})."
        )


def upcoming_tab(prices) -> None:
    now = datetime.now(TZ)
    up = an.upcoming_prices(prices, now)
    if up.empty:
        st.info("No upcoming prices stored. Click **Fetch new data**; tomorrow's prices are usually published after 15:00.")
        return
    last_end = up["end"].max()
    if last_end.date() <= now.date():
        st.caption("Tomorrow's prices are not published yet (usually after 15:00). Fetch again later to see them.")
    current = up.iloc[0]
    cheapest = up.loc[up["price"].idxmin()]
    priciest = up.loc[up["price"].idxmax()]
    c = st.columns(3)
    c[0].metric("Price now", f"€ {current.price:.3f}/kWh")
    c[1].metric(f"Cheapest hour ahead: {cheapest.start:%a %H:%M}", f"€ {cheapest.price:.3f}/kWh")
    c[2].metric(f"Most expensive ahead: {priciest.start:%a %H:%M}", f"€ {priciest.price:.3f}/kWh")

    windows = an.best_windows(up, [1, 2, 3, 4])
    st.markdown("**Best time to start a longer job** (washing machine ≈ 2 h, dishwasher ≈ 3 h, EV charge ≈ 4 h)")
    pick = st.radio("Highlight", list(windows["hours"]), format_func=lambda h: f"{h:g} h", horizontal=True, index=min(1, len(windows) - 1))
    w = windows[windows["hours"] == pick].iloc[0]
    st.plotly_chart(charts.upcoming_prices(up, (w.start, w.end), now), width="stretch")
    table = windows.assign(
        Duration=windows["hours"].map(lambda h: f"{h:g} h"),
        Start=windows["start"].dt.strftime("%a %H:%M"),
        End=windows["end"].dt.strftime("%a %H:%M"),
        **{"Avg price (€/kWh)": windows["avg_price"].round(3), "vs average": windows["vs_avg"].map(lambda v: f"{v:+.3f}")},
    )[["Duration", "Start", "End", "Avg price (€/kWh)", "vs average"]]
    st.dataframe(table, hide_index=True, width="stretch")
    gas = an.upcoming_prices(prices, now, "gas")
    if not gas.empty:
        st.caption(f"Gas: € {gas['price'].iloc[0]:.3f}/m³ now. Gas prices are fixed per day, so timing does not matter for gas.")


def savings_tab(hourly, daily, monthly, prices, wx, city, today) -> None:
    up = an.usage_with_prices(an.recent(hourly, 365, today), prices)

    st.subheader("1. Do you use electricity when it is cheap?")
    if up.empty:
        st.info("Needs hourly usage and price history. Fetch data first.")
    else:
        t = an.timing_by_month(up)
        st.plotly_chart(
            charts.lines(t, "month", {"your_price": "What you paid per kWh", "avg_price": "Average price that month"}, "€ per kWh", ".3f"),
            width="stretch",
        )
        total = t["timing_effect"].sum()
        verdict = "saved" if total < 0 else "cost"
        st.markdown(
            f"Compared with spreading the same use evenly over the day, your timing **{verdict} {eur(abs(total))}** "
            f"since {pd.Timestamp(t['month'].iloc[0]):%B %Y}. Each day, the 6 most expensive hours took **{t['share_expensive'].mean():.0%}** "
            f"of your use and the 6 cheapest **{t['share_cheap'].mean():.0%}** (25% each would be neutral)."
        )

        st.subheader("2. What if you shifted flexible use?")
        share = st.slider("Share of use in the expensive hours that could move (dishwasher, washing, dryer, EV, boiler)", 0, 60, 30, 5, format="%d%%") / 100
        s = an.shift_savings(up, share)
        c = st.columns(3)
        c[0].metric("Energy moved", f"{s['moved_kwh']:,.0f} kWh")
        c[1].metric(f"Saved over {s['days']} days", eur(s["savings"]))
        c[2].metric("Per year", eur(s["per_year"]))
        st.caption("Assumes the moved energy runs in the same day's 6 cheapest hours at their average price.")

    st.subheader("3. Always-on use")
    bl = an.baseload(hourly, prices)
    if not bl.empty:
        st.plotly_chart(charts.bars(bl.tail(18), "month", "watts", "Watts, lowest night hour", ".0f"), width="stretch")
        last = bl.iloc[-1]
        st.markdown(
            f"About **{last.watts:.0f} W** runs all the time (fridge, router, standby, ventilation, pumps). That is "
            f"**{last.kwh_year:,.0f} kWh** or **{eur(last.cost_year)}** a year. Every 10 W less saves about "
            f"{eur(10 / 1000 * 8760 * (last.cost_year / last.kwh_year if last.kwh_year else 0))} a year. "
            "Smart plugs on the TV corner and a check of old fridges or freezers are the usual wins."
        )

    st.subheader("4. Gas use and outdoor temperature")
    if not city:
        st.info("Pick your nearest city under Settings to compare gas use with the weather.")
    else:
        df, fit = an.gas_vs_temperature(daily, wx)
        if fit is None:
            st.info("Not enough gas and weather data yet.")
        else:
            st.plotly_chart(charts.gas_scatter(df, fit), width="stretch")
            seasons = fit["seasons"].rename(columns={"season": "Heating season", "m3_per_hdd": "m³ per degree day"})
            st.markdown(
                f"Below 18 °C every degree colder costs about **{fit['m3_per_hdd']:.2f} m³ a day**; "
                f"hot water and cooking use about **{max(fit['base_m3_per_day'], 0):.1f} m³ a day**. "
                "A falling m³ per degree day between seasons means insulation or thermostat changes are working."
            )
            st.dataframe(seasons.style.format({"m³ per degree day": "{:.3f}"}), hide_index=True)

    fi = an.feed_in_summary(monthly)
    if not fi.empty and fi["usage_fed"].sum() > 0:
        st.subheader("5. Solar feed-in")
        st.plotly_chart(charts.bars(fi.tail(18), "month_label", "usage_fed", "kWh fed back", ",.0f", charts.SERIES[3]), width="stretch")
        recent = fi.tail(12)
        fed_price = recent["earned"].sum() / max(recent["usage_fed"].sum(), 1e-9)
        buy_price = (recent["eur_per_kwh_used"]).mean()
        neg = an.negative_price_feed_in(an.recent(hourly, 365, today), prices)
        st.markdown(
            f"Last 12 months: **{recent['usage_fed'].sum():,.0f} kWh** fed back for **{eur(recent['earned'].sum())}**, "
            f"about **€ {fed_price:.3f}/kWh**, while you pay about **€ {buy_price:.3f}/kWh** for what you use. "
            "Using your own solar power (running appliances at midday) is worth more than feeding it back."
        )
        if neg["hours"]:
            st.markdown(f"You fed back **{neg['kwh']:.0f} kWh** in **{neg['hours']} hours** with a negative market price. "
                        "Frank's smart feed-in or an inverter limit can avoid that.")


def main() -> None:
    if "account" not in st.session_state:
        login_screen()
    else:
        dashboard()


if __name__ == "__main__":
    main()

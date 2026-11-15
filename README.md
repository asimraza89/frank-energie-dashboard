# Home energy dashboard for Frank Energie customers

A small dashboard on top of your own Frank Energie data: year-over-year use and cost, your most expensive days, when in the day you use energy, the cheapest hours coming up, and a few analyses that show where you can save.

It uses the unofficial [python-frank-energie](https://github.com/HiDiHo01/python-frank-energie) package to talk to Frank Energie. It is not made by or affiliated with Frank Energie.

## Privacy: everybody runs their own copy

There is no shared server. Each household runs the app on its own computer:

* You sign in with your own Frank Energie email and password. The password is sent only to Frank Energie and is never saved.
* If you tick "Keep me signed in", only a login token is stored, in `~/.frank-dashboard/<account>/tokens.json` (readable by your user only).
* Your energy data is cached in `~/.frank-dashboard/<account>/data.sqlite` on your computer. Nothing is stored in this folder or the repository.
* The app listens on `127.0.0.1` only, so other devices on your network cannot open it.
* Sign out removes the token. Tick "Also delete my cached data" to remove everything.

To share it with a neighbour, share the code (this repository), not your running app. They run it on their own computer and sign in with their own account.

## Running it

You need Python 3.12 or newer.

* macOS / Linux: `./run.sh`
* Windows: double-click `run.bat`

The first start installs the dependencies into a local `.venv` folder (a minute or two). The dashboard then opens in your browser at http://localhost:8501. You can also click "Try it with demo data" to look around without an account.

## Fetching only what is new

The first sync fetches your history: one request per month for daily totals, and one request per day for hourly detail (last 400 days by default, adjustable under Settings). This takes a few minutes once. After that:

* Days and months that can no longer change are marked final and are never fetched again.
* Only the last few days (meter data can arrive late), the current and previous month, and today's and tomorrow's prices are fetched again. A normal daily sync is a handful of requests.
* If a sync is interrupted, everything fetched so far is kept and the next sync continues where it stopped.

The exact rules are at the top of [`frankdash/sync.py`](frankdash/sync.py).

## What is on the dashboard

| Tab | What it shows |
|---|---|
| Overview | This month and this year so far versus the same days last year, plus a few highlights |
| Year over year | Monthly electricity, gas and solar feed-in, use or cost, one bar per year |
| Top days | The 5 most expensive days and the 5 days with the highest electricity and gas use, with the hourly profile of a chosen day |
| When we use energy | Average use by hour (weekday vs weekend), a weekday × hour heatmap and your peak hours, for the last 30 days to 1 year |
| Best hours ahead | Upcoming all-in prices (tomorrow's appear after about 15:00) and the cheapest 1, 2, 3 and 4 hour blocks to run the washing machine, dishwasher or EV |
| Lower the bill | See below |

### Lower the bill

1. **Timing.** Your usage-weighted price per kWh against the plain average price each month, and the share of your use that falls in each day's 6 most expensive hours. This tells you whether your timing costs or saves you money on a dynamic contract.
2. **Shift simulator.** Move a chosen share of the use in expensive hours to the same day's cheapest hours and see the saving per year.
3. **Always-on use.** The lowest hourly use between midnight and 05:00, in watts, and what that costs per year.
4. **Gas and outdoor temperature.** Daily gas use against the daily mean temperature (from [Open-Meteo](https://open-meteo.com), using the coordinates of the city you pick, never your address). It estimates m³ per degree day and the base use for hot water and cooking, per heating season, so you can see whether insulation or thermostat changes work.
5. **Solar feed-in.** What you fed back, what you earned per kWh compared with what you pay, and how much you fed back while the market price was negative.

Costs are the variable costs Frank Energie reports per slot (energy, markup, energy tax, VAT). Fixed charges such as grid operator costs and the yearly energy tax refund are not included, so the totals are lower than your invoices.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests and the demo mode use `frankdash/mock.py`, which replaces only the network call of the real `FrankEnergie` client with synthetic responses, so the package's own parsing still runs.

```
app.py               Streamlit UI
frankdash/client.py  wrapper around python-frank-energie
frankdash/sync.py    incremental sync planner and runner
frankdash/storage.py SQLite cache
frankdash/analysis.py all calculations (pure pandas)
frankdash/charts.py  Plotly figures
frankdash/weather.py Open-Meteo daily temperatures
frankdash/mock.py    demo data
```

The python-frank-energie package is unofficial and changes often. If Frank Energie changes its API, updating the package (`.venv/bin/pip install -U python-frank-energie`) is usually the fix.

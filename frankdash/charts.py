"""Plotly figures with one consistent look."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# Categorical slots in fixed order (validated palette).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MUTED = "#a3a29c"
GOOD = "#1baf7a"
BAD = "#e34948"
SEQ_BLUE = [[0, "#cde2fb"], [0.5, "#5598e7"], [1, "#0d366b"]]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _layout(fig: go.Figure, y_title: str = "", height: int = 380) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=24, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
        bargap=0.25,
        bargroupgap=0.08,
    )
    fig.update_yaxes(title_text=y_title, gridcolor="rgba(128,128,128,0.15)", zeroline=False)
    fig.update_xaxes(showgrid=False)
    return fig


def yoy_bars(table: pd.DataFrame, y_title: str, fmt: str) -> go.Figure:
    """Months on x, one bar per year. Colour follows the year, oldest first."""
    fig = go.Figure()
    years = sorted(table.columns)
    for i, year in enumerate(years):
        fig.add_bar(
            x=[MONTHS[m - 1] for m in table.index],
            y=table[year],
            name=str(year),
            marker_color=SERIES[i % len(SERIES)] if len(years) <= 4 else None,
            marker_line_width=0,
            hovertemplate=f"{year}: %{{y:{fmt}}}<extra></extra>",
        )
    fig.update_layout(barmode="group")
    return _layout(fig, y_title)


def day_profile(df: pd.DataFrame, y_title: str) -> go.Figure:
    fig = go.Figure()
    for i, (energy, part) in enumerate(df.groupby("energy", sort=False)):
        fig.add_bar(x=part["hour"].dt.strftime("%H:%M"), y=part["usage"], name=energy, marker_color=SERIES[i])
    return _layout(fig, y_title, 300)


def hour_profile(prof: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    order = ["All days", "Weekday", "Weekend"]
    for i, daytype in enumerate(order):
        part = prof[prof["daytype"] == daytype]
        if part.empty:
            continue
        fig.add_scatter(
            x=part["hour_of_day"],
            y=part["usage"],
            name=daytype,
            mode="lines+markers",
            line=dict(width=2, color=SERIES[i], dash="solid" if i == 0 else "dot"),
            marker=dict(size=6),
            hovertemplate=f"{daytype}: %{{y:.2f}} kWh<extra></extra>",
        )
    fig.update_xaxes(tickmode="array", tickvals=list(range(0, 24, 3)), ticktext=[f"{h:02d}:00" for h in range(0, 24, 3)])
    return _layout(fig, "Average kWh in that hour")


def heatmap(grid: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Heatmap(
            z=grid.values,
            x=[f"{h:02d}" for h in grid.columns],
            y=list(grid.index),
            colorscale=SEQ_BLUE,
            xgap=2,
            ygap=2,
            colorbar=dict(title="kWh"),
            hovertemplate="%{y} %{x}:00 · %{z:.2f} kWh<extra></extra>",
        )
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(hovermode="closest")
    return _layout(fig, "", 300)


def upcoming_prices(up: pd.DataFrame, highlight: tuple | None, now) -> go.Figure:
    colors = [MUTED] * len(up)
    if highlight is not None:
        start, end = highlight
        colors = [SERIES[0] if (s >= start and e <= end) else MUTED for s, e in zip(up["start"], up["end"])]
    fig = go.Figure(
        go.Bar(
            x=up["start"],
            y=up["price"],
            marker_color=colors,
            marker_line_width=0,
            customdata=up["end"].dt.strftime("%H:%M"),
            hovertemplate="%{x|%a %H:%M}–%{customdata}: € %{y:.3f}/kWh<extra></extra>",
        )
    )
    fig.add_hline(y=up["price"].mean(), line_dash="dot", line_color=MUTED, annotation_text="average", annotation_position="top left")
    fig.update_xaxes(tickformat="%a %H:%M")
    fig.update_layout(hovermode="closest")
    return _layout(fig, "€ per kWh, all-in")


def lines(df: pd.DataFrame, x: str, series: dict[str, str], y_title: str, fmt: str) -> go.Figure:
    fig = go.Figure()
    for i, (col, label) in enumerate(series.items()):
        fig.add_scatter(
            x=df[x], y=df[col], name=label, mode="lines+markers",
            line=dict(width=2, color=SERIES[i]), marker=dict(size=7),
            hovertemplate=f"{label}: %{{y:{fmt}}}<extra></extra>",
        )
    return _layout(fig, y_title)


def bars(df: pd.DataFrame, x: str, y: str, y_title: str, fmt: str, color: str = SERIES[0]) -> go.Figure:
    fig = go.Figure(go.Bar(x=df[x], y=df[y], marker_color=color, marker_line_width=0, hovertemplate=f"%{{x}}: %{{y:{fmt}}}<extra></extra>"))
    fig.update_layout(hovermode="closest")
    return _layout(fig, y_title)


def gas_scatter(df: pd.DataFrame, fit: dict) -> go.Figure:
    fig = go.Figure()
    seasons = sorted(df["season"].unique())[-4:]
    for i, season in enumerate(seasons):
        part = df[df["season"] == season]
        fig.add_scatter(
            x=part["tmean"], y=part["usage"], mode="markers", name=f"Season {season}",
            marker=dict(size=8, color=SERIES[i], opacity=0.75, line=dict(width=1, color="white")),
            customdata=part["day"].astype(str),
            hovertemplate="%{customdata}: %{x:.1f} °C, %{y:.2f} m³<extra></extra>",
        )
    xs = pd.Series(sorted(df["tmean"]))
    ys = (fit["base_m3_per_day"] + fit["m3_per_hdd"] * (18 - xs).clip(lower=0))
    fig.add_scatter(x=xs, y=ys, mode="lines", name="Typical", line=dict(color=MUTED, width=2, dash="dash"), hoverinfo="skip")
    fig.update_xaxes(title_text="Average outdoor temperature (°C)")
    fig.update_layout(hovermode="closest")
    return _layout(fig, "Gas per day (m³)", 420)

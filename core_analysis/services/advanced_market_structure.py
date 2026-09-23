from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

try:
    from sklearn.cluster import DBSCAN
except Exception:  # pragma: no cover - optional dependency fallback
    DBSCAN = None


@dataclass(frozen=True)
class MarketStructureConfig:
    fractal_window: int = 5
    density_tolerance_pct: float = 0.012
    volume_quantile: float = 0.80
    volume_profile_bins: int = 24
    value_area_pct: float = 0.70
    chart_tail: int = 90


class AdvancedMarketStructureAnalyzer:
    def __init__(self, config: MarketStructureConfig | None = None):
        self.config = config or MarketStructureConfig()

    def analyze(self, df: pd.DataFrame, symbol: str = "") -> dict[str, Any]:
        data = self._prepare_ohlcv(df)
        if data.empty or len(data) < max(20, self.config.fractal_window * 3):
            return {
                "error": f"Advanced market structure needs at least {max(20, self.config.fractal_window * 3)} OHLCV rows."
            }

        pivots = self._detect_fractals(data)
        structure_events = self._classify_structure(data, pivots)
        density_zones = self._cluster_density_zones(data, pivots)
        trendlines = self._detect_trendlines(data, pivots)
        liquidity_sweeps = self._detect_liquidity_sweeps(data, pivots, trendlines)
        structure_liquidity_rows = self._structure_liquidity_display_rows(structure_events, liquidity_sweeps)
        baselines = self._calculate_baselines(data)
        profile = self._volume_profile(data)
        predictions = self._predict_zone_outcomes(data, density_zones, baselines, profile)
        chart = self._build_chart_payload(
            data=data,
            pivots=pivots,
            zones=density_zones,
            trendlines=trendlines,
            sweeps=liquidity_sweeps,
            baselines=baselines,
            profile=profile,
        )

        return {
            "symbol": symbol,
            "rows_used": int(len(data)),
            "latest_date": self._format_date(data["business_date"].iloc[-1]),
            "latest_close": round(float(data["close"].iloc[-1]), 2),
            "fractal_window": self.config.fractal_window,
            "pivot_count": int(len(pivots)),
            "swing_high_count": int((pivots["pivot_type"] == "swing_high").sum()) if not pivots.empty else 0,
            "swing_low_count": int((pivots["pivot_type"] == "swing_low").sum()) if not pivots.empty else 0,
            "structure_events": structure_events,
            "structure_liquidity_rows": structure_liquidity_rows,
            "density_zones": density_zones,
            "trendlines": trendlines,
            "liquidity_sweeps": liquidity_sweeps,
            "baselines": baselines,
            "profile": profile,
            "predictions": predictions,
            "chart": chart,
            "ml_model": "Deterministic structural heuristic",
            "warnings": self._build_warnings(data, density_zones),
        }

    def _prepare_ohlcv(self, df: pd.DataFrame) -> pd.DataFrame:
        column_map = {
            "business_date": "business_date",
            "open_price_adj": "open",
            "high_price_adj": "high",
            "low_price_adj": "low",
            "close_price_adj": "close",
            "volume": "volume",
        }
        work = df.rename(columns={source: target for source, target in column_map.items() if source in df.columns}).copy()
        required = ["business_date", "open", "high", "low", "close"]
        if not set(required).issubset(work.columns):
            return pd.DataFrame()
        if "volume" not in work.columns:
            work["volume"] = np.nan

        work["business_date"] = pd.to_datetime(work["business_date"])
        for column in ["open", "high", "low", "close", "volume"]:
            work[column] = pd.to_numeric(work[column], errors="coerce")
        work = (
            work.dropna(subset=["business_date", "open", "high", "low", "close"])
            .sort_values("business_date")
            .reset_index(drop=True)
        )
        if work["volume"].isna().all():
            work["volume"] = 0.0
        else:
            work["volume"] = work["volume"].fillna(work["volume"].median())
        work["bar_index"] = np.arange(len(work))
        work["typical_price"] = (work["high"] + work["low"] + work["close"]) / 3
        work["true_range"] = self._true_range(work)
        work["atr"] = work["true_range"].rolling(14, min_periods=1).mean()
        return work

    def _detect_fractals(self, data: pd.DataFrame) -> pd.DataFrame:
        window = max(3, int(self.config.fractal_window))
        if window % 2 == 0:
            window += 1
        wing = window // 2
        pivots = []

        for index in range(wing, len(data) - wing):
            slice_df = data.iloc[index - wing:index + wing + 1]
            row = data.iloc[index]
            is_high = row["high"] == slice_df["high"].max() and (slice_df["high"] == row["high"]).sum() == 1
            is_low = row["low"] == slice_df["low"].min() and (slice_df["low"] == row["low"]).sum() == 1
            if is_high:
                pivots.append(self._pivot_row(row, "swing_high", row["high"]))
            if is_low:
                pivots.append(self._pivot_row(row, "swing_low", row["low"]))

        return pd.DataFrame(pivots).sort_values("index").reset_index(drop=True) if pivots else pd.DataFrame(
            columns=["index", "date", "pivot_type", "price", "volume"]
        )

    def _pivot_row(self, row: pd.Series, pivot_type: str, price: float) -> dict[str, Any]:
        return {
            "index": int(row["bar_index"]),
            "date": self._format_date(row["business_date"]),
            "pivot_type": pivot_type,
            "price": round(float(price), 2),
            "volume": float(row["volume"]),
        }

    def _classify_structure(self, data: pd.DataFrame, pivots: pd.DataFrame) -> list[dict[str, Any]]:
        if pivots.empty:
            return []

        events = []
        last_swing_high = None
        last_swing_low = None
        trend = "neutral"
        pivot_by_index = pivots.groupby("index")

        for _, row in data.iterrows():
            index = int(row["bar_index"])
            close = float(row["close"])

            if last_swing_high is not None and close > last_swing_high["price"]:
                event_type = "BOS" if trend in {"bullish", "neutral"} else "CHoCH"
                events.append({
                    "date": self._format_date(row["business_date"]),
                    "event": event_type,
                    "direction": "Bullish",
                    "level": round(float(last_swing_high["price"]), 2),
                    "close": round(close, 2),
                })
                trend = "bullish"
                last_swing_high = None

            if last_swing_low is not None and close < last_swing_low["price"]:
                event_type = "BOS" if trend in {"bearish", "neutral"} else "CHoCH"
                events.append({
                    "date": self._format_date(row["business_date"]),
                    "event": event_type,
                    "direction": "Bearish",
                    "level": round(float(last_swing_low["price"]), 2),
                    "close": round(close, 2),
                })
                trend = "bearish"
                last_swing_low = None

            if index in pivot_by_index.groups:
                for _, pivot in pivot_by_index.get_group(index).iterrows():
                    if pivot["pivot_type"] == "swing_high":
                        last_swing_high = pivot
                    elif pivot["pivot_type"] == "swing_low":
                        last_swing_low = pivot

        return events[-25:]

    def _cluster_density_zones(self, data: pd.DataFrame, pivots: pd.DataFrame) -> list[dict[str, Any]]:
        high_volume = data[data["volume"] >= data["volume"].quantile(self.config.volume_quantile)]
        pivot_points = []
        if not pivots.empty:
            for _, pivot in pivots.iterrows():
                pivot_points.append({
                    "price": float(pivot["price"]),
                    "volume": float(pivot["volume"]),
                    "source": pivot["pivot_type"],
                    "date": pivot["date"],
                })
        for _, row in high_volume.iterrows():
            pivot_points.append({
                "price": float(row["typical_price"]),
                "volume": float(row["volume"]),
                "source": "high_volume_node",
                "date": self._format_date(row["business_date"]),
            })
        if not pivot_points:
            return []

        points = pd.DataFrame(pivot_points).sort_values("price").reset_index(drop=True)
        if DBSCAN is not None and len(points) >= 4:
            eps = max(float(data["close"].iloc[-1]) * self.config.density_tolerance_pct, float(data["atr"].median()) * 0.5)
            labels = DBSCAN(eps=eps, min_samples=2).fit(points[["price"]]).labels_
            points["cluster"] = labels
            grouped = [cluster_df for label, cluster_df in points.groupby("cluster") if label != -1]
        else:
            grouped = self._fallback_price_clusters(points, data)

        zones = []
        max_volume = max(float(points["volume"].sum()), 1.0)
        latest_close = float(data["close"].iloc[-1])
        for order, cluster_df in enumerate(grouped, start=1):
            if len(cluster_df) < 2:
                continue
            low = float(cluster_df["price"].min())
            high = float(cluster_df["price"].max())
            center = float(np.average(cluster_df["price"], weights=np.maximum(cluster_df["volume"], 1.0)))
            touches = int(len(cluster_df))
            volume_share = float(cluster_df["volume"].sum()) / max_volume
            strength = round((touches * 10) + (volume_share * 100), 2)
            zone_type = "Liquidity Pool"
            if center > latest_close:
                zone_type = "Supply / Resistance"
            elif center < latest_close:
                zone_type = "Demand / Support"
            zones.append({
                "rank": order,
                "type": zone_type,
                "low": round(low, 2),
                "high": round(high, 2),
                "center": round(center, 2),
                "touches": touches,
                "volume_density": round(volume_share * 100, 2),
                "strength": strength,
                "sources": ", ".join(sorted(set(cluster_df["source"].astype(str)))),
                "prediction": "",
                "hold_probability": None,
            })

        zones.sort(key=lambda zone: zone["strength"], reverse=True)
        for rank, zone in enumerate(zones[:10], start=1):
            zone["rank"] = rank
        return zones[:10]

    def _fallback_price_clusters(self, points: pd.DataFrame, data: pd.DataFrame) -> list[pd.DataFrame]:
        tolerance = max(float(data["close"].iloc[-1]) * self.config.density_tolerance_pct, float(data["atr"].median()) * 0.5)
        clusters = []
        current = []
        for _, point in points.iterrows():
            if not current:
                current = [point]
                continue
            current_center = float(np.mean([item["price"] for item in current]))
            if abs(float(point["price"]) - current_center) <= tolerance:
                current.append(point)
            else:
                if len(current) >= 2:
                    clusters.append(pd.DataFrame(current))
                current = [point]
        if len(current) >= 2:
            clusters.append(pd.DataFrame(current))
        return clusters

    def _detect_trendlines(self, data: pd.DataFrame, pivots: pd.DataFrame) -> list[dict[str, Any]]:
        trendlines = []
        if pivots.empty:
            return trendlines

        highs = pivots[pivots["pivot_type"] == "swing_high"].tail(8)
        lows = pivots[pivots["pivot_type"] == "swing_low"].tail(8)
        descending = self._trendline_from_pivots(highs, "Descending liquidity trendline", require_descending=True)
        ascending = self._trendline_from_pivots(lows, "Ascending liquidity trendline", require_ascending=True)
        for line in (ascending, descending):
            if line:
                line["latest_value"] = round(self._line_value(line, len(data) - 1), 2)
                trendlines.append(line)
        return trendlines

    def _trendline_from_pivots(
        self,
        pivots: pd.DataFrame,
        label: str,
        require_ascending: bool = False,
        require_descending: bool = False,
    ) -> dict[str, Any] | None:
        if len(pivots) < 2:
            return None
        rows = pivots.tail(2).to_dict("records")
        first, second = rows[0], rows[1]
        if require_ascending and second["price"] <= first["price"]:
            return None
        if require_descending and second["price"] >= first["price"]:
            return None
        x1 = int(first["index"])
        x2 = int(second["index"])
        if x2 == x1:
            return None
        y1 = float(first["price"])
        y2 = float(second["price"])
        slope = (y2 - y1) / (x2 - x1)
        return {
            "label": label,
            "start_index": x1,
            "end_index": x2,
            "start_date": first["date"],
            "end_date": second["date"],
            "start_price": round(y1, 2),
            "end_price": round(y2, 2),
            "slope": round(slope, 6),
            "direction": "Ascending" if slope > 0 else "Descending",
        }

    def _detect_liquidity_sweeps(
        self,
        data: pd.DataFrame,
        pivots: pd.DataFrame,
        trendlines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if pivots.empty:
            return []
        sweeps = []
        high_pivots = pivots[pivots["pivot_type"] == "swing_high"]
        low_pivots = pivots[pivots["pivot_type"] == "swing_low"]

        for _, row in data.iterrows():
            index = int(row["bar_index"])
            prior_highs = high_pivots[high_pivots["index"] < index]
            prior_lows = low_pivots[low_pivots["index"] < index]
            if not prior_highs.empty:
                level = float(prior_highs.iloc[-1]["price"])
                if row["high"] > level and row["close"] < level:
                    sweeps.append(self._sweep_row(row, "Buy-side liquidity sweep", level))
            if not prior_lows.empty:
                level = float(prior_lows.iloc[-1]["price"])
                if row["low"] < level and row["close"] > level:
                    sweeps.append(self._sweep_row(row, "Sell-side liquidity sweep", level))

            for line in trendlines:
                if index <= line["end_index"]:
                    continue
                line_value = self._line_value(line, index)
                if line["direction"] == "Ascending" and row["low"] < line_value < row["close"]:
                    sweeps.append(self._sweep_row(row, "Ascending trendline sweep", line_value))
                elif line["direction"] == "Descending" and row["high"] > line_value > row["close"]:
                    sweeps.append(self._sweep_row(row, "Descending trendline sweep", line_value))

        return sweeps[-20:]

    def _sweep_row(self, row: pd.Series, sweep_type: str, level: float) -> dict[str, Any]:
        return {
            "date": self._format_date(row["business_date"]),
            "type": sweep_type,
            "level": round(float(level), 2),
            "close": round(float(row["close"]), 2),
        }

    def _structure_liquidity_display_rows(
        self,
        structure_events: list[dict[str, Any]],
        liquidity_sweeps: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows = []
        for event in structure_events:
            direction = str(event.get("direction") or "")
            rows.append({
                "date": event.get("date", ""),
                "signal": f"{direction} {event.get('event', '')}".strip(),
                "signal_class": "text-success-custom" if direction == "Bullish" else "text-danger-custom",
                "level": event.get("level"),
                "close": event.get("close"),
            })
        for sweep in liquidity_sweeps:
            rows.append({
                "date": sweep.get("date", ""),
                "signal": sweep.get("type", ""),
                "signal_class": "text-warning",
                "level": sweep.get("level"),
                "close": sweep.get("close"),
            })
        return sorted(rows, key=lambda row: row.get("date") or "", reverse=True)

    def _calculate_baselines(self, data: pd.DataFrame) -> dict[str, Any]:
        volume = data["volume"].replace(0, np.nan)
        typical = data["typical_price"]
        cumulative_volume = volume.fillna(0).cumsum()
        cumulative_vwap = (typical * volume.fillna(0)).cumsum() / cumulative_volume.replace(0, np.nan)
        hma = self._hma(data["close"], 21)
        rolling_std = data["close"].rolling(20, min_periods=5).std()
        latest_vwap = cumulative_vwap.iloc[-1]
        latest_hma = hma.iloc[-1]
        latest_std = rolling_std.iloc[-1]
        return {
            "series": {
                "vwap": self._series_payload(data, cumulative_vwap),
                "hma": self._series_payload(data, hma),
                "upper_band": self._series_payload(data, cumulative_vwap + (2 * rolling_std)),
                "lower_band": self._series_payload(data, cumulative_vwap - (2 * rolling_std)),
            },
            "latest_vwap": round(float(latest_vwap), 2) if pd.notna(latest_vwap) else None,
            "latest_hma": round(float(latest_hma), 2) if pd.notna(latest_hma) else None,
            "latest_upper_band": round(float(latest_vwap + (2 * latest_std)), 2) if pd.notna(latest_vwap) and pd.notna(latest_std) else None,
            "latest_lower_band": round(float(latest_vwap - (2 * latest_std)), 2) if pd.notna(latest_vwap) and pd.notna(latest_std) else None,
            "premium_discount": "Premium" if pd.notna(latest_vwap) and data["close"].iloc[-1] > latest_vwap else "Discount",
        }

    def _volume_profile(self, data: pd.DataFrame) -> dict[str, Any]:
        prices = data["typical_price"].to_numpy(dtype=float)
        volumes = data["volume"].to_numpy(dtype=float)
        min_price = float(np.nanmin(data["low"]))
        max_price = float(np.nanmax(data["high"]))
        if min_price == max_price:
            return {"poc": round(min_price, 2), "vah": round(max_price, 2), "val": round(min_price, 2), "bins": []}
        hist, edges = np.histogram(prices, bins=self.config.volume_profile_bins, range=(min_price, max_price), weights=volumes)
        centers = (edges[:-1] + edges[1:]) / 2
        poc_index = int(np.argmax(hist))
        total_volume = float(hist.sum())
        if total_volume <= 0:
            return {"poc": round(float(centers[poc_index]), 2), "vah": None, "val": None, "bins": []}

        selected = {poc_index}
        selected_volume = float(hist[poc_index])
        left = poc_index - 1
        right = poc_index + 1
        while selected_volume / total_volume < self.config.value_area_pct and (left >= 0 or right < len(hist)):
            left_volume = hist[left] if left >= 0 else -1
            right_volume = hist[right] if right < len(hist) else -1
            if right_volume >= left_volume:
                selected.add(right)
                selected_volume += float(right_volume)
                right += 1
            else:
                selected.add(left)
                selected_volume += float(left_volume)
                left -= 1

        selected_centers = centers[sorted(selected)]
        bins = [
            {"price": round(float(center), 2), "volume": round(float(volume), 2)}
            for center, volume in zip(centers, hist)
        ]
        return {
            "poc": round(float(centers[poc_index]), 2),
            "vah": round(float(selected_centers.max()), 2),
            "val": round(float(selected_centers.min()), 2),
            "bins": bins,
        }

    def _predict_zone_outcomes(
        self,
        data: pd.DataFrame,
        zones: list[dict[str, Any]],
        baselines: dict[str, Any],
        profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not zones:
            return []

        latest_close = float(data["close"].iloc[-1])
        latest_atr = max(float(data["atr"].iloc[-1]), 0.01)
        latest_vwap = baselines.get("latest_vwap") or latest_close
        poc = profile.get("poc") or latest_close
        features = []
        for zone in zones:
            distance_atr = abs(float(zone["center"]) - latest_close) / latest_atr
            features.append([
                float(zone["strength"]),
                float(zone["touches"]),
                float(zone["volume_density"]),
                distance_atr,
                abs(float(zone["center"]) - latest_vwap) / latest_atr,
                abs(float(zone["center"]) - poc) / latest_atr,
            ])

        # The previous RandomForest path trained on labels derived from the same
        # feature medians it then predicted on (circular self-labelling), so its
        # "hold probability" was effectively noise that jumped between near-
        # identical zones. We use a deterministic, monotonic structural heuristic
        # so the same structure always yields the same, explainable probability.
        probabilities = self._heuristic_hold_probabilities(np.asarray(features, dtype=float))
        model_name = "Deterministic structural heuristic"

        predictions = []
        for zone, probability in zip(zones, probabilities):
            label = "Likely Hold" if probability >= 0.55 else "Break Risk"
            zone["prediction"] = label
            zone["hold_probability"] = round(float(probability) * 100, 2)
            predictions.append({
                "zone": f"{zone['low']:.2f} - {zone['high']:.2f}",
                "center": zone["center"],
                "prediction": label,
                "hold_probability": round(float(probability) * 100, 2),
                "model": model_name,
            })
        return predictions

    def _heuristic_hold_probabilities(self, features: np.ndarray) -> np.ndarray:
        if features.size == 0:
            return np.asarray([])
        strength = features[:, 0]
        touches = features[:, 1]
        volume_density = features[:, 2]
        distance_atr = features[:, 3]
        score = (
            0.35 * self._minmax(strength) +
            0.20 * self._minmax(touches) +
            0.20 * self._minmax(volume_density) +
            0.25 * (1 - self._minmax(distance_atr))
        )
        return np.clip(0.25 + (score * 0.65), 0.05, 0.95)

    def _build_chart_payload(
        self,
        data: pd.DataFrame,
        pivots: pd.DataFrame,
        zones: list[dict[str, Any]],
        trendlines: list[dict[str, Any]],
        sweeps: list[dict[str, Any]],
        baselines: dict[str, Any],
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        tail = data.tail(min(self.config.chart_tail, len(data))).copy()
        start_index = int(tail["bar_index"].iloc[0])
        end_index = int(tail["bar_index"].iloc[-1])
        candles = [
            {
                "index": int(row["bar_index"]),
                "date": self._format_date(row["business_date"]),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": round(float(row["volume"]), 2),
            }
            for _, row in tail.iterrows()
        ]
        pivot_rows = []
        if not pivots.empty:
            pivot_rows = [
                row
                for row in pivots.to_dict("records")
                if start_index <= int(row["index"]) <= end_index
            ]
        visible_sweeps = [
            sweep for sweep in sweeps if tail["business_date"].min() <= pd.to_datetime(sweep["date"]) <= tail["business_date"].max()
        ]
        visible_series = {
            key: [point for point in values if start_index <= int(point["index"]) <= end_index]
            for key, values in baselines.get("series", {}).items()
        }
        return {
            "candles": candles,
            "pivots": pivot_rows,
            "zones": zones[:8],
            "trendlines": trendlines,
            "sweeps": visible_sweeps,
            "baselines": visible_series,
            "profile": profile,
        }

    def _series_payload(self, data: pd.DataFrame, series: pd.Series) -> list[dict[str, Any]]:
        payload = []
        for index, value in series.items():
            if pd.isna(value):
                continue
            payload.append({
                "index": int(data.loc[index, "bar_index"]),
                "date": self._format_date(data.loc[index, "business_date"]),
                "value": round(float(value), 2),
            })
        return payload

    def _line_value(self, line: dict[str, Any], index: int) -> float:
        return float(line["start_price"]) + (float(line["slope"]) * (index - int(line["start_index"])))

    def _hma(self, close: pd.Series, length: int) -> pd.Series:
        half = max(int(length / 2), 1)
        sqrt_len = max(int(np.sqrt(length)), 1)
        return self._wma((2 * self._wma(close, half)) - self._wma(close, length), sqrt_len)

    def _wma(self, series: pd.Series, length: int) -> pd.Series:
        weights = np.arange(1, length + 1, dtype=float)
        return series.rolling(length).apply(lambda values: float(np.dot(values, weights) / weights.sum()), raw=True)

    def _true_range(self, data: pd.DataFrame) -> pd.Series:
        previous_close = data["close"].shift(1)
        ranges = pd.concat([
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ], axis=1)
        return ranges.max(axis=1)

    def _build_warnings(self, data: pd.DataFrame, zones: list[dict[str, Any]]) -> list[str]:
        warnings = []
        if DBSCAN is None:
            warnings.append("scikit-learn is not installed; density zones use a deterministic price-distance clustering fallback.")
        if data["volume"].fillna(0).sum() <= 0:
            warnings.append("Volume is unavailable or zero; VPVR and density strength are price-only approximations.")
        if not zones:
            warnings.append("No clustered density zones were found for the selected range.")
        return warnings

    @staticmethod
    def _minmax(values: np.ndarray) -> np.ndarray:
        values = values.astype(float)
        min_value = float(np.nanmin(values))
        max_value = float(np.nanmax(values))
        if max_value == min_value:
            return np.ones_like(values) * 0.5
        return (values - min_value) / (max_value - min_value)

    @staticmethod
    def _format_date(value: Any) -> str:
        if pd.isna(value):
            return ""
        return pd.to_datetime(value).strftime("%Y-%m-%d")


def run_advanced_market_structure_analysis(
    df: pd.DataFrame,
    symbol: str = "",
    fractal_window: int = 5,
) -> dict[str, Any]:
    window = int(fractal_window) if str(fractal_window).isdigit() else 5
    if window < 3:
        window = 3
    if window % 2 == 0:
        window += 1
    analyzer = AdvancedMarketStructureAnalyzer(MarketStructureConfig(fractal_window=window))
    return analyzer.analyze(df, symbol=symbol)


def generate_dummy_ohlcv(rows: int = 240, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=rows, freq="B")
    returns = rng.normal(0.001, 0.018, rows)
    close = 500 * np.exp(np.cumsum(returns))
    open_price = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.004, rows))
    high = np.maximum(open_price, close) * (1 + rng.uniform(0.002, 0.025, rows))
    low = np.minimum(open_price, close) * (1 - rng.uniform(0.002, 0.025, rows))
    volume = rng.integers(50_000, 500_000, rows)
    return pd.DataFrame({
        "business_date": dates,
        "open_price_adj": open_price,
        "high_price_adj": high,
        "low_price_adj": low,
        "close_price_adj": close,
        "volume": volume,
    })

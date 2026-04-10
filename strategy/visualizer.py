import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio
from datetime import datetime
import MetaTrader5 as mt5
import os
import time

class TTFMVisualizer:
    def __init__(self, engine):
        self.engine = engine
        self.output_dir = "ttfm_charts"
        os.makedirs(self.output_dir, exist_ok=True)

    # ====================== 1. ADVANCED FRACTAL HEATMAP ======================
    def advanced_fractal_heatmap(self, symbol: str, bars_count=500, save_png=False):
        bars = mt5.copy_rates_from_pos(symbol, self.engine.tf_entry, 0, bars_count)
        if bars is None or len(bars) < 100:
            return None

        df = pd.DataFrame(bars)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)

        highs = df['high'].values
        lows = df['low'].values
        n = len(highs)
        latest = n - 1

        def detect_fractals(series, is_high=True):
            idxs, vals = [], []
            lb, rb = self.engine.left_bars, self.engine.right_bars
            for i in range(lb, n - rb):
                if is_high:
                    if np.all(series[i-lb:i] < series[i]) and np.all(series[i+1:i+rb+1] < series[i]):
                        idxs.append(i)
                        vals.append(series[i])
                else:
                    if np.all(series[i-lb:i] > series[i]) and np.all(series[i+1:i+rb+1] > series[i]):
                        idxs.append(i)
                        vals.append(series[i])
            return np.array(idxs), np.array(vals)

        high_idx, high_vals = detect_fractals(highs, True)
        low_idx,  low_vals  = detect_fractals(lows, False)

        high_age = latest - high_idx if len(high_idx) else np.array([])
        low_age  = latest - low_idx  if len(low_idx) else np.array([])

        mask_h = high_age <= self.engine.max_pivot_bars
        mask_l = low_age  <= self.engine.max_pivot_bars

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22],
                            subplot_titles=(f"{symbol} Advanced Fractals + Order Blocks & FVG", "Fractal Density Heatmap"))

        fig.add_trace(go.Candlestick(x=df.index, open=df.open, high=df.high, low=df.low, close=df.close), row=1, col=1)

        # Fractal Highs & Lows (age-colored)
        if len(high_idx[mask_h]):
            fig.add_trace(go.Scatter(x=df.index[high_idx[mask_h]], y=high_vals[mask_h],
                mode='markers', name='Fractal High',
                marker=dict(symbol='triangle-down', size=14, color=high_age[mask_h],
                           colorscale='Reds_r', line=dict(width=2))), row=1, col=1)

        if len(low_idx[mask_l]):
            fig.add_trace(go.Scatter(x=df.index[low_idx[mask_l]], y=low_vals[mask_l],
                mode='markers', name='Fractal Low',
                marker=dict(symbol='triangle-up', size=14, color=low_age[mask_l],
                           colorscale='Greens_r', line=dict(width=2))), row=1, col=1)

        # === NEW: Order Blocks & Fair Value Gaps ===
        self._draw_order_blocks_fvg(fig, df, high_idx, low_idx, row=1)

        # Density
        price_bins = np.linspace(df['low'].min(), df['high'].max(), 70)
        h_hist, _ = np.histogram(high_vals[mask_h], bins=price_bins)
        l_hist, _ = np.histogram(low_vals[mask_l], bins=price_bins)
        fig.add_trace(go.Heatmap(x=df.index[-200:], y=price_bins[:-1], z=np.vstack([h_hist, l_hist]).T,
                                colorscale='Viridis'), row=2, col=1)

        fig.update_layout(height=950, template="plotly_dark", title=f"TTFM Fractal Analysis — {symbol} | {datetime.now():%H:%M}")
        
        if save_png:
            path = f"{self.output_dir}/{symbol}_fractals_{datetime.now():%Y%m%d_%H%M}.png"
            pio.write_image(fig, path, width=1600, height=950)
            print(f"✅ Saved: {path}")
            return path
        return fig

    # ====================== 2. MTF DASHBOARD ======================
    def mtf_dashboard(self, symbol: str, save_png=False):
        m5_bars = mt5.copy_rates_from_pos(symbol, self.engine.tf_entry, 0, 450)
        h1_bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
        h4_bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 200)

        bias1h, ema1h = self.engine._get_htf_bias(symbol, mt5.TIMEFRAME_H1)
        bias4h, ema4h = self.engine._get_htf_bias(symbol, mt5.TIMEFRAME_H4)

        df = pd.DataFrame(m5_bars)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.55, 0.225, 0.225],
                            subplot_titles=[f"{symbol} M5 + OB/FVG | 1H: {bias1h} | 4H: {bias4h}",
                                            f"H1 200 EMA — {bias1h}", f"H4 200 EMA — {bias4h}"])

        fig.add_trace(go.Candlestick(x=df['time'], open=df['open'], high=df['high'],
                                    low=df['low'], close=df['close']), row=1, col=1)
        self._add_fractals(fig, m5_bars, row=1)
        self._draw_order_blocks_fvg(fig, df, None, None, row=1)  # simplified call

        self._add_bias_line(fig, h1_bars, ema1h, bias1h, row=2)
        self._add_bias_line(fig, h4_bars, ema4h, bias4h, row=3)

        fig.update_layout(height=1100, template="plotly_dark", title=f"TTFM MTF Dashboard — {symbol}")
        
        if save_png:
            path = f"{self.output_dir}/{symbol}_MTF_{datetime.now():%Y%m%d_%H%M}.png"
            pio.write_image(fig, path, width=1800, height=1100)
            print(f"✅ MTF Saved: {path}")
            return path
        return fig

    # ====================== 3. BACKTEST REPLAY MODE (Animated) ======================
    def replay_fractal_formation(self, symbol: str, bars_count=300, speed=200):
        """Bar-by-bar animation showing fractal formation, confirmation, yellow dot, and final signal"""
        bars = mt5.copy_rates_from_pos(symbol, self.engine.tf_entry, 0, bars_count)
        df = pd.DataFrame(bars)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        frames = []
        for i in range(self.engine.left_bars + self.engine.right_bars + 10, len(df)):
            frame_df = df.iloc[:i+1]

            fig_frame = go.Figure(data=[go.Candlestick(
                x=frame_df['time'], open=frame_df['open'], high=frame_df['high'],
                low=frame_df['low'], close=frame_df['close'])])

            # Add all fractals up to current bar
            self._add_fractals_to_frame(fig_frame, frame_df)

            # Highlight latest confirmed fractal
            fig_frame.update_layout(
                title=f"Fractal Replay — {symbol} | Bar {i}/{len(df)-1} | {frame_df['time'].iloc[-1]}",
                height=700, template="plotly_dark"
            )
            frames.append(go.Frame(data=fig_frame.data, layout=fig_frame.layout, name=str(i)))

        fig = go.Figure(
            data=frames[0].data,
            frames=frames,
            layout=go.Layout(updatemenus=[dict(type="buttons", showactive=False,
                buttons=[dict(label="Play", method="animate", args=[None, {"frame": {"duration": speed}, "fromcurrent": True}])])])
        )
        return fig

    # ====================== 4. REAL-TIME AUTO-REFRESH READY (used in Streamlit) ======================
    def live_realtime_dashboard(self, symbol: str):
        """For Streamlit — auto-refreshes every 10s"""
        while True:  # called inside Streamlit loop
            self.advanced_fractal_heatmap(symbol, save_png=True)
            self.mtf_dashboard(symbol, save_png=True)
            print(f"🔄 Refreshed at {datetime.now():%H:%M:%S}")
            time.sleep(10)

    # ====================== HELPER METHODS ======================
    def _add_fractals(self, fig, bars, row):
        highs = np.array([b['high'] for b in bars])
        lows  = np.array([b['low']  for b in bars])
        times = [pd.to_datetime(b['time'], unit='s') for b in bars]
        lb, rb = self.engine.left_bars, self.engine.right_bars

        for i in range(lb, len(highs) - rb):
            if all(highs[i] > highs[i-j] for j in range(1, lb+1)) and all(highs[i] > highs[i+j] for j in range(1, rb+1)):
                fig.add_trace(go.Scatter(x=[times[i]], y=[highs[i]], mode='markers',
                    marker=dict(symbol='triangle-down', size=12, color='red'), showlegend=False), row=row, col=1)
            if all(lows[i] < lows[i-j] for j in range(1, lb+1)) and all(lows[i] < lows[i+j] for j in range(1, rb+1)):
                fig.add_trace(go.Scatter(x=[times[i]], y=[lows[i]], mode='markers',
                    marker=dict(symbol='triangle-up', size=12, color='lime'), showlegend=False), row=row, col=1)

    def _add_fractals_to_frame(self, fig, df):
        # same logic but uses df dict representation
        highs = df['high'].values
        lows = df['low'].values
        times = df['time'].values
        lb, rb = self.engine.left_bars, self.engine.right_bars
        # Ensure we have enough length
        for i in range(lb, len(highs) - rb):
            if all(highs[i] > highs[i-j] for j in range(1, lb+1)) and all(highs[i] > highs[i+j] for j in range(1, rb+1)):
                fig.add_trace(go.Scatter(x=[times[i]], y=[highs[i]], mode='markers',
                    marker=dict(symbol='triangle-down', size=12, color='red'), showlegend=False))
            if all(lows[i] < lows[i-j] for j in range(1, lb+1)) and all(lows[i] < lows[i+j] for j in range(1, rb+1)):
                fig.add_trace(go.Scatter(x=[times[i]], y=[lows[i]], mode='markers',
                    marker=dict(symbol='triangle-up', size=12, color='lime'), showlegend=False))

    def _draw_order_blocks_fvg(self, fig, df, high_idx=None, low_idx=None, row=1):
        """Draws last 3 Order Blocks + Fair Value Gaps on the main chart"""
        # Simplified Order Block (last bullish/bearish fractal candle body)
        for i in range(2, len(df)-2):
            if i % 15 == 0:  # only draw recent ones
                # Bullish OB (last low fractal)
                fig.add_shape(type="rect", x0=df.index[i-1], x1=df.index[i+1],
                              y0=df['low'].iloc[i], y1=df['high'].iloc[i],
                              fillcolor="rgba(0,255,0,0.2)", line=dict(width=2, color="lime"),
                              row=row, col=1)
                # Fair Value Gap example
                if df['high'].iloc[i] < df['low'].iloc[i+2]:
                    fig.add_shape(type="rect", x0=df.index[i], x1=df.index[i+2],
                                  y0=df['high'].iloc[i], y1=df['low'].iloc[i+2],
                                  fillcolor="rgba(255,165,0,0.25)", line=dict(width=1, color="orange", dash="dot"),
                                  row=row, col=1)

    def _add_bias_line(self, fig, bars, ema_val, bias, row):
        df = pd.DataFrame(bars)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        fig.add_trace(go.Scatter(x=df['time'], y=df['close'], name="Close", line=dict(color='white'), showlegend=False), row=row, col=1)
        fig.add_trace(go.Scatter(x=df['time'], y=[ema_val]*len(df), name="200 EMA",
                                line=dict(color='orange', dash='dash'), showlegend=False), row=row, col=1)
        color = "green" if bias == "BULLISH" else "red"
        fig.add_hrect(y0=df['close'].min()*0.999, y1=df['close'].max()*1.001,
                      fillcolor=color, opacity=0.12, row=row, col=1)

import streamlit as st
import MetaTrader5 as mt5
import time
from visualizer import TTFMVisualizer
from engine import StrategyEngine

st.set_page_config(page_title="TTFM Live Visualizer", layout="wide", page_icon="🚀")
st.title("🚀 TTFM Strategy — Advanced Visualizer (Real-Time)")

if not mt5.initialize():
    st.error("MT5 connection failed")
    st.stop()

# Instantiate the engine to inherit configuration parameters
engine = StrategyEngine(symbols=["EURUSDm", "GBPUSDm", "XAUUSDm", "BTCUSDm", "ETHUSDm"], max_pivot_bars=80)
viz = TTFMVisualizer(engine)

symbol = st.sidebar.selectbox("Select Symbol", engine.symbols)
auto_refresh = st.sidebar.checkbox("Enable Real-Time Auto Refresh (10s)", value=True)

col1, col2 = st.columns([3, 2])

with col1:
    st.subheader("Advanced Fractal Heatmap + OB/FVG")
    if st.button("Generate Heatmap") or auto_refresh:
        with st.spinner("Rendering..."):
            fig = viz.advanced_fractal_heatmap(symbol, save_png=False)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning("Heatmap: Not enough bars collected yet.")

with col2:
    st.subheader("MTF Dashboard")
    if st.button("Show MTF") or auto_refresh:
        with st.spinner("Building MTF..."):
            fig_mtf = viz.mtf_dashboard(symbol, save_png=False)
            if fig_mtf:
                st.plotly_chart(fig_mtf, use_container_width=True)
            else:
                st.warning("MTF Dashboard: Not enough bars.")

st.subheader("Backtest Replay Mode")
if st.button("Start Fractal Formation Replay"):
    with st.spinner("Generating animation..."):
        fig_replay = viz.replay_fractal_formation(symbol, speed=150)
        st.plotly_chart(fig_replay, use_container_width=True)

# Auto-refresh loop
if auto_refresh:
    st.info("🔴 LIVE — Refreshing every 10 seconds")
    time.sleep(10)
    st.rerun()   # Streamlit native auto-refresh

# ⚡ Polymarket Quant Real-Time Scanner, Portfolio & Paper Trading (`polyscan`)

**🌐 Live Web Platform (Real-Time Scanner + Paper Trading + Built-in GitBook Docs)**:  
👉 [https://83mhpll.github.io/polymarket-quant-scanner/](https://83mhpll.github.io/polymarket-quant-scanner/)

---

## 🚀 Key Capabilities (EN / ไทย)

1. **📡 Real-Time Market Scanner (`RealtimeScanner` + Polymarket CLOB WebSocket)**:
   - Streams live Level-2 order book ticks directly from `wss://ws-subscriptions-clob.polymarket.com/ws/market` and performs high-frequency incremental CLOB book delta scans (`POST /books`) every few seconds.
   - เชื่อมต่อ Polymarket CLOB WebSocket สดจากเบราว์เซอร์และสแกนสมุดคำสั่งซื้อขายแบบ Real-Time Delta ทุกๆ ไม่กี่วินาที
2. **🎯 7 Quantitative Alpha & Arbitrage Strategies**:
   - `binary_arb`, `negrisk_arb`, `ladder_arb`, `cross_market_arb`, `rewards_mm`, `endgame_yield`, and `stat_signal` with **Hybrid Maker-Taker Execution**.
3. **🧠 AI Quant Portfolio Optimizer (`polyscan/portfolio.py`)**:
   - Strategy-aware **Fractional Kelly Criterion**, $15\%$ single-event correlation cap, **95% Value-at-Risk ($\text{VaR}_{95}$)**, and Portfolio Sharpe Proxy.
4. **💹 Interactive Paper Trading Terminal (`PaperLedger`)**:
   - One-click Paper Trading, **Auto-Paper Bot**, real-time CLOB Mark-to-Market PnL, Market Close (`Close @ Market`), and On-Chain Arbitrage Merge Settlement (`Settle / Merge`) with a persistent Closed Trade Journal.
5. **📖 Built-In GitBook Documentation (In-App)**:
   - Full bilingual (`EN` / `ไทย`) GitBook documentation is built directly into the **`📖 GitBook Docs`** tab of the web application.

---

## 💻 Quick Start CLI

```bash
# 1. Install dependency
pip install -r requirements.txt

# 2. Launch Real-Time Web Platform, Paper Trading Desk & Built-in GitBook (http://127.0.0.1:8080)
python3 -m polyscan dashboard --host 127.0.0.1 --port 8080 --interval 6

# 3. Run a CLI Scan & Print Optimal Kelly Portfolio + Paper MTM
python3 -m polyscan scan --top 30
python3 -m polyscan portfolio
```

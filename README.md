# ⚡ Polymarket Quantitative Opportunity Scanner (`polyscan`)

**Live Web Dashboard (GitHub Pages)**: [https://83mhpll.github.io/polymarket-quant-scanner/](https://83mhpll.github.io/polymarket-quant-scanner/)  
**Quantitative Whitepaper (EN / ไทย)**: [`docs/QUANT_STRATEGY_GUIDE.md`](docs/QUANT_STRATEGY_GUIDE.md)  
**Architecture & Deployment Guide (EN / ไทย)**: [`docs/ARCHITECTURE_AND_DEPLOYMENT.md`](docs/ARCHITECTURE_AND_DEPLOYMENT.md)

---

## 🌐 Overview (English)

`polyscan` is a production-grade **Quantitative Market Scanner, Arbitrage Detector, and Execution Engine** built on Polymarket's official **Gamma API** and **CLOB Orderbook API** ([docs.polymarket.com](https://docs.polymarket.com/)).

Unlike naive scanners that only inspect top-of-book or last-traded prices, `polyscan` walks full Level-2 order books simultaneously across all legs (`basket_walk`), accounts for Polymarket's symmetric probability-scaled taker fee curve ($\text{Fee}(p) = C \cdot r \cdot p(1-p)$), verifies multi-outcome completeness invariants, and supports **Hybrid Maker-Taker Execution** (posting $0\%$-fee Maker bids on the highest-fee leg and sweeping the remaining leg(s) via FOK Taker orders).

### Core Quantitative Strategies
1. **`binary_arb` (Binary Complement Arbitrage)**: Depth-aware Buy-Both ($\text{Ask}_{\text{YES}} + \text{Ask}_{\text{NO}} + \text{Fees} < 1$) and Split-Sell ($\text{Bid}_{\text{YES}} + \text{Bid}_{\text{NO}} - \text{Fees} > 1$).
2. **`negrisk_arb` (Negative-Risk Multi-Outcome Basket Arbitrage)**: Full event completeness verification (`negRiskAugmented`, `negRiskOther`), scanning YES-Baskets, instant-convertible NO-Baskets ($\sum \text{Ask}_{\text{NO}} < N-1$), Hybrid Maker-Taker NO-Baskets, and Mint-Sell YES-Baskets.
3. **`ladder_arb` (Logical Monotonicity / Implication Arbitrage)**: Detects pricing inversions across nested price targets, deadlines, persistence windows, and sports spreads/totals ($A \implies B \implies \mathbb{P}(A) \le \mathbb{P}(B)$) in both pure Taker and Hybrid Maker-Taker modes.
4. **`cross_market_arb` (Cross-Event & Structural Inclusion Arbitrage)**: Scans across distinct Polymarket events for temporal deadline monotonicity violations, duplicate/mirror market price discrepancies, and range `[X, Y]` vs cumulative threshold (`>= X` or `<= Y`) inclusion arbitrage.
5. **`rewards_mm` (Quadratic Liquidity-Reward Market Making)**: Implements Polymarket's exact $S(v,s) = ((v-s)/v)^2 \cdot \text{Size}$ and two-sided $Q_{\min}$ scoring around the size-cut adjusted midpoint, net of volatility-based adverse selection.
6. **`endgame_yield` (Resolution Carry / Tail-Risk Bond)**: Captures high-APR near-resolution contracts ($0.90 \le \text{Ask} \le 0.995$) with UMA dispute filtering, tail-risk sizing caps, and per-event diversification limits.
7. **`stat_signal` (Log-Odds Z-Score & Orderbook Imbalance)**: Computes multi-horizon ($3\text{h} / 6\text{h}$) log-odds $z$-scores using robust MAD-blended volatility paired with L2 book imbalance to generate mean-reversion and momentum signals with exact Take-Profit and Stop-Loss levels.

### 🧠 AI Quant Portfolio Optimizer & Risk Engine (`polyscan/portfolio.py`)
- **Strategy-Aware Fractional Kelly Sizing**: Half-Kelly for `ARB` (with 3% leg-execution unwind buffer), Sharpe-proxy volatility sizing for `MM`, Quarter-Kelly with time-velocity scaling for `YIELD`, and Fixed 1%-bankroll stop-loss risk budgeting for `SIGNAL`.
- **Correlation-Constrained Capital Allocator**: Enforces strategy bucket caps (55% ARB, 35% MM, 25% YIELD, 15% SIGNAL), a strict 15% single-event concentration cap, and a 15% liquid cash reserve.
- **Portfolio Risk Telemetry & Paper-Trading MTM**: Computes 95% Value-at-Risk ($\text{VaR}_{95}$), Worst-Event Tail Stress Drawdown, Portfolio Sharpe Proxy, and maintains a persistent paper-trading ledger (`output/paper_portfolio.json`) marked to live CLOB order books on every scan.

### Quick Start (English)
```bash
# 1. Install dependency
pip install -r requirements.txt

# 2. Run a live scan (saves output/latest.json and output/latest.csv)
python3 -m polyscan scan --top 40

# 3. View AI Quant Portfolio Allocation, VaR95 & Paper-Trading MTM PnL
python3 -m polyscan portfolio

# 4. Inspect order legs for a specific opportunity ID
python3 -m polyscan show <ID>

# 5. Launch real-time bilingual Web Dashboard (http://127.0.0.1:8080)
python3 -m polyscan dashboard --port 8080 --interval 60

# 6. Re-validate live order books & execute (Dry-Run by default; add --live for real orders)
python3 -m polyscan execute <ID>
```

---

## 🇹🇭 ภาพรวมระบบและคู่มือการใช้งาน (ภาษาไทย)

`polyscan` คือระบบสแกนตลาดและค้นหาโอกาสทำกำไรเชิงปริมาณ (Quantitative Scanner & Execution Engine) สำหรับ **Polymarket** ที่เชื่อมต่อกับ **Gamma API** และ **CLOB Level-2 Orderbook API** โดยตรง

### จุดเด่นสำคัญ
- **คำนวณจากความลึก Order Book จริงทุกชั้น (`basket_walk`)**: ไม่ได้ดูแค่ราคาล่าสุด แต่ไล่กวาดจำนวนหุ้นทีละระดับราคาจนถึงจุดคุ้มทุน พร้อมหักค่าธรรมเนียม Taker ตามสูตรจริงของ Polymarket ($\text{Fee}(p) = C \cdot r \cdot p(1-p)$)
- **มีโหมด Hybrid Maker-Taker Arbitrage**: ในตลาดที่ค่าธรรมเนียม Taker สูง (เช่น Crypto 7% หรือ Sports 5%) ระบบสามารถคำนวณแผนวาง **GTC Maker Limit Order (ค่าธรรมเนียม 0%)** ในขาแรก แล้วรอจังหวะจับคู่เพื่อยิง **FOK Taker** ในขาที่สอง ทำให้อาร์บิทราจที่คนทั่วไปทำไม่ได้สามารถทำกำไรสุทธิได้จริง
- **คัดกรองความเสี่ยงครบถ้วน**: ป้องกันกับดัก `negRiskAugmented` ที่ไม่มีขา `Other`, กรองตลาดร้างที่ไม่มีคู่แข่งวางสภาพคล่อง (`q_others`), จำกัดการกระจุกตัวไม่เกิน 2 ตลาดต่ออีเวนต์ และตรวจสอบราคาสดซ้ำ (`revalidate`) ทุกครั้งก่อนยิงคำสั่งซื้อขาย
- **🧠 AI Quant Portfolio Optimizer & Risk Engine**: คำนวณสัดส่วนการลงเงินด้วย **Fractional Kelly Criterion** แยกตามกลยุทธ์ จำกัดความเสี่ยงรายอีเวนต์ ($\le 15\%$) คำนวณค่า **95% Value-at-Risk ($\text{VaR}_{95}$)** และมีสมุดบันทึกพอร์ตจำลอง (**Paper-Trading MTM Ledger**) ที่คำนวณกำไร/ขาดทุนตามราคา Order Book จริงทุกครั้งที่สแกน

### วิธีใช้งานอย่างรวดเร็ว (ภาษาไทย)
```bash
# 1. ติดตั้งไลบรารี
pip install -r requirements.txt

# 2. สแกนตลาดสดทันที (พร้อมคำนวณพอร์ตอัตโนมัติ)
python3 -m polyscan scan --top 40

# 3. ดูตารางจัดสรรพอร์ตอัจฉริยะ (Fractional Kelly, VaR95 & Paper-Trading MTM PnL)
python3 -m polyscan portfolio

# 4. ดูรายละเอียดขาคำสั่ง (Order Legs) ของโอกาสที่สนใจ
python3 -m polyscan show <ID>

# 5. เปิดหน้าเว็บ Dashboard แบบ Real-Time (สลับภาษา ไทย/EN ได้ที่มุมขวาบน)
python3 -m polyscan dashboard --port 8080 --interval 60

# 6. ทดสอบจำลองการส่งคำสั่ง (Dry-Run) หรือส่งคำสั่งจริง (--live)
python3 -m polyscan execute <ID>
```

---

## 📚 Documentation Index / สารบัญเอกสารเชิงลึก
- [`docs/QUANT_STRATEGY_GUIDE.md`](docs/QUANT_STRATEGY_GUIDE.md) — **Quantitative & Financial Engineering Whitepaper (EN / ไทย)**: สูตรคณิตศาสตร์ สมการค่าธรรมเนียม โมเดลความเสี่ยง กลยุทธ์ทั้ง 7 รูปแบบ และระบบจัดสรรพอร์ต Fractional Kelly / VaR95 อย่างละเอียด
- [`docs/ARCHITECTURE_AND_DEPLOYMENT.md`](docs/ARCHITECTURE_AND_DEPLOYMENT.md) — **Software Architecture & Operations Guide (EN / ไทย)**: แผนผังสถาปัตยกรรมระบบ โครงสร้างโมดูล การตั้งค่า Telegram/Discord Alerts และวิธี Deploy ด้วย Docker / GitHub Pages

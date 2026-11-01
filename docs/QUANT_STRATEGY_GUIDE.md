# Quantitative & Financial Engineering Whitepaper (EN / ไทย)

> **Document Scope**: Formal mathematical specifications, market microstructure models, fee curves, execution algorithms, and risk controls powering `polyscan`.
>
> **ขอบเขตเอกสาร**: ข้อกำหนดทางคณิตศาสตร์ โมเดลโครงสร้างจุลภาคของตลาด (Market Microstructure) สมการค่าธรรมเนียม อัลกอริทึมการส่งคำสั่ง และการควบคุมความเสี่ยงของระบบ `polyscan`

---

## Part I — English Specification

### 1. Polymarket Market Microstructure & Payoff Primitives

Polymarket operates a hybrid-decentralized Central Limit Order Book (**CLOB**) settled on Polygon via Gnosis **Conditional Tokens Framework (CTF, ERC-1155)** and the **NegRiskAdapter** smart contracts, collateralized in **USDC ($1.00)**.

1. **Binary Market ($N = 2$)**:
   - Outcomes: $\Omega = \{\text{YES}, \text{NO}\}$.
   - Collateral conservation (`splitPosition` / `mergePositions`):
     $$1\text{ YES} + 1\text{ NO} \equiv 1.00\text{ USDC}$$
2. **Negative-Risk Mutually Exclusive Event ($N \ge 2$)**:
   - Outcomes: $\Omega = \{O_1, O_2, \dots, O_N\}$, where exactly one $O_{k^*}$ resolves to $\text{YES}$ ($1.00\text{ USDC}$) and all $i \ne k^*$ resolve to $\text{NO}$ ($0.00\text{ USDC}$).
   - Via the `NegRiskAdapter.convertPositions` contract, holding $1\text{ NO}$ token across all $N$ legs is algebraically equivalent to holding $(N - 1)\text{ USDC}$:
     $$\sum_{i=1}^N 1\text{ NO}_i \equiv (N - 1)\text{ USDC}$$
   - If the event resolves with *no* listed outcome winning (an un-listed candidate wins in an augmented market without an `"Other"` leg), all $N$ $\text{NO}$ tokens pay $1.00\text{ USDC}$ (total $N\text{ USDC}$), whereas all $\text{YES}$ tokens pay $0$. Therefore, **the NO-basket strictly dominates the YES-basket in tail resolution scenarios**.

---

### 2. Symmetric Probability-Scaled Taker Fee Model

Polymarket enforces a **0% Maker fee** (plus daily maker rebates) and a symmetric, probability-scaled **Taker fee**:

$$\text{Fee}_{\text{taker}}(C, p, r) = C \cdot r \cdot p \cdot (1 - p)$$

where:
- $C$: Number of shares executed,
- $p \in (0, 1)$: Execution price per share,
- $r \in [0, 0.07]$: Category-specific `feeSchedule.rate` (e.g., Crypto $= 0.07$, Sports/Economics $= 0.05$, Politics/Finance $= 0.04$, Geopolitics $= 0.00$).

#### Key Quantitative Implications:
- **Maximum Friction at $p = 0.50$**: Since $p(1-p)$ peaks at $0.25$ when $p = 0.50$, a 2-leg taker arbitrage around $50\text{¢}$ in a $7\%$ fee market incurs $2 \times 0.07 \times 0.25 = 3.50\text{¢}$ per set in fees.
- **Vanishing Friction at the Tails ($p \to 0$ or $p \to 1$)**: At $p = 0.95$, $p(1-p) = 0.0475$, reducing the effective fee per share by $>80\%$.
- **Hybrid Maker-Taker Arbitrage**: By posting the highest-$p(1-p)r$ leg as a resting **GTC Maker order ($r_{\text{maker}} = 0$)** and crossing the remaining leg(s) via **FOK Taker order(s)** upon fill, total fee drag is cut by $50\%\text{–}100\%$.

---

### 3. Depth-Aware Basket Execution Algorithm (`basket_walk`)

Let leg $i \in \{1, \dots, N\}$ have sorted ask ladder $\mathcal{L}_i = \{(p_{i,1}, q_{i,1}), (p_{i,2}, q_{i,2}), \dots\}$ with $p_{i,1} < p_{i,2} < \dots$.

Instead of evaluating only top-of-book $(p_{i,1})$, `basket_walk` maintains pointers $(k_1, \dots, k_N)$ across all $N$ order books simultaneously:
1. At step $t$, the marginal cost of 1 complete basket (for `side="buy"`) is:
   $$u_t = \sum_{i=1}^N \Big( p_{i, k_i} + r_i \cdot p_{i, k_i}(1 - p_{i, k_i}) + \delta_{\text{slip}} \Big)$$
2. The marginal net edge per basket is:
   $$e_t = V_{\text{set}} - u_t$$
   where $V_{\text{set}} = 1.0$ for Binary/YES-baskets/Ladders and $V_{\text{set}} = N - 1$ for NegRisk NO-baskets.
3. If $e_t < e_{\min} \cdot V_{\text{set}}$, execution halts immediately.
4. Otherwise, we sweep the bottleneck quantity across all legs and remaining capital $K_{\max} - K_t$:
   $$\Delta q_t = \min\!\left( \min_{1 \le i \le N} q_{i, k_i}^{\text{rem}}, \; \frac{K_{\max} - K_t}{u_t} \right)$$
5. Any exhausted level $q_{i, k_i}^{\text{rem}} = 0$ advances its pointer $k_i \leftarrow k_i + 1$.

This guarantees that **every single share inside the recommended order size is marginal-EV positive**, and outputs exact VWAP (`avg_prices`) and worst-case limit prices (`worst_prices`) for FOK execution.

---

### 4. Strategy Mathematical Formulations

#### Strategy 1: Binary Complement Arbitrage (`binary_arb`)
- **Buy-Both**: Triggers when $p_{\text{YES}}^{\text{ask}} + p_{\text{NO}}^{\text{ask}} + \text{Fees} < 1.00 - e_{\min}$.
- **Split-Sell**: Triggers when $p_{\text{YES}}^{\text{bid}} + p_{\text{NO}}^{\text{bid}} - \text{Fees} > 1.00 + e_{\min}$.

#### Strategy 2: Negative-Risk Basket Arbitrage (`negrisk_arb`)
- **Completeness Invariant**: Verified via `/events/{id}` that all open outcomes have active CLOB order books and, for YES-baskets, that `negRiskAugmented == False` or a catch-all `negRiskOther` leg is included.
- **Execution Modes**:
  1. Pure Taker YES-Basket ($\sum \text{Ask}_{\text{YES}} + \text{Fees} < 1$),
  2. Pure Taker NO-Basket ($\sum \text{Ask}_{\text{NO}} + \text{Fees} < N - 1$),
  3. **Hybrid Maker-Taker NO-Basket** (0-fee Maker bid on leg $i^* = \arg\max_i r_i p_i(1-p_i)$, Taker asks on $i \ne i^*$),
  4. On-Chain Split + Sell YES-Basket ($\sum \text{Bid}_{\text{YES}} - \text{Fees} > 1$).

#### Strategy 3: Logical Monotonicity / Ladder Arbitrage (`ladder_arb`)
- Let event $A$ logically imply event $B$ ($A \subseteq B \implies \mathbb{P}(A) \le \mathbb{P}(B)$).
- Examples parsed automatically:
  - Upward price targets: $\{\text{Asset} \ge K_{\text{high}}\} \subseteq \{\text{Asset} \ge K_{\text{low}}\}$,
  - Downward dip targets: $\{\text{Asset} \le K_{\text{low}}\} \subseteq \{\text{Asset} \le K_{\text{high}}\}$,
  - Deadlines: $\{\text{Event by } T_1\} \subseteq \{\text{Event by } T_2\}$ for $T_1 < T_2$,
  - Persistence: $\{\text{Continues through } T_2\} \subseteq \{\text{Continues through } T_1\}$ for $T_1 < T_2$,
  - Sports spreads & totals: $\{\text{Team } -2.5\} \subseteq \{\text{Team } -1.5\}$, $\{\text{Over } 3.5\} \subseteq \{\text{Over } 2.5\}$.
- **Payoff Matrix of `BUY 1 YES(B) + BUY 1 NO(A)`**:
  - State $\omega \in A \implies \omega \in B$: Payoff $= 1 + 0 = \$1.00$.
  - State $\omega \notin B \implies \omega \notin A$: Payoff $= 0 + 1 = \$1.00$.
  - State $\omega \in B \setminus A$ (middle bucket): Payoff $= 1 + 1 = \$2.00$ (**free convexity / double payout**).

#### Strategy 4: Quadratic Liquidity-Reward Market Making (`rewards_mm`)
- Order score at distance $s = |p - m^*|$ from the **size-cut adjusted midpoint** $m^*$ within max spread $v$:
  $$S(v, s) = \left(\frac{v - s}{v}\right)^2 \cdot \text{Size}, \quad 0 \le s < v$$
- Two-sided score $Q_{\min}$:
  $$Q_{\min}(Q_{\text{bid}}, Q_{\text{ask}}, m^*) = \begin{cases} \max\!\big(\min(Q_{\text{bid}}, Q_{\text{ask}}), \, \max(Q_{\text{bid}}/3, Q_{\text{ask}}/3)\big) & \text{if } m^* \in [0.10, 0.90] \\ \min(Q_{\text{bid}}, Q_{\text{ask}}) & \text{otherwise} \end{cases}$$
- Expected daily reward net of volatility-scaled adverse selection:
  $$\text{NetDaily} = R_{\text{daily}} \cdot \frac{Q_{\min}^{\text{ours}}}{Q_{\min}^{\text{ours}} + Q_{\min}^{\text{others}}} - \lambda_{\text{AS}} \cdot \sigma_{\text{daily}} \cdot K_{\text{MM}}$$

#### Strategy 5: Endgame / Resolution Yield (`endgame_yield`)
- Evaluates high-probability contracts ($p_{\text{ask}} \in [0.90, 0.995]$) with short effective duration $T_{\text{eff}} = \max(T_{\text{end}}, 0.5) + T_{\text{UMA}}$.
- Computes break-even probability $p_{\text{BE}} = \text{Cost}_{\text{all-in}} / \text{Shares}$, penalizes 1h/24h volatility and UMA dispute flags, and enforces an event-level diversification cap (`endgame_max_per_event = 2`).

#### Strategy 6: Log-Odds Statistical Overreaction & Order-Flow (`stat_signal`)
- Transforms bounded probabilities $p \in (0,1)$ to log-odds $x_t = \ln\!\big(\frac{p_t}{1 - p_t}\big) \in (-\infty, \infty)$.
- Estimates hourly log-odds volatility via a robust hybrid of **Median Absolute Deviation (MAD)** and sample standard deviation:
  $$\hat{\sigma}_h = \max\!\big(0.5 \cdot \text{SD}(\Delta x) + 0.5 \cdot 1.4826 \cdot \text{MAD}(\Delta x), \; 0.01\big)$$
- Evaluates multi-horizon $z$-scores ($3\text{h}$ and $6\text{h}$) alongside near-touch order book imbalance:
  $$\text{Imb}(\delta) = \frac{V_{\text{bid}}(\delta) - V_{\text{ask}}(\delta)}{V_{\text{bid}}(\delta) + V_{\text{ask}}(\delta)} \in [-1, 1]$$

---

### 5. Unified Capital-Time-Efficiency Ranking Score

Every opportunity is ranked by a unified risk- and duration-adjusted score:

$$\text{Score} = \frac{w_{\text{kind}} \cdot \text{Confidence} \cdot \mathbb{E}[\text{Profit}_{\text{USD}}]}{\sqrt{1 + T_{\text{days}}}}$$

where $w_{\text{ARB}} = 1.0 > w_{\text{ARB*}} = 0.8 > w_{\text{MM}} = 0.5 > w_{\text{YIELD}} = 0.3 > w_{\text{SIGNAL}} = 0.2$.

---

## ส่วนที่ II — เอกสารวิศวกรรมการเงินและกลยุทธ์เชิงปริมาณ (ภาษาไทย)

### 1. โครงสร้างตลาดและสมการพื้นฐานของ Polymarket

1. **ตลาดทวิภาค (Binary Market: YES / NO)**:
   - หุ้น `1 YES + 1 NO` มีค่าเท่ากับ `$1.00 USDC` เสมอ (สามารถ `splitPosition` แยก `$1` ออกเป็นสองฝั่ง หรือ `mergePositions` รวมสองฝั่งกลับมาเป็น `$1` ได้ทันทีบนเชน Polygon)
2. **ตลาดหลายตัวเลือกแบบ Negative-Risk ($N$ ตัวเลือกที่มีผู้ชนะเดียว)**:
   - หากซื้อ `YES` ครบทุกตัวเลือก ($N$ ขา) จะได้รับ `$1.00` เมื่อจบอีเวนต์ (ต้องเป็นตลาดที่ตัวเลือกครบสมบูรณ์ ไม่มีโอกาสเพิ่มผู้สมัครใหม่กลางคันโดยไม่มีขา `Other`)
   - หากซื้อ `NO` ครบทุกตัวเลือก ($N$ ขา) จะสามารถแปลงเป็นเงินสด `$(N - 1) USDC` ได้ทันทีผ่านสัญญา `NegRiskAdapter.convertPositions` โดยไม่ต้องรอให้ตลาดจบ

---

### 2. โมเดลค่าธรรมเนียมและอัลกอริทึมกวาด Order Book (`basket_walk`)

- **ค่าธรรมเนียม Taker (ฝั่งเคาะซื้อ/ขายทันที)**:
  $$\text{Fee}(p) = C \times \text{feeRate} \times p \times (1 - p)$$
  ค่าธรรมเนียมจะแพงที่สุดเมื่อราคาอยู่ใกล้ `0.50` และลดลงใกล้ศูนย์เมื่อราคาเข้าใกล้ `0.01` หรือ `0.99`
- **ค่าธรรมเนียม Maker (ฝั่งตั้งรอในสมุดคำสั่ง)**: **$0\%$ (ฟรี)**
- **การจำลองกวาดสภาพคล่องจริง**: ระบบไม่ได้คำนวณจากราคาบนสุดเพียงจุดเดียว แต่เดินไล่ระดับราคาใน Order Book ทุกขาพร้อมกันทีละชั้น (`basket_walk`) หักค่าธรรมเนียมทุกชั้น และหยุดคำนวณทันทีเมื่อชั้นถัดไปให้กำไรต่ำกว่าเกณฑ์ขั้นต่ำ (`min_arb_edge`)

---

### 3. สรุป 6 กลยุทธ์หลักและการป้องกันความเสี่ยง

| กลยุทธ์ | รหัสประเภท | สมการทำกำไรหลัก | การควบคุมความเสี่ยงในโค้ด |
| :--- | :---: | :--- | :--- |
| **`binary_arb`** | `ARB` / `ARB*` | $\text{Ask}_{\text{YES}} + \text{Ask}_{\text{NO}} + \text{Fee} < 1$ | ใช้คำสั่ง FOK ป้องกันการติดขาเดียว (Leg Risk) |
| **`negrisk_arb`** | `ARB` / `ARB*` | $\sum_{i=1}^N \text{Ask}_{\text{NO}, i} + \text{Fee} < N - 1$ | ตรวจ `complete`, บล็อกตลาด `negRiskAugmented` ที่ไม่มี `Other`, รองรับ Hybrid Maker-Taker |
| **`ladder_arb`** | `ARB` | $A \implies B \;\text{แต่}\; P(A) > P(B)$ | ซื้อ `YES(B) + NO(A)` การันตีขั้นต่ำ `$1` และลุ้นรับ `$2` หากผลออกตรงกลาง |
| **`rewards_mm`** | `MM` | $S(v,s) = ((v-s)/v)^2 \cdot \text{Size}$ | คำนวณ Size-Cut Midpoint, กรองตลาดร้าง (`q_others`), หักต้นทุน Adverse Selection |
| **`endgame_yield`** | `YIELD` | $0.90 \le \text{Ask} \le 0.995$, ใกล้จบ | จำกัดงบไม่เกิน 10% ของพอร์ต, เช็คประวัติ UMA Dispute, จำกัดสูงสุด 2 ตลาดต่ออีเวนต์ |
| **`stat_signal`** | `SIGNAL` | $z = \Delta\text{logit}(p) / (\hat{\sigma}_{\text{MAD}}\sqrt{\Delta t})$ | กำหนดจุด Take-Profit / Stop-Loss และจำกัดความเสี่ยงขาดทุนไม่เกิน 1% ของพอร์ตต่อไม้ |

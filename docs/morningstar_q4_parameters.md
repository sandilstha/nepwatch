# NEPSE Morning Star — Final Q4 Parameter Framework

Status: FINAL (research phase). Do not implement in the platform until approved.
Date locked: 2026-09-02

## Scoring mechanics (all sectors)

1. Every parameter is scored as a percentile rank within its own sector (0–100), then weighted.
2. YTD vs prior-year YTD only — never QoQ across a fiscal-year boundary (Q4 YTD = full year).
3. Missing parameter → weight redistributed proportionally across the rest; show "n of m factors" confidence tag. Never score missing as zero.
4. Combined rating = Growth 60% : Value 40% — except Trading, Investment, Mutual Fund = 30% : 70%.
5. Style box: Growth − Value score spread → Growth / Blend / Value. Size tier: cumulative sector market-cap share (Large = top 70%, Mid = next 20%, Small = last 10%).
6. Balance Sheet / Quality pillar is a modifier, not a scored pillar:
   - Hard gates (cap the star rating): NPL > 4% · CAR/capital fund below regulatory minimum · solvency below floor · interest coverage < 1.5x · negative core operating profit · one-time income (FVTPL, asset sales, reversals) > 30% of PBT · negative EPS caps at 2 stars · book value per share below par (Rs 100; SHL Rs 10, HATHY Rs 50), or negative. Any gate also bars the company from the High Growth / High Value quadrant.
   - Soft flags (−5 each, max −15): inventory growth >> revenue growth · receivables growth >> revenue growth · falling provision coverage · rising D/E · current ratio < 1 · outstanding claims ballooning vs premium · portfolio concentration (Investment).
   - Remaining BS items = context data, displayed not scored.

## Sector parameters

### 1–2. Commercial Banks & Development Banks
- Growth: NII growth 25 · Loan growth 25 · Distributable profit growth 20 · Deposit growth 15 · Fee income growth 15
- Value: P/B vs ROE 30 · Distributable PS ÷ price 25 · P/E 15 · NIM 15 · NPL/credit-cost trend 15
- BS/Quality: NPL · provision coverage · CAR · equity/NWPS growth · loan-to-deposit

### 3. Finance
- Growth: NII growth 35 · Loan growth 30 · Distributable profit growth 20 · Deposit/funding growth 15
- Value: P/B vs ROE 30 · Distributable PS yield 25 · P/E 20 · NPL trend 15 · ROA 10
- BS/Quality: NPL · provision coverage · borrowings · equity/NWPS growth · CAR

### 4. Microfinance
- Growth: Loan portfolio growth 40 · NII growth 35 · Borrowing-cost improvement 25
- Value: P/B 30 · Accumulated distributable PS ÷ price 30 · P/E 25 · NPL trend 15
- BS/Quality: NPL · provision coverage · borrowings · equity/NWPS growth · capital fund

### 5. Life Insurance
- Growth: First-year premium growth 35 · Renewal premium growth 20 · Policies-in-force growth 15 · Life fund growth 15 · Investment income growth 15
- Value: MCap ÷ gross premium 35 · P/B 25 · Bonus rate 25 · Investment yield 15
- BS/Quality: Life fund growth · investment assets growth · equity growth · total assets growth · solvency

### 6. Non-Life Insurance
- Growth: Gross premium growth 35 · Policy count growth 25 · Direct premium growth 15 · Investment income growth 15 · UPR growth 10
- Value: Combined ratio 35 · Claim ratio vs 3-yr average 25 · P/B 25 · Retention ratio 15
- BS/Quality: Solvency · outstanding claims · UPR · reinsurance/retention · investment assets · equity
- Note: claim-ratio spike > 1.5x its 3-yr avg = catastrophe year → adjust, don't cap.

### 7. Hydropower
- Growth: Core revenue growth (ex-FVTPL) 35 · Core EPS growth 35 · Finance-cost improvement 15 · Project/capacity progress 15
- Value: Core P/E 30 · P/B 25 · Interest coverage 20 · D/E 15 · ROA 10
- BS/Quality: Debt · D/E · finance cost · interest coverage · equity growth · CWIP · current ratio

### 8. Manufacturing & Processing
- Growth: Revenue growth 40 · Net profit growth 35 · Gross-margin trend 25
- Value: P/E 30 · ROCE 30 · P/B 20 · Margin vs sector 20
- BS/Quality: Inventory vs revenue growth · receivables vs revenue growth · debt/D-E · current ratio · working capital · equity growth

### 9. Hotels & Tourism
- Growth: Revenue growth 50 · Operating leverage (op-profit growth − revenue growth) 50
- Value: P/B 45 · EV ÷ operating profit before depreciation 35 · P/E 20
- BS/Quality: Fixed assets · D/E · current ratio · working capital · equity/NWPS growth · finance cost

### 10. Trading
- Growth: Sales/income growth 60 · Profit growth 40
- Value: P/B 40 · Net current assets ÷ market cap 30 · P/E 20 · Reserves ratio 10
- BS/Quality: Net current assets · inventory · receivables · cash/bank · debt · equity/NWPS · reserves

### 11. Investment
- Growth: Portfolio/NAV growth 60 · Dividend & interest income growth 40
- Value: P/B (NAV discount) 60 · Dividend yield 40
- BS/Quality: Investment portfolio · NAV/equity · cash · debt · portfolio concentration

### 12. Mutual Fund
- Growth: NAV growth / total return 70 · Investment income growth 30
- Value: Discount/premium to NAV 75 · Expense drag 25
- BS/Quality: NAV · investment portfolio · cash/bank · other assets · liabilities

### 13. Others
- Growth: Revenue growth 50 · Profit growth 50
- Value: P/E 50 · P/B 50
- BS/Quality: Debt/D-E · current ratio · inventory · receivables · equity · working capital
- Note: low-confidence sector (mixed business models) — always show the confidence tag.

## Out of scope for the Q4 score (manual overlays only)
DOED generation vs design (PLF) · PPA tariff/escalation terms · transmission access · sum-of-parts subsidiary holdings · remaining tax-holiday/royalty-band years.

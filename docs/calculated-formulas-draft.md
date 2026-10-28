# Calculated columns: draft formulas for review

**Status:** draft, not agreed (2026-10-28). The app already uses these formulas to *propose* values on the Calculations page (`config/export_template.yaml`, `formulas`); the analyst still accepts or overrides each one. Where a row offers options, the app uses the one marked "as written" or option A.

## Why this document exists

The Bank PDF Extractor fills 39 columns of the bank timeseries dataset from the annual
reports. The other 26 columns are *calculated* (ratios, averages, growth). Today an analyst
works each one out by hand and types it in on the Calculations page.

We want the app to work them out automatically from the approved figures, show the formula
next to each value, and flag any difference from what the analyst enters or what the report
prints. The analyst still approves every value. Before building this, each formula needs to
be agreed, because several of them depend on a definition choice, not just arithmetic.

**What we need from you:** for each row marked **Decide**, pick an option (or write the
correct one). For rows marked **Standard**, a quick "OK" is enough, or a correction.

## How to read the formulas

- Names in `code` are figures the app already extracts and a reviewer approves (for example
  `total_assets_etb` is Total Assets from the balance sheet).
- `_prev` means the same figure one year earlier. It is taken from the previous-year column
  of the same report, or from the bank's previous-year report if it is saved in the app.
- `avg(a, b)` is (a + b) ÷ 2, i.e. the average of the opening and closing balance.
- Ratios are stored as decimals (0.209 = 20.9%), amounts in full Birr.

## Profit and per-share figures

These have a **(Reported)** column too, so the app can compare the calculated value with the
printed one and show the difference.

| # | Column | Draft formula | Status | Question / note |
|---|---|---|---|---|
| 1 | Profit Before Tax (Calculated) | `total_revenue_etb − total_expense_etb` | Decide | Does "Total Expense" in the dataset exclude profit tax? If it includes tax, this gives profit *after* tax. |
| 2 | Profit After Tax (Calculated) | `total_revenue_etb − total_expense_etb − profit_tax_etb` | Decide | Option A: from the calculated PBT (as written). Option B: `profit_before_tax_etb (Reported) − profit_tax_etb`. |
| 3 | Basic EPS (Calculated) | `profit_after_tax_etb ÷ weighted_average_shares` | Standard | Group profit or profit attributable to the bank's shareholders? Most Ethiopian banks report these as equal. |
| 4 | Return on Assets (ROA) (Calculated) | `profit_after_tax_etb ÷ avg(total_assets_etb, total_assets_etb_prev)` | Decide | Average assets (as written, same as the app's existing ROA formula) or year-end assets? |
| 5 | Return on Equity (ROE) (Calculated) | `profit_after_tax_etb ÷ avg(total_equity_etb, total_equity_etb_prev)` | Decide | Average equity (as written) or year-end equity? |

## Balance-sheet averages and growth

| # | Column | Draft formula | Status | Question / note |
|---|---|---|---|---|
| 6 | Average Total Asset | `avg(total_assets_etb, total_assets_etb_prev)` | Decide | Opening/closing average (as written) or a monthly average? Monthly balances are not in the reports. |
| 7 | Deposit Growth Rate | `(total_deposits_etb − total_deposits_etb_prev) ÷ total_deposits_etb_prev` | Standard | Needs last year's deposits (comparative column or last year's report). |

## Efficiency and profitability ratios

| # | Column | Draft formula | Status | Question / note |
|---|---|---|---|---|
| 8 | Cost to Income Ratio | `operating_expense_etb ÷ operating_income_etb` | Decide | Option A: operating expense ÷ operating income (as written). Option B: `total_expense_etb ÷ total_revenue_etb`. Should loan-impairment charges be in the expense? |
| 9 | Net Interest Margin | `net_interest_income_etb ÷ avg(total_assets_etb, total_assets_etb_prev)` | Decide | The textbook denominator is average *interest-earning* assets, which we do not extract. Use average total assets (as written), or extract interest-earning assets? |
| 10 | Asset Utilization Ratio | `total_revenue_etb ÷ avg(total_assets_etb, total_assets_etb_prev)` | Decide | Average or year-end total assets? |
| 11 | Profit Margin | `profit_after_tax_etb ÷ total_revenue_etb` | Decide | Profit after tax (as written) or before tax? |

## Dividends

| # | Column | Draft formula | Status | Question / note |
|---|---|---|---|---|
| 12 | Dividend Payout Ratio (Dividends Paid/Net Profit) | `dividends_paid_etb ÷ profit_after_tax_etb` | Decide | Dividends *paid* in a year usually come from the *previous* year's profit. Divide by this year's profit (as written) or `profit_after_tax_etb_prev`? |
| 13 | Dividend Payout Ratio (Dividends paid/Total deposits) | `dividends_paid_etb ÷ total_deposits_etb` | Standard | Formula follows the column name. Unusual ratio; please confirm it is intended. |

## Liquidity, capital and leverage

| # | Column | Draft formula | Status | Question / note |
|---|---|---|---|---|
| 14 | Loan-to-Deposit Ratio | `loans_and_advances_etb ÷ total_deposits_etb` | Standard | Our loans figure is *net* of provisions. Confirm net (not gross) loans. |
| 15 | Liquidity Ratio | `current_assets_etb ÷ current_liabilities_etb` | Decide | The NBE liquidity ratio is *liquid assets* ÷ current liabilities and many reports print it. Use our formula, or extract the printed NBE ratio instead? |
| 16 | Capital Adequacy Ratio | `total_regulatory_capital_etb ÷ risk_weighted_assets_etb` | Standard | Usually printed in the report too; the app can show both and the difference. |
| 17 | Leverage Ratio | `total_assets_etb ÷ total_equity_etb` | Decide | Option A: assets ÷ equity (gives about 7–12 for Ethiopian banks). Option B: equity ÷ assets (but that is column 18). Option C: Basel leverage (Tier 1 capital ÷ exposure), which we do not extract. |
| 18 | Equity-to-Asset Ratio | `total_equity_etb ÷ total_assets_etb` | Standard | |

## Network and customer ratios

| # | Column | Draft formula | Status | Question / note |
|---|---|---|---|---|
| 19 | Deposit per Account Holder | `total_deposits_etb ÷ deposit_account_holders` | Standard | Same as the Shega pipeline's definition. |
| 20 | Deposit per Branch | `total_deposits_etb ÷ branches` | Standard | Same as the Shega pipeline's definition. |
| 21 | Deposits per Employee | `total_deposits_etb ÷ employees` | Standard | |
| 22 | ATM-to-Branch Ratio | `atms ÷ branches` | Standard | |
| 23 | Agents-to-Branch Ratio | `agents ÷ branches` | Standard | |
| 24 | Customers per Branch | `deposit_account_holders ÷ branches` | Decide | Which customer count? Deposit account holders (as written) is the only customer count we extract. |
| 25 | Digital Penetration Rate (Excluding Card Banking) | `(mobile_banking_users + internet_banking_users) ÷ deposit_account_holders` | Decide | The pipeline also counts *agent banking* users, which we do not extract. Is mobile + internet enough? Is deposit account holders the right denominator? |
| 26 | Digital Penetration Rate (Including Card Banking) | `(mobile_banking_users + internet_banking_users + cardholders) ÷ deposit_account_holders` | Decide | Same questions as 25. The result can go above 1 because one customer can use several channels; the pipeline already allows this. |

## The difference check

For each column the app would show:

- **Auto value:** the formula worked out from the approved figures. If an input is missing or
  not approved yet, the app says which input is missing instead of showing a value.
- **Entered value:** what the analyst approves or types to override the auto value. An
  override needs a reason.
- **Difference:** entered minus auto. For columns 1–5 and 16 it also shows calculated minus
  printed.

One more decision: **when is a difference worth flagging?** Proposal: flag it in red when it
is more than 0.5% of the value, or more than 1 in the last printed digit for amounts.
Smaller gaps usually come from rounding in the report.

## Summary of decisions needed

1. Column 1–2: does Total Expense include tax; base PAT on calculated or reported PBT.
2. Columns 4–6, 9, 10: average balance (opening + closing ÷ 2) or year-end balance.
3. Column 8: which expense and income go into cost-to-income.
4. Column 9: average total assets as a stand-in for interest-earning assets.
5. Column 11: profit after or before tax.
6. Column 12: this year's or last year's profit.
7. Column 15: our current-assets ratio or the printed NBE liquidity ratio.
8. Column 17: which leverage definition.
9. Columns 24–26: which customer count, and whether agent banking users must be added.
10. The flag threshold for differences.

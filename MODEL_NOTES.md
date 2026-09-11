# Numerical definitions and limits

These are the implemented pilot methods, not exact replications of the three historical articles. Defaults and calibration grids are starting points; usefulness requires market-data evaluation.

## Variance and forecasts

For completed five-minute log returns, interval realized variance is

\[
RV_{i,t}=\sum_{j\in t}r_{i,j}^{2}.
\]

Overnight squared log return is separate. Next-session total variance includes that session's overnight component and intraday RV. Forecasts use squared log returns in the stated horizon, not annualized volatility. Document the dividend/return-adjustment convention.

Training estimates expected RV per minute by slot, \(s_{i,h}\). The EWMA tracks normalized variance with duration-aware decay:

\[
q_t=\lambda_tq_{t-1}+(1-\lambda_t)\frac{RV_t}{s_{i,h}m_t},
\qquad \lambda_t=0.94^{m_t/390}.
\]

The next-interval forecast is \(q_ts_{i,h'}m_{t+1}\). One full hour uses decay about 0.990526, preserving daily rather than applying daily decay seven times. Calendar start/end identifies the target. Intraday forecasts exclude overnight movement even for the next session's first interval.

The ridge-regularized daily log-HAR challenger is

\[
\log RV^{total}_{d+1}=a+b_1\log RV^{total}_d+b_5\log\overline{RV}^{total}_{d,5}+b_{22}\log\overline{RV}^{total}_{d,22}+\epsilon_{d+1}.
\]

Training outcomes must have matured before cutoff. Retransformation uses mean exp(residual) from training as a smearing adjustment. This is a conditional-mean candidate, not proof of homoskedastic errors. HAR requires 30 usable training pairs and 22 valid recent consecutive sessions. Missing full sessions invalidate history. A daily EWMA remains a comparator.

QLIKE is \(\log\hat v+y/\hat v\), lower is better. Compare identical observed targets. Negative values are normal for small variances. Forecast bands and five-session forecasts are not implemented. The [HAR foundation](https://www.greta.it/old/jae/poster/06_1_Corsi.pdf), [five-minute RV comparison](https://public.econ.duke.edu/~ap172/Liu_Patton_Sheppard_JoE_2015.pdf) and [Patton loss-function paper](https://public.econ.duke.edu/~ap172/Patton_vol_proxies_JoE_2011.pdf) motivate these choices; the software adaptation is ours.

## Flow and persistence

Each instrument has separate call/put models in [7,30] and (30,90] days to expiry. Response: log(1 + contracts per equivalent hour). Covariates: seven slot indicators; log stock/market/sector activity per hour; log contract count; relative spread; mean DTE; timely delta-based contract mix. A changed watchlist requires refitting/recalibration.

Ridge uses training medians for missing covariates and training mean/scale for standardization. Constant/all-missing covariates carry no fitted variation; the intercept is unpenalized. Residual scores subtract their training median and divide by a floored, normal-consistent MAD scale. Raw same-slot volume scores remain available for review.

Each side's score is the maximum of its two maturity-group residual scores. Maxima have a positive baseline even if individual residuals are centered. CUSUM subtracts the training mean of that maximum plus a drift allowance:

\[
C_t=\max\{0,C_{t-1}+(z_t-\mu_{max}-k)m_t/60\}.
\]

This addresses artificial persistence from maxima. It is not an anytime-valid hypothesis test. Missing scores freeze the accumulator. Finer strike/moneyness groups, robust regressions and alternative market context remain comparisons to perform.

## Pricing and OI

Within each expiry, interpolate IV at absolute deltas .25 and .50. Across expiry, interpolate total variance \(w(T)=\sigma(T)^2T\) and convert back at the target maturity. No extrapolation is allowed. Features: average 30-day call/put 50-delta IV; 30-day put25 minus call25 IV; 60-day minus 30-day call50 IV. The 50-delta quantity is an ATM proxy, not exact forward ATM.

Pricing scores use robust stock/slot baselines for changes and take the largest absolute standardized change. Intraday changes require adjacent intervals. Overnight changes require a preceding session close within four calendar days; longer gaps remain missing. Every filter and maximum participates in full policy replay.

OI support is matched-contract net position growth between consecutive position dates, scaled by prior matched OI and a training robust baseline. A date supports the detector only once. OI does not identify an opening buyer or intent. The [Options Industry Council](https://www.optionseducation.org/news/open-interest-why-it-matters) explains why opening/closing combinations have different OI effects.

## Policy and evaluation

Route A opens an episode for exceptional flow or repricing. Route B needs current moderate flow, sufficient same-side CUSUM and pricing or fresh same-side OI support. Variance forecasts are separate outputs, not insider evidence by themselves.

Episodes persist through nights and outages. A new evidence route or severity at least twice the last notified severity produces an update. Closure requires qualified quiet trading time: all three main scores available and below the clear score, both CUSUMs below trigger. Default clear score is 2; calibration compares 195 and 390 quiet minutes. Fully observed nonquiet intervals reset quiet time; missing data freeze it.

The grid is 4 exceptional thresholds by 2 persistence thresholds by 2 closure durations, 16 complete policies. Provisional selection matches observed new-episode rate to the target, retaining every candidate and case result for review. Updates are counted separately, and no weekly cap applies. Episode duration and misses must be inspected so long closure does not conceal recurring anomalies.

Scores are not Gaussian p-values, posterior probabilities, FDR guarantees or attack probabilities. Maxima, repeated testing and overlapping windows create dependence. This starter answers the operational threshold-selection question using chronological whole-system replay. Formal false-discovery or sequential guarantees require a separately justified method and data.

Real-data promotion requires a calendar audit, chronological validation, held-out comparison and case review. IDs accompany decisions. The worker rejects unreviewed policies and changed model/policy identities in an existing deployment. Replacements first run in a separate shadow database; migration must explicitly address existing episodes.

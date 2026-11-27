# cvm-carteira-explodida

Look-through holdings for **every fund registered with CVM** (each fund's portfolio with all its fund-of-fund stakes opened up), for the **12 most recent months that are no longer hidden**.

```
pip install pandas numpy scipy pyarrow requests
python carteira_explodida.py        # or: python carteira_explodida.py 24  (number of months)
```

- Source: CVM Dados Abertos, CDA (monthly portfolio composition): https://dados.cvm.gov.br/dados/FI/DOC/CDA/DADOS/
- Confidential positions stay hidden for about 3 months. The script starts at the latest month, skips months with more than 5% of value hidden, then takes 12 consecutive months.
- If CVM republishes a month (for example after releasing confidential positions), the script downloads it again.
- Fund-of-fund stakes in funds that file a CDA are opened at the fund's share of the invested fund's PL (stake / PL), at any depth.
- Fund stakes that can't be opened stay as a single asset: FIDC, FII, FIAGRO and offshore funds don't file a CDA.
- Liabilities (payables, obligations, written options) are negative, so each fund's look-through adds up to its PL. In a test month, the median fund matched exactly and 90% matched within ±5%.

Output (`saida/`):

| file | columns |
|---|---|
| `AAAAMM.parquet` | `fundo` (CNPJ), `ativo`, `valor` (R$ that belongs to the fund after look-through) |
| `fundos_AAAAMM.parquet` | `fundo`, `pl`, `de_outros_fundos` (share of PL owned by other funds), `externo` (1 − that), `em_cotas_que_abrem`, `papel` (FIC / direct / fund-only master) |
| `total_AAAAMM.parquet` | `ativo`, `valor_total`: total held by all funds **without double counting** |
| `ativos.parquet` | `ativo`, `bloco`, `tipo`, `codigo`, `descricao`, `emissor`, `cnpj_emissor`, `vencimento` |

About 12 million rows and 20 seconds per month, plus download time (about 40–100 MB per zip).

### Double counting
Each fund's look-through is correct on its own, but **summing every fund counts a master once and again in each FIC that holds it**
(in Jul/2026: R$ 14.5 tri naive vs R$ 8.8 tri real; debentures R$ 1.53 tri vs R$ 0.82 tri).
For totals, weight each fund by the share of its PL held by outside investors: `sum(valor * externo)`, which equals `total_AAAAMM.parquet`
exactly. To see only what reaches end investors, use funds with `externo > 0` and drop the `só de fundos (master/alimentado)` ones.
Fund-of-fund stakes are opened only when the invested fund reports a positive PL.

"""Carteira explodida (look-through) de TODOS os fundos da CVM, 12 meses sem posições ocultas.

Fonte: CVM Dados Abertos, CDA (composição da carteira, mensal). Posições confidenciais ficam ocultas por ~3 meses;
o script pula os meses recentes com muita posição oculta e pega os 12 meses seguintes já abertos.
Cotas de fundos que mandam CDA são abertas (FIC dentro de FIC, quantos níveis houver); cotas de fundos sem CDA
(FIDC, FII, FIAGRO, exterior) ficam como um ativo.

Uso:  python carteira_explodida.py            (meses = 12, limite de oculto = 5%)
Saída: saida/AAAAMM.parquet        -> fundo, ativo, valor (R$ da posição que é do fundo, depois de abrir as cotas)
       saida/fundos_AAAAMM.parquet -> fundo, pl, de_outros_fundos, externo, em_cotas_que_abrem, papel
       saida/total_AAAAMM.parquet  -> ativo, valor_total (total dos fundos SEM dupla contagem)
       saida/ativos.parquet        -> ativo, bloco, tipo, codigo, descricao, emissor, cnpj_emissor, vencimento

Dupla contagem: a carteira explodida de CADA fundo está certa, mas somar todos os fundos conta o master e de novo cada FIC
que o compra. Para totais, pese cada fundo pela parte do PL que é de investidores de fora (externo = 1 - de_outros_fundos):
  total(ativo) = soma(valor x externo) = total_AAAAMM.parquet.  Para olhar só o que chega ao investidor final, use os
fundos com externo > 0 (FICs e fundos diretos), não os 'só de fundos (master/alimentado)'.
"""
import io, re, sys, zipfile
from pathlib import Path
import numpy as np, pandas as pd, requests
from scipy import sparse

URL = "https://dados.cvm.gov.br/dados/FI/DOC/CDA/DADOS/"
MESES, LIMITE_OCULTO, NIVEIS = 12, 0.05, 15
AQUI = Path(__file__).resolve().parent
ZIPS, SAIDA = AQUI / "dados", AQUI / "saida"
REN = {"CNPJ_FUNDO": "CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO_COTA": "CNPJ_FUNDO_CLASSE_COTA"}   # nomes antigos (antes da CVM 175)


def baixa(ym):
    """Baixa o zip do mês; baixa de novo se a CVM republicou (ela republica quando abre as confidenciais)."""
    f = ZIPS / f"cda_fi_{ym}.zip"
    novo = pd.Timestamp(requests.head(f"{URL}cda_fi_{ym}.zip", timeout=60).headers["Last-Modified"]).tz_localize(None)
    if not f.exists() or pd.Timestamp(f.stat().st_mtime, unit="s") < novo:
        print(f"  baixando {ym} ...", flush=True)
        f.write_bytes(requests.get(f"{URL}cda_fi_{ym}.zip", timeout=1800).content)
    return zipfile.ZipFile(f)


def le(z):
    """Todas as posições do mês (todos os blocos + confidenciais) e o PL de cada fundo."""
    ler = lambda n: pd.read_csv(z.open(n), sep=";", encoding="latin1", dtype=str, quoting=3).rename(columns=REN)
    nomes = z.namelist()
    pl = pd.concat([ler(n) for n in nomes if "_PL_" in n]).drop_duplicates("CNPJ_FUNDO_CLASSE")
    pl = pl.set_index("CNPJ_FUNDO_CLASSE").VL_PATRIM_LIQ.astype(float)
    pos = []
    for n in nomes:
        if "_PL_" in n:
            continue
        x = ler(n)
        bloco = re.search(r"BLC_\d|CONFID", n)
        x["bloco"] = ("fie_" if "cda_fie" in n else "") + (bloco.group(0) if bloco else "exterior")
        pos.append(x)
    pos = pd.concat(pos, ignore_index=True)
    pos["valor"] = pd.to_numeric(pos.VL_MERC_POS_FINAL, errors="coerce").fillna(0)
    passivo = pos.TP_APLIC.fillna("").str.contains("passiva|a pagar|obriga|lan.adas", case=False)   # passivo vem com sinal +
    pos.loc[passivo, "valor"] *= -1
    return pl, pos


def ativo_id(p):
    """Um nome por ativo: código > ISIN > título público+vencimento > descrição > emissor+vencimento."""
    t = lambda c: p[c].fillna("").str.strip() if c in p else pd.Series("", index=p.index)
    tpf = (t("TP_TITPUB") + " " + t("DT_VENC")).str.strip()
    emi = (t("CPF_CNPJ_EMISSOR") + " " + t("EMISSOR") + " " + t("DT_VENC")).str.strip()
    cota = ("COTA " + t("CNPJ_FUNDO_CLASSE_COTA") + " " + t("NM_FUNDO_CLASSE_SUBCLASSE_COTA")).str.strip()
    cod = t("CD_ATIVO").where(t("CD_ATIVO") != "", t("CD_ISIN")).where(lambda s: s != "", tpf)
    cod = cod.where(cod != "", t("DS_ATIVO")).where(lambda s: s != "", emi)
    cod = cod.where(t("CNPJ_FUNDO_CLASSE_COTA") == "", cota)
    cod = cod.where(p.bloco.str.endswith("CONFID") == False, "(confidencial)")
    return t("TP_APLIC") + " | " + cod, pd.DataFrame({"bloco": p.bloco, "tipo": t("TP_APLIC"), "codigo": cod,
                                                     "descricao": t("DS_ATIVO"), "emissor": t("EMISSOR"),
                                                     "cnpj_emissor": t("CPF_CNPJ_EMISSOR"), "vencimento": t("DT_VENC")})


def explode(pl, pos):
    """E = D + W·D + W²·D + ...  (W[i,j] = parte do fundo j que é do fundo i; D = posições diretas que não abrem)."""
    filho = pos.get("CNPJ_FUNDO_CLASSE_COTA", pd.Series(np.nan, index=pos.index))
    abre = filho.isin(pl.index[pl > 0]) & (filho != pos.CNPJ_FUNDO_CLASSE)          # PL zero/negativo: não abre (fica como ativo)
    fundos = pd.Index(sorted(set(pl.index) | set(pos.CNPJ_FUNDO_CLASSE)))
    c = pos[abre]
    W = sparse.csr_matrix(((c.valor / c[filho.name].map(pl)).values,
                           (fundos.get_indexer(c.CNPJ_FUNDO_CLASSE), fundos.get_indexer(c[filho.name]))), shape=(len(fundos),) * 2)
    d = pos[~abre]
    ids, info = ativo_id(d)
    ativos = pd.Index(ids.unique())
    D = sparse.csr_matrix((d.valor.values, (fundos.get_indexer(d.CNPJ_FUNDO_CLASSE), ativos.get_indexer(ids))),
                          shape=(len(fundos), len(ativos)))
    E, T = D.copy(), D
    for _ in range(NIVEIS):
        T = W @ T
        T.data[np.abs(T.data) < 1] = 0; T.eliminate_zeros()      # menos de R$ 1: ignora
        if T.nnz == 0:
            break
        E = E + T
    E = E.tocoo()
    out = pd.DataFrame({"fundo": fundos[E.row], "ativo": ativos[E.col], "valor": E.data})
    info = info.assign(ativo=ids.values).drop_duplicates("ativo")
    # quem é dono de quem: parte do PL de cada fundo que está com OUTROS fundos (soma da coluna de W)
    de_fundos = np.clip(np.asarray(W.sum(axis=0)).ravel(), 0, 1)
    em_cotas = c.groupby("CNPJ_FUNDO_CLASSE").valor.sum().reindex(fundos).fillna(0) / pl.reindex(fundos)
    F = pd.DataFrame({"fundo": fundos, "pl": pl.reindex(fundos).values, "de_outros_fundos": de_fundos,
                      "externo": 1 - de_fundos, "em_cotas_que_abrem": em_cotas.values})
    F["papel"] = np.select([F.de_outros_fundos >= .95, F.em_cotas_que_abrem >= .5], ["só de fundos (master/alimentado)", "FIC"],
                           "direto")
    # total do mercado sem dupla contagem = soma das posições diretas que não abrem (= soma de valor x externo)
    tot = pd.DataFrame({"ativo": ativos, "valor_total": np.asarray(D.sum(axis=0)).ravel()})
    return out[out.valor.abs() >= 1], info, F, tot[tot.valor_total.abs() >= 1]


def main():
    ZIPS.mkdir(exist_ok=True); SAIDA.mkdir(exist_ok=True)
    meses = sorted(set(re.findall(r"cda_fi_(\d{6})\.zip", requests.get(URL, timeout=60).text)), reverse=True)
    feitos, infos = [], []
    for ym in meses:
        if len(feitos) == MESES:
            break
        pl, pos = le(baixa(ym))
        oculto = pos.valor[pos.bloco.str.endswith("CONFID")].sum() / pos.valor.sum()
        if not feitos and oculto > LIMITE_OCULTO:
            print(f"{ym}: {oculto:.0%} oculto - pula"); continue
        out, info, F, tot = explode(pl, pos)
        out.to_parquet(SAIDA / f"{ym}.parquet", index=False); infos.append(info); feitos.append(ym)
        F.to_parquet(SAIDA / f"fundos_{ym}.parquet", index=False); tot.to_parquet(SAIDA / f"total_{ym}.parquet", index=False)
        print(f"{ym}: {oculto:.1%} oculto | {pl.size:,} fundos | {len(out):,} linhas explodidas", flush=True)
    pd.concat(infos).drop_duplicates("ativo").to_parquet(SAIDA / "ativos.parquet", index=False)
    print(f"pronto: {feitos[-1]} a {feitos[0]} em {SAIDA}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        MESES = int(sys.argv[1])
    main()

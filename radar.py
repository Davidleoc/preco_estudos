"""Radar de Certificações, Inglês e Pós-graduação.

Roda uma vez por dia (GitHub Actions), visita as páginas listadas em
catalogo.yaml, extrai preços/descontos/cupons, compara com o dia anterior
(estado/estado.json) e:
  - gera docs/index.html (publicado no GitHub Pages);
  - envia resumo pelo Telegram e/ou e-mail, se configurado.

Uso local:  python radar.py            (gera relatório e notifica)
            python radar.py --sem-envio (só gera o relatório)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

RAIZ = Path(__file__).parent
ARQ_ESTADO = RAIZ / "estado" / "estado.json"
ARQ_HTML = RAIZ / "docs" / "index.html"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
IOF_CARTAO = float(os.getenv("IOF_CARTAO", "0.035"))  # IOF de compra internacional no cartão
# Termos que indicam promoção de verdade (evita "promotions" de newsletter
# ou "promoção" no sentido de carreira).
PALAVRAS_PROMO = re.compile(
    r"black\s*friday|cyber\s*(week|monday)|\d{1,2}\s?%\s*(off|de desconto|discount|desconto)|"
    r"\bdesconto\b|\bcupom\b|coupon code|promo code|\bsale\b|oferta por tempo|limited[- ]time|"
    r"save (up to )?(\$|\d)",
    re.I,
)
CATEGORIAS = {
    "certificacao": {"icone": "🎓", "titulo": "Certificações",
                     "descricao": "Provas que comprovam conhecimento técnico. Preço em dólar, voucher vale 12 meses."},
    "ingles": {"icone": "🗣️", "titulo": "Inglês",
               "descricao": "Aulas e planos de conversação. Todas as provas acima são em inglês."},
    "pos": {"icone": "🏛️", "titulo": "Pós-graduação",
            "descricao": "MBAs e especializações EAD. Exigem diploma de graduação."},
}
HOJE = dt.date.today()


# ------------------------------------------------------------------ coleta

def baixar(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def textos(conteudo_html: str) -> tuple[str, str]:
    """Devolve (texto visível, texto visível + textos embutidos em JavaScript).

    Sites em Next.js (ex.: Cambly) mandam parte do conteúdo, como preços,
    dentro de <script> no formato "children":"...". Esses textos entram no
    segundo valor para que os regex do catálogo também os encontrem.
    """
    sopa = BeautifulSoup(conteudo_html, "html.parser")
    for tag in sopa(["script", "style", "noscript", "svg"]):
        tag.decompose()
    visivel = re.sub(r"\s+", " ", sopa.get_text(" ", strip=True))
    embutidos = re.findall(r'"children"\s*:\s*"([^"]{1,200})"', conteudo_html.replace('\\"', '"'))
    return visivel, visivel + " " + " ".join(embutidos)


def trechos_promocionais(texto: str, limite: int = 4) -> list[str]:
    """Frases curtas da página que falam de desconto/promoção."""
    achados: list[str] = []
    for m in PALAVRAS_PROMO.finditer(texto):
        ini = max(0, m.start() - 90)
        fim = min(len(texto), m.end() + 110)
        trecho = texto[ini:fim].strip()
        # descarta trechos muito parecidos com um já escolhido
        if any(trecho[:60] in a or a[:60] in trecho for a in achados):
            continue
        achados.append(("…" if ini else "") + trecho + ("…" if fim < len(texto) else ""))
        if len(achados) >= limite:
            break
    return achados


def coletar_fonte(fonte: dict) -> dict:
    resultado = {"url": fonte["url"], "campos": {}, "trechos": [], "erro": None}
    try:
        texto, completo = textos(baixar(fonte["url"]))
    except Exception as e:  # noqa: BLE001 - qualquer falha vira aviso no relatório
        resultado["erro"] = f"{type(e).__name__}: {e}"[:200]
        return resultado
    for nome, regra in (fonte.get("campos") or {}).items():
        # regra pode ser só o regex, ou {regex: ..., todos: true} para juntar
        # todas as ocorrências (ex.: vários planos na mesma página)
        padrao, todos = (regra["regex"], regra.get("todos")) if isinstance(regra, dict) else (regra, False)
        alvo = texto if re.search(padrao, texto, re.I) else completo
        achados = [re.sub(r"\s+", " ", m.group(1)).strip() for m in re.finditer(padrao, alvo, re.I)]
        achados = list(dict.fromkeys(achados))
        resultado["campos"][nome] = (" | ".join(achados) if todos else achados[0]) if achados else None
    resultado["trechos"] = trechos_promocionais(texto)
    return resultado


def coletar_cupons_lf(url: str) -> dict:
    """Lê o README do agregador techiescamp e devolve as promoções ativas."""
    try:
        md = baixar(url)
    except Exception as e:  # noqa: BLE001
        return {"erro": f"{type(e).__name__}: {e}"[:200], "promocoes": [], "titulo": ""}
    titulo = md.splitlines()[0].lstrip("# ").strip() if md else ""
    promocoes = []
    for bloco in md.split("\n## ")[1:]:
        linhas = bloco.splitlines()
        nome = linhas[0].strip()
        corpo = "\n".join(linhas[1:])
        codigos = [
            c for c in dict.fromkeys(re.findall(r"\*\*([A-Z0-9]{5,})\*\*", corpo))
            if not cupom_antigo(c)
        ]
        if not codigos:
            continue
        resumo = next(
            (l.strip() for l in linhas[1:] if l.strip() and not l.startswith((">", "-", "#"))),
            "",
        )
        prazo = next((re.sub(r"^>\s*", "", l).strip() for l in linhas if re.search(r"ends|expires|valid", l, re.I)), "")
        sem_md = lambda t: re.sub(r"\*\*|\[!\w+\]|\[([^\]]+)\]\([^)]+\)", r"", t).strip()
        promocoes.append({"nome": nome, "codigos": codigos, "resumo": sem_md(resumo), "prazo": sem_md(prazo)})
    # o agregador lista as promoções atuais no topo; as de baixo costumam estar vencidas
    return {"erro": None, "promocoes": promocoes[:5], "titulo": titulo}


def cupom_antigo(codigo: str) -> bool:
    """Cupons da LF costumam terminar em <ano><sufixo>, ex.: LEAP24COM, OCT26CT."""
    m = re.search(r"(\d{2})(COM|CT|ILT|CD|BUNCT|SBCT|CCCT)$", codigo)
    return bool(m) and int(m.group(1)) < HOJE.year % 100


def cotacao_dolar() -> float | None:
    try:
        return float(requests.get("https://open.er-api.com/v6/latest/USD", timeout=20).json()["rates"]["BRL"])
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ comparação

def assinatura(dados) -> str:
    return hashlib.sha1(json.dumps(dados, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def comparar(itens: list[dict], cupons: dict, anterior: dict) -> list[str]:
    novidades: list[str] = []
    for item in itens:
        for i, fonte in enumerate(item["coleta"]):
            chave = f"{item['id']}#{i}"
            antes = anterior.get("fontes", {}).get(chave)
            if not antes or fonte["erro"]:
                continue
            for campo, valor in fonte["campos"].items():
                velho = antes.get("campos", {}).get(campo)
                if valor and velho and valor != velho:
                    novidades.append(f"{icone_campo(campo)} {item['nome']}: {rotulo(campo)} mudou de {velho} para {valor}")
            if assinatura(fonte["trechos"]) != assinatura(antes.get("trechos", [])) and fonte["trechos"]:
                novidades.append(f"📰 {item['nome']}: texto de promoção na página mudou")
    codigos_antes = set(anterior.get("cupons_lf", []))
    if codigos_antes:
        for p in cupons["promocoes"]:
            novos = [c for c in p["codigos"] if c not in codigos_antes]
            if novos:
                novidades.append(f"🎟️ Novo cupom Linux Foundation: {', '.join(novos)} — {p['nome']}")
    return novidades


def atualizar_historico(itens: list[dict], anterior: dict) -> dict:
    hist = anterior.get("historico", {})
    for item in itens:
        for i, fonte in enumerate(item["coleta"]):
            for campo, valor in fonte["campos"].items():
                if not valor:
                    continue
                serie = hist.setdefault(f"{item['id']}#{i}.{campo}", [])
                if not serie or serie[-1]["valor"] != valor:
                    serie.append({"data": HOJE.isoformat(), "valor": valor})
                del serie[:-90]
    return hist


# ------------------------------------------------------------------ apresentação

def rotulo(campo: str) -> str:
    return {
        "preco_prova_usd": "preço da prova (US$)",
        "parcela_brl": "parcela a partir de (R$)",
        "parcelas": "nº de parcelas",
        "banner_promo": "banner do site",
        "desconto": "desconto anunciado",
        "planos_de_por": "Grupos | Private+ | Pro (de → por)",
    }.get(campo, campo.replace("_", " "))


def em_reais(usd: float | None, dolar: float | None) -> str:
    if usd is None or not dolar:
        return ""
    total = usd * dolar * (1 + IOF_CARTAO)
    return f"≈ R$ {total:,.0f}".replace(",", ".")


def proximas_datas(cfg: dict) -> list[tuple[int, str, dt.date]]:
    saida = []
    for d in cfg.get("datas_importantes", []):
        data = d["data"] if isinstance(d["data"], dt.date) else dt.date.fromisoformat(str(d["data"]))
        dias = (data - HOJE).days
        if dias >= -1:
            saida.append((dias, d["nome"], data))
    return sorted(saida)[:4]


def preco_atual_usd(item: dict) -> float | None:
    for fonte in item["coleta"]:
        v = fonte["campos"].get("preco_prova_usd")
        if v:
            return float(v)
    return item.get("preco_referencia_usd")


def esc(t) -> str:
    return html.escape(str(t)) if t is not None else ""


def lista_html(icone: str, titulo: str, itens) -> str:
    if not itens:
        return ""
    lis = "".join(f"<li>{esc(i)}</li>" for i in itens)
    return f"<div class='bloco'><h4>{icone} {esc(titulo)}</h4><ul>{lis}</ul></div>"


# Linhas da ficha de cada item: (chave no catálogo, ícone, rótulo)
FICHA = [
    ("formato", "🧪", "Formato"), ("duracao", "⏱️", "Duração"), ("aprovacao", "✅", "Aprovação"),
    ("validade", "📅", "Validade"), ("idioma", "🌐", "Idioma"),
    ("estudo_estimado", "📚", "Tempo de estudo"), ("voce_ja_tem", "💪", "Você já tem"),
]


def chips_item(item: dict) -> str:
    chips = []
    if item.get("prioridade"):
        chips.append(f"<span class='chip destaque'>⭐ Prioridade {item['prioridade']}</span>")
    if item.get("emissor"):
        chips.append(f"<span class='chip'>🏢 {esc(item['emissor'])}</span>")
    formato = (item.get("formato") or "").lower()
    if "prática" in formato:
        chips.append("<span class='chip'>🖥️ Prova prática</span>")
    elif "múltipla" in formato:
        chips.append("<span class='chip'>📝 Múltipla escolha</span>")
    elif "ao vivo" in formato:
        chips.append("<span class='chip'>🎥 Online ao vivo</span>")
    elif "ead" in formato:
        chips.append("<span class='chip'>💻 EAD</span>")
    return "".join(chips)


def icone_campo(campo: str) -> str:
    if "desconto" in campo or "banner" in campo:
        return "🏷️"
    if "parcela" in campo:
        return "💳"
    return "💲"


def icone_data(nome: str) -> str:
    n = nome.lower()
    if "black friday" in n:
        return "🛍️"
    if "cyber" in n:
        return "💻"
    if "cupom" in n:
        return "🎟️"
    return "📅"


def card_item(item: dict, dolar: float | None) -> str:
    cat = CATEGORIAS[item["categoria"]]
    linhas = [
        f"<tr><th>{ic} {nome}</th><td>{esc(item[chave])}</td></tr>"
        for chave, ic, nome in FICHA if item.get(chave)
    ]
    preco = ""
    usd = preco_atual_usd(item) if item["categoria"] == "certificacao" else None
    if usd:
        do_site = any(f["campos"].get("preco_prova_usd") for f in item["coleta"])
        selo = ("<span class='selo ok'>✔ lido do site hoje</span>" if do_site
                else "<span class='selo'>ℹ valor de referência</span>")
        preco = (f"<div class='preco'><span class='preco-ic'>💲</span><div><b>US$ {usd:g}</b>"
                 f"<span class='brl'>{em_reais(usd, dolar)} com IOF</span></div>{selo}</div>")
    capturas = []
    for fonte in item["coleta"]:
        if fonte["erro"]:
            capturas.append(f"<li class='erro'>⚠️ Não consegui ler <a href='{esc(fonte['url'])}'>a página</a>: {esc(fonte['erro'])}</li>")
            continue
        for campo, valor in fonte["campos"].items():
            if campo == "preco_prova_usd":
                continue
            v = esc(valor) if valor else "<span class='erro'>não encontrado (a página pode ter mudado)</span>"
            capturas.append(f"<li>{icone_campo(campo)} <b>{esc(rotulo(campo))}:</b> {v}</li>")
        capturas += [f"<li class='trecho'>💬 “{esc(t)}”</li>" for t in fonte["trechos"]]
    bloco_capturas = (f"<div class='visto'><h4>👀 Visto no site hoje</h4><ul class='sem-marca'>{''.join(capturas)}</ul></div>"
                      if capturas else "")
    return f"""
<article class="card cat-{item['categoria']}">
  <header>
    <span class="avatar">{cat['icone']}</span>
    <div class="titulo"><h3>{esc(item['nome'])}</h3><div class="chips">{chips_item(item)}</div></div>
  </header>
  {preco}
  <table>{''.join(linhas)}</table>
  {bloco_capturas}
  <details><summary>📖 Como funciona, o que estudar e material</summary>
    {lista_html('⚙️', 'Como funciona', item.get('como_funciona'))}
    {lista_html('🎯', 'O que estudar (lacunas prováveis)', item.get('lacunas'))}
    {lista_html('⚠️', 'Pontos de atenção', item.get('atencao'))}
    {lista_html('🧰', 'Material', item.get('material'))}
  </details>
  <a class="botao" href="{esc(item.get('link_oficial', ''))}" target="_blank" rel="noopener">🔗 Site oficial</a>
</article>"""


CSS = """
:root{--bg:#f4f6fa;--card:#fff;--txt:#1b1f24;--muted:#5d6670;--line:#e2e5e9;--acc:#1f6feb;--ok:#1a7f37;--okbg:#e6f4ea;--warn:#b35900;--warnbg:#fff4e5;--chip:#eef2f7;
--cert:#1f6feb;--certbg:#e8f0fe;--ing:#8250df;--ingbg:#f3ecff;--pos:#bf3989;--posbg:#fdeef6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1216;--card:#171b21;--txt:#e6e9ed;--muted:#9aa4af;--line:#2a313a;--acc:#58a6ff;--ok:#3fb950;--okbg:#12261a;--warn:#e3a043;--warnbg:#2b2112;--chip:#212833;
--cert:#58a6ff;--certbg:#132238;--ing:#b392f0;--ingbg:#231a33;--pos:#f778ba;--posbg:#2e1525}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1150px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.7rem;margin:0 0 4px}
.muted{color:var(--muted);font-size:.9em}a{color:var(--acc)}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}nav a{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 12px;text-decoration:none;color:var(--txt);font-size:.9em}
.secao{display:flex;align-items:center;gap:12px;margin:36px 0 12px}.secao .ic{font-size:1.6rem;width:46px;height:46px;flex:none;border-radius:12px;display:grid;place-items:center;background:var(--card);border:1px solid var(--line)}
.secao h2{margin:0;font-size:1.25rem}.secao p{margin:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:8px}
.kpi,.card,.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi{display:flex;gap:12px;align-items:center}.kpi .ic{font-size:1.8rem}.kpi b{display:block;font-size:1.3rem;line-height:1.2}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{display:flex;flex-direction:column;gap:10px;border-top:4px solid var(--c,var(--line))}
.cat-certificacao{--c:var(--cert);--cbg:var(--certbg)}.cat-ingles{--c:var(--ing);--cbg:var(--ingbg)}.cat-pos{--c:var(--pos);--cbg:var(--posbg)}
.card header{display:flex;gap:12px;align-items:flex-start}.avatar{font-size:1.5rem;width:44px;height:44px;flex:none;border-radius:50%;display:grid;place-items:center;background:var(--cbg)}
.card h3{margin:0 0 6px;font-size:1.02rem;line-height:1.3}.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--chip);border-radius:999px;padding:1px 9px;font-size:.78em;white-space:nowrap}.chip.destaque{background:var(--cbg);color:var(--c);font-weight:600}
.preco{display:flex;align-items:center;gap:10px;background:var(--cbg);border-radius:10px;padding:8px 12px;flex-wrap:wrap}
.preco-ic{font-size:1.4rem}.preco b{font-size:1.25rem;display:block;line-height:1.1}.brl{font-size:.88em;color:var(--muted)}
.selo{margin-left:auto;font-size:.75em;background:var(--card);border-radius:999px;padding:2px 8px;color:var(--muted)}.selo.ok{color:var(--ok)}
table{border-collapse:collapse;width:100%;font-size:.92em}th{text-align:left;color:var(--muted);font-weight:500;padding:3px 10px 3px 0;vertical-align:top;white-space:nowrap}td{padding:3px 0}
h4{margin:10px 0 4px;font-size:.9rem}ul{margin:0;padding-left:20px}li{margin:3px 0}ul.sem-marca{list-style:none;padding-left:0}
.visto{background:var(--chip);border-radius:10px;padding:4px 12px 8px}.trecho{color:var(--muted);font-size:.86em}
.erro{color:var(--warn)}details summary{cursor:pointer;color:var(--acc);font-weight:500}.bloco{margin-top:6px}
.botao{margin-top:auto;align-self:flex-start;text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:5px 12px;font-size:.88em}
.novidade{list-style:none;padding:0;margin:0}.novidade li{padding:6px 0;border-bottom:1px solid var(--line)}.novidade li:last-child{border:0}
.vazio{color:var(--ok);background:var(--okbg);border-radius:8px;padding:8px 12px;margin:0}
code{background:var(--warnbg);color:var(--warn);padding:2px 8px;border-radius:6px;font-weight:700;font-size:.95em}
.promo{display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--line)}.promo:first-child{border-top:0}.promo .ic{font-size:1.4rem}
.legenda{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:.85em;color:var(--muted);margin-top:8px}
@media (max-width:420px){.grid{grid-template-columns:1fr}th{white-space:normal}}
"""


def secao(id_: str, icone: str, titulo: str, descricao: str) -> str:
    return (f"<div class='secao' id='{id_}'><span class='ic'>{icone}</span>"
            f"<div><h2>{esc(titulo)}</h2><p class='muted'>{esc(descricao)}</p></div></div>")


def reais(valor: float) -> str:
    return f"R$ {valor:.2f}".replace(".", ",")


def gerar_html(cfg: dict, itens: list[dict], cupons: dict, novidades: list[str], dolar: float | None) -> str:
    agora = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
    datas = "".join(
        f"<div class='kpi'><span class='ic'>{icone_data(nome)}</span><div><span class='muted'>{esc(nome)}</span>"
        f"<b>{'hoje' if dias == 0 else ('ontem' if dias < 0 else f'em {dias} dias')}</b>"
        f"<span class='muted'>{data.strftime('%d/%m/%Y')}</span></div></div>"
        for dias, nome, data in proximas_datas(cfg)
    )
    kpi_dolar = (f"<div class='kpi'><span class='ic'>💵</span><div><span class='muted'>Dólar hoje com IOF</span>"
                 f"<b>{reais(dolar * (1 + IOF_CARTAO))}</b><span class='muted'>comercial {reais(dolar)}</span></div></div>"
                 ) if dolar else ""
    bloco_nov = (
        "<ul class='novidade'>" + "".join(f"<li>{esc(n)}</li>" for n in novidades) + "</ul>"
        if novidades else "<p class='vazio'>✅ Nenhuma mudança desde a última verificação.</p>"
    )
    banners = {f["campos"].get("banner_promo") for i in itens for f in i["coleta"] if f["campos"].get("banner_promo")}
    bloco_banner = "".join(f"<div class='promo'><span class='ic'>📣</span><div><b>Banner no site oficial da Linux Foundation</b>"
                           f"<br>{esc(b)}</div></div>" for b in banners)
    promos = "".join(
        f"<div class='promo'><span class='ic'>🎟️</span><div><b>{esc(p['nome'])}</b><br>"
        f"Cupom: {' '.join(f'<code>{esc(c)}</code>' for c in p['codigos'])}"
        f"<br><span class='muted'>{esc(p['resumo'])} {('⏰ ' + esc(p['prazo'])) if p['prazo'] else ''}</span></div></div>"
        for p in cupons["promocoes"]
    ) or f"<p class='erro'>⚠️ Não consegui ler os cupons: {esc(cupons.get('erro'))}</p>"
    secoes = ""
    for cat, info in CATEGORIAS.items():
        cards = "".join(card_item(i, dolar) for i in sorted(
            (i for i in itens if i["categoria"] == cat), key=lambda x: x.get("prioridade", 99)))
        secoes += secao(cat, info["icone"], info["titulo"], info["descricao"]) + f"<div class='grid'>{cards}</div>"
    nav = "".join(f"<a href='#{a}'>{t}</a>" for a, t in [
        ("novidades", "🆕 Novidades"), ("cupons", "🎟️ Cupons"),
        *((c, f"{i['icone']} {i['titulo']}") for c, i in CATEGORIAS.items())])
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar de Certificações</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📡</text></svg>">
<style>{CSS}</style></head><body><main>
<h1>📡 Radar de Certificações</h1>
<div class="muted">🕗 Atualizado em {agora} (horário de Brasília) · 👤 {esc(cfg['perfil']['resumo'])}</div>
<nav>{nav}</nav>
<div class="kpis">{kpi_dolar}{datas}</div>
{secao('novidades', '🆕', 'Novidades de hoje', 'O que mudou desde a verificação anterior: preço, cupom ou texto de promoção.')}
<div class="box">{bloco_nov}</div>
{secao('cupons', '🎟️', 'Cupons Linux Foundation ativos', 'Códigos de desconto para CKA, LFCS, PCA e bundles. Use no checkout do site da Linux Foundation.')}
<div class="box">{bloco_banner}{promos}
<p class="muted">Fonte: <a href="{esc(cfg['cupons_linux_foundation']['pagina'])}">{esc(cupons.get('titulo') or 'agregador')}</a>.
Confirme o desconto no checkout antes de pagar.</p></div>
{secoes}
<div class="box" style="margin-top:32px"><b>🔎 Como ler os ícones</b>
<div class="legenda"><span>💲 preço</span><span>✔ lido do site hoje</span><span>ℹ valor de referência (o site não mostra preço)</span>
<span>🏷️ desconto</span><span>💳 parcelamento</span><span>👀 capturado do site hoje</span><span>💬 trecho de promoção</span>
<span>🖥️ prova prática</span><span>📝 múltipla escolha</span><span>⭐ ordem sugerida</span><span>⚠️ ponto de atenção</span></div>
<p class="muted">Preços em reais = dólar comercial × (1 + IOF de {IOF_CARTAO:.1%}). A fatura usa a cotação do dia do pagamento.
Formato e tempo de estudo vêm do catalogo.yaml. Confira sempre a página oficial antes de comprar.</p></div>
</main></body></html>"""


def gerar_texto_telegram(cupons: dict, itens: list[dict], novidades: list[str], dolar: float | None, url_pages: str, cfg: dict) -> str:
    l = [f"<b>📡 Radar de Certificações — {HOJE.strftime('%d/%m/%Y')}</b>"]
    if dolar:
        l.append(f"💵 Dólar + IOF: {reais(dolar * (1 + IOF_CARTAO))}")
    for dias, nome, _ in proximas_datas(cfg)[:3]:
        l.append(f"{icone_data(nome)} {esc(nome)}: {'hoje' if dias == 0 else f'em {dias} dias'}")
    l.append("\n<b>🆕 Novidades</b>")
    l += [f"• {esc(n)}" for n in novidades[:10]] or ["✅ Nenhuma mudança desde ontem"]
    banners = {f["campos"].get("banner_promo") for i in itens for f in i["coleta"] if f["campos"].get("banner_promo")}
    l.append("\n<b>🎟️ Cupons Linux Foundation</b>")
    l += [f"📣 Site oficial: {esc(b)}" for b in banners]
    for p in cupons["promocoes"][:4]:
        l.append(f"• {esc(p['nome'])}: <code>{esc(', '.join(p['codigos']))}</code>")
    l.append("\n<b>🎓 Preços das provas</b>  (ℹ = valor de referência)")
    for i in sorted((i for i in itens if i["categoria"] == "certificacao"), key=lambda x: x.get("prioridade", 99)):
        usd = preco_atual_usd(i)
        if usd:
            ref = "" if any(f["campos"].get("preco_prova_usd") for f in i["coleta"]) else " ℹ"
            l.append(f"• {esc(i['nome'].split(' — ')[0])}: US$ {usd:g}{ref} {em_reais(usd, dolar)}")
    for cat in ("ingles", "pos"):
        info = CATEGORIAS[cat]
        linhas = []
        for i in (i for i in itens if i["categoria"] == cat):
            for f in i["coleta"]:
                vals = [f"{icone_campo(k)} {v}" for k, v in f["campos"].items() if v]
                if vals:
                    linhas.append(f"• {esc(i['nome'])}: {esc('  '.join(vals))}")
        if linhas:
            l.append(f"\n<b>{info['icone']} {info['titulo']}</b>")
            l += linhas
    if url_pages:
        l.append(f"\n🔗 <a href=\"{esc(url_pages)}\">Ver relatório completo</a>")
    return "\n".join(l)[:4000]


# ------------------------------------------------------------------ envio

def enviar_telegram(texto: str) -> None:
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("Telegram não configurado (TELEGRAM_TOKEN/TELEGRAM_CHAT_ID). Pulando.")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
        "chat_id": chat, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    print("Telegram:", r.status_code, r.text[:200] if r.status_code != 200 else "ok")


def enviar_email(assunto: str, corpo_html: str) -> None:
    host, user, senha, para = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (host and user and senha and para):
        print("E-mail não configurado (SMTP_HOST/SMTP_USER/SMTP_PASS/EMAIL_TO). Pulando.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = assunto, user, para
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        s.login(user, senha)
        s.sendmail(user, [p.strip() for p in para.split(",")], msg.as_string())
    print("E-mail enviado para", para)


# ------------------------------------------------------------------ main

def main() -> int:
    enviar = "--sem-envio" not in sys.argv
    cfg = yaml.safe_load((RAIZ / "catalogo.yaml").read_text(encoding="utf-8"))
    anterior = json.loads(ARQ_ESTADO.read_text(encoding="utf-8")) if ARQ_ESTADO.exists() else {}

    itens = cfg["itens"]
    for item in itens:
        item["coleta"] = [coletar_fonte(f) for f in item.get("fontes", [])]
        for f in item["coleta"]:
            status = f"ERRO {f['erro']}" if f["erro"] else f"{f['campos']} | {len(f['trechos'])} trechos"
            print(f"[{item['id']}] {status}")
    cupons = coletar_cupons_lf(cfg["cupons_linux_foundation"]["url"])
    print(f"[cupons] {len(cupons['promocoes'])} promoções {cupons['erro'] or ''}")
    dolar = cotacao_dolar()

    novidades = comparar(itens, cupons, anterior)
    estado = {
        "atualizado_em": HOJE.isoformat(),
        "fontes": {f"{i['id']}#{n}": {k: f[k] for k in ("campos", "trechos")}
                   for i in itens for n, f in enumerate(i["coleta"]) if not f["erro"]},
        "cupons_lf": sorted({c for p in cupons["promocoes"] for c in p["codigos"]}) or anterior.get("cupons_lf", []),
        "historico": atualizar_historico(itens, anterior),
        "dolar": dolar,
    }
    ARQ_ESTADO.parent.mkdir(exist_ok=True)
    ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")

    pagina = gerar_html(cfg, itens, cupons, novidades, dolar)
    ARQ_HTML.parent.mkdir(exist_ok=True)
    ARQ_HTML.write_text(pagina, encoding="utf-8")
    print(f"Relatório: {ARQ_HTML} | {len(novidades)} novidade(s)")

    if enviar:
        so_mudancas = os.getenv("NOTIFICAR_SO_MUDANCAS", "false").lower() == "true"
        if so_mudancas and not novidades:
            print("Sem novidades e NOTIFICAR_SO_MUDANCAS=true. Não vou enviar.")
            return 0
        url_pages = os.getenv("URL_PAGES", "")
        enviar_telegram(gerar_texto_telegram(cupons, itens, novidades, dolar, url_pages, cfg))
        assunto = f"Radar de Certificações {HOJE.strftime('%d/%m')}" + (f" — {len(novidades)} novidade(s)" if novidades else "")
        enviar_email(assunto, pagina)
    return 0


if __name__ == "__main__":
    sys.exit(main())

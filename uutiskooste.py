#!/usr/bin/env python3
"""
Uutiskooste – prototyyppi.

Hakee uutisotsikot RSS-syötteistä, niputtaa saman uutisen eri medioista
yhdeksi, kirjoittaa jokaiselle asiallisen otsikon ja lyhyen tiivistelmän
Googlen Gemini-API:lla (ilmaistaso) ja tekee tuloksesta HTML-sivun.

Ei riippuvuuksia – pelkkä Python 3.9+.

Käyttö:
    export GEMINI_API_KEY=...              # https://aistudio.google.com/apikey
    python3 uutiskooste.py                 # viimeiset 24 h
    python3 uutiskooste.py --tunnit 6      # viimeiset 6 h
    python3 uutiskooste.py --ulos site/index.html --ei-avausta
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# --- Asetukset -------------------------------------------------------------

# Lisää tai poista syötteitä vapaasti. Toimimaton syöte ohitetaan.
# Saman median useampi syöte voi käyttää samaa nimeä (klikkimittari yhdistää ne).
SYOTTEET = [
    ("Yle", "https://yle.fi/rss/uutiset/paauutiset"),
    ("Yle", "https://yle.fi/rss/uutiset/tuoreimmat"),
    ("Helsingin Sanomat", "https://www.hs.fi/rss/tuoreimmat.xml"),
    ("Ilta-Sanomat", "https://www.is.fi/rss/tuoreimmat.xml"),
    ("Iltalehti", "https://www.iltalehti.fi/rss/uutiset.xml"),
]

# Mallit kokeillaan järjestyksessä: jos ensimmäinen on ruuhkassa tai poistunut,
# siirrytään seuraavaan. Kaikilla on Gemini-API:n ilmaistaso.
MALLIT = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
MAX_UUTISIA = 120                  # montako otsikkoa lähetetään mallille
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{malli}:generateContent"

# --- RSS-haku ----------------------------------------------------------------

TAGI_RE = re.compile(r"<[^>]+>")


def siisti(teksti):
    if not teksti:
        return ""
    teksti = TAGI_RE.sub(" ", teksti)
    teksti = html.unescape(teksti)
    return re.sub(r"\s+", " ", teksti).strip()


def lue_aika(arvo):
    if not arvo:
        return None
    try:
        dt = parsedate_to_datetime(arvo)           # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(arvo.replace("Z", "+00:00"))  # Atom
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


MEDIA_NS = "{http://search.yahoo.com/mrss/}"


def etsi_kuva(item):
    """Etsii uutisen pikkukuvan RSS:n enclosure- tai media-kentistä."""
    for enc in item.findall("enclosure"):
        if enc.get("type", "").startswith("image") and enc.get("url"):
            return enc.get("url")
    for tag in ("thumbnail", "content"):
        for m in item.iter(MEDIA_NS + tag):
            url = m.get("url")
            if url and (tag == "thumbnail" or m.get("medium") == "image"
                        or m.get("type", "").startswith("image")
                        or re.search(r"\.(jpe?g|png|webp)", url, re.I)):
                return url
    return ""


def hae_syote(nimi, url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Uutiskooste/0.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except Exception as e:
        print(f"  ! {nimi}: haku epäonnistui ({e})", file=sys.stderr)
        return []
    try:
        juuri = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"  ! {nimi}: syöte ei ole kelvollista XML:ää ({e})", file=sys.stderr)
        return []

    uutiset = []
    # RSS 2.0
    for item in juuri.iter("item"):
        uutiset.append({
            "lahde": nimi,
            "otsikko": siisti(item.findtext("title")),
            "kuvaus": siisti(item.findtext("description"))[:600],
            "linkki": (item.findtext("link") or "").strip(),
            "aika": lue_aika(item.findtext("pubDate")),
            "kuva": etsi_kuva(item),
        })
    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in juuri.findall("a:entry", ns):
        linkki = e.find("a:link", ns)
        uutiset.append({
            "lahde": nimi,
            "otsikko": siisti(e.findtext("a:title", namespaces=ns)),
            "kuvaus": siisti(e.findtext("a:summary", namespaces=ns)
                             or e.findtext("a:content", namespaces=ns))[:600],
            "linkki": linkki.get("href", "") if linkki is not None else "",
            "aika": lue_aika(e.findtext("a:updated", namespaces=ns)
                             or e.findtext("a:published", namespaces=ns)),
            "kuva": etsi_kuva(e),
        })
    print(f"  {nimi}: {len(uutiset)} uutista")
    return uutiset


def hae_kaikki(tunnit):
    raja = datetime.now(timezone.utc) - timedelta(hours=tunnit)
    with ThreadPoolExecutor(max_workers=8) as ex:
        tulokset = ex.map(lambda kv: hae_syote(*kv), SYOTTEET)
    kaikki, nahdyt = [], set()
    for lista in tulokset:
        for u in lista:
            avain = u["linkki"] or u["otsikko"]
            if not u["otsikko"] or avain in nahdyt:
                continue
            if u["aika"] and u["aika"] < raja:
                continue
            nahdyt.add(avain)
            kaikki.append(u)
    kaikki.sort(key=lambda u: u["aika"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)
    return kaikki[:MAX_UUTISIA]


# --- Tiivistys Geminin avulla ---------------------------------------------

OHJE = """Olet uutistoimittaja. Saat listan suomalaisten medioiden uutisia
(otsikko + ingressi). Monet otsikot ovat klikkiotsikoita, jotka piilottavat asian.

Tehtäväsi:
1. Niputa samaa tapahtumaa käsittelevät uutiset yhdeksi jutuksi.
2. Kirjoita jokaiselle jutulle asiallinen, informatiivinen otsikko, joka kertoo
   suoraan mitä tapahtui (ei kysymyksiä, ei arvoituksia, ei "katso kuvat").
3. Kirjoita 1–3 lauseen tiivistelmä VAIN annettujen tietojen perusteella.
   Jos ingressi ei kerro asiaa, sano se suoraan (esim. "Otsikosta ei selviä,
   mitä tapahtui.") äläkä arvaa.
4. Anna luokka: Kotimaa, Ulkomaat, Talous, Politiikka, Urheilu, Kulttuuri,
   Tiede ja teknologia, Viihde tai Muut.
5. Anna tärkeys 1–5 (5 = laajasti merkittävä uutinen, 1 = viihde/juoru).
6. Arvioi JOKAINEN annettu uutinen erikseen: onko sen otsikko klikkiotsikko?
   Klikkiotsikko piilottaa asian, kiusoittelee, liioittelee tai johtaa harhaan
   (esim. "Et usko mitä tapahtui", "Tämä yllätti kaikki", pelkkä sitaatti
   ilman asiayhteyttä). Asiallinen otsikko kertoo mitä tapahtui.
   Listaa klikkiotsikoiden id:t kenttään "klikkiotsikot".

Kirjoita lopuksi 3–5 lauseen yleiskatsaus päivän tärkeimmistä uutisista.

Vastaa PELKKÄNÄ JSONina tässä muodossa, ei muuta tekstiä:
{
  "yleiskatsaus": "...",
  "jutut": [
    {"otsikko": "...", "tiivistelma": "...", "luokka": "...",
     "tarkeys": 3, "uutiset": [0, 4]}
  ],
  "klikkiotsikot": [4, 7]
}
Kentässä "uutiset" ovat alkuperäisten uutisten id-numerot."""


def kutsu_gemini(uutiset, mallit, avain):
    rivit = [f"[{i}] ({u['lahde']}) {u['otsikko']}\n    {u['kuvaus']}"
             for i, u in enumerate(uutiset)]
    runko = json.dumps({
        "systemInstruction": {"parts": [{"text": OHJE}]},
        "contents": [{"role": "user",
                      "parts": [{"text": "Uutiset:\n\n" + "\n".join(rivit)}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.3},
    }).encode()
    viimeisin = None
    for malli in mallit:
        req = urllib.request.Request(
            API_URL.format(malli=malli), data=runko,
            headers={"x-goog-api-key": avain, "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                vastaus = json.load(r)
            osat = vastaus["candidates"][0]["content"]["parts"]
            teksti = "".join(o.get("text", "") for o in osat)
            print(f"  käytetty malli: {malli}")
            return pura_json(teksti)
        except urllib.error.HTTPError as e:
            viimeisin = f"{malli}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}"
        except (KeyError, IndexError, json.JSONDecodeError, OSError) as e:
            viimeisin = f"{malli}: {e}"
        print(f"  ! {viimeisin}", file=sys.stderr)
    sys.exit(f"Tiivistys epäonnistui kaikilla malleilla. Viimeisin virhe: {viimeisin}")


def pura_json(teksti):
    teksti = teksti.strip()
    teksti = re.sub(r"^```(?:json)?\s*|\s*```$", "", teksti)
    try:
        return json.loads(teksti)
    except json.JSONDecodeError:
        alku, loppu = teksti.find("{"), teksti.rfind("}")
        if alku >= 0 and loppu > alku:
            return json.loads(teksti[alku:loppu + 1])
        raise


# --- HTML-sivu ---------------------------------------------------------------

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e4e2dd;
--accent:#0b6e4f;--warn:#b54708;--warnbg:#fff4e5;--bar:#ecebe7}
@media(prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#ececee;
--muted:#9a9aa1;--line:#2c2c30;--accent:#4cc39a;--warn:#f5a35c;--warnbg:#3a2a1a;--bar:#2a2a2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:28px;margin:0 0 4px}.paiva{color:var(--muted);margin:0 0 20px}
.laatikko{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin-bottom:16px}
.laatikko h3{font-size:15px;margin:0 0 12px}
.mittari{display:grid;grid-template-columns:auto 1fr auto;gap:8px 10px;align-items:center;font-size:14px}
.palkki{height:10px;background:var(--bar);border-radius:5px;overflow:hidden}
.palkki i{display:block;height:100%;background:var(--warn);border-radius:5px}
.luku{color:var(--muted);font-variant-numeric:tabular-nums;text-align:right}
.selite{font-size:12px;color:var(--muted);margin:10px 0 0}
.suodattimet{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}
.suodattimet button{border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:999px;padding:6px 12px;font-size:14px;cursor:pointer}
.suodattimet button.on{background:var(--accent);border-color:var(--accent);color:#fff}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;
margin-bottom:12px;overflow:hidden}
article .kuva{display:block;width:100%;aspect-ratio:2/1;object-fit:cover;background:var(--bar)}
article .sisus{padding:16px 18px}
article h2{font-size:18px;margin:0 0 6px;line-height:1.3}
article p{margin:0 0 10px}
.meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px;color:var(--muted);margin-bottom:8px}
.tag{border:1px solid var(--line);border-radius:6px;padding:1px 7px}
.vs{background:var(--warnbg);border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:14px}
.vs .lbl{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;color:var(--warn);margin-bottom:2px}
.vs s{color:var(--muted);text-decoration-color:var(--warn)}
.oikea{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;color:var(--accent);margin-bottom:2px}
details{font-size:14px;color:var(--muted)}summary{cursor:pointer}
details ul{margin:8px 0 0;padding-left:18px}details li{margin-bottom:4px}
details a{color:var(--ink)}
"""

JS = """
const btns=[...document.querySelectorAll('.suodattimet button')];
btns.forEach(b=>b.onclick=()=>{btns.forEach(x=>x.classList.toggle('on',x===b));
const l=b.dataset.l;document.querySelectorAll('article').forEach(a=>
a.style.display=(l==='Kaikki'||a.dataset.l===l)?'':'none');});
"""


def klikkimittari(uutiset, klikit):
    """Laskee kunkin median klikkiotsikoiden osuuden."""
    tilasto = {}
    for i, u in enumerate(uutiset):
        kaikki, klikki = tilasto.get(u["lahde"], (0, 0))
        tilasto[u["lahde"]] = (kaikki + 1, klikki + (i in klikit))
    rivit = sorted(tilasto.items(), key=lambda kv: -kv[1][1] / kv[1][0])
    return [(nimi, k, n, round(100 * k / n)) for nimi, (n, k) in rivit]


def tee_html(tulos, uutiset):
    e = html.escape
    jutut = sorted(tulos.get("jutut", []), key=lambda j: -int(j.get("tarkeys", 0)))
    klikit = {i for i in tulos.get("klikkiotsikot", []) if isinstance(i, int)}
    luokat = sorted({j.get("luokka", "Muut") for j in jutut})
    from zoneinfo import ZoneInfo
    t = datetime.now(ZoneInfo("Europe/Helsinki"))
    nyt = f"{t.day}.{t.month}.{t.year} klo {t:%H.%M}"

    # Klikkimittari
    mittari = "".join(
        f'<span>{e(nimi)}</span><div class="palkki"><i style="width:{pros}%"></i></div>'
        f'<span class="luku">{pros} % <small>({k}/{n})</small></span>'
        for nimi, k, n, pros in klikkimittari(uutiset, klikit))

    kortit = []
    for j in jutut:
        idt = [i for i in j.get("uutiset", []) if isinstance(i, int) and 0 <= i < len(uutiset)]
        alkup = [f'<li><a href="{e(uutiset[i]["linkki"])}" target="_blank" rel="noopener">'
                 f'{e(uutiset[i]["otsikko"])}</a> – {e(uutiset[i]["lahde"])}</li>' for i in idt]
        kuva = next((uutiset[i]["kuva"] for i in idt if uutiset[i].get("kuva")), "")
        kuva_html = (f'<img class="kuva" src="{e(kuva)}" alt="" loading="lazy" '
                     f'referrerpolicy="no-referrer" onerror="this.remove()">') if kuva else ""
        klikki_id = next((i for i in idt if i in klikit), None)
        if klikki_id is not None:
            u = uutiset[klikki_id]
            vs = (f'<div class="vs"><span class="lbl">Klikkiotsikko · {e(u["lahde"])}</span>'
                  f'<s>{e(u["otsikko"])}</s></div><span class="oikea">Mitä oikeasti tapahtui</span>')
        else:
            vs = ""
        n = len(idt)
        kortit.append(f"""
<article data-l="{e(j.get('luokka','Muut'))}">{kuva_html}<div class="sisus">
  <div class="meta"><span class="tag">{e(j.get('luokka','Muut'))}</span>
  <span>{n} {'lähde' if n == 1 else 'lähdettä'}</span></div>
  {vs}<h2>{e(j.get('otsikko',''))}</h2>
  <p>{e(j.get('tiivistelma',''))}</p>
  <details><summary>Alkuperäiset otsikot</summary><ul>{''.join(alkup)}</ul></details>
</div></article>""")

    napit = "".join(f'<button data-l="{e(l)}">{e(l)}</button>' for l in luokat)
    return f"""<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Uutiskooste</title><style>{CSS}</style></head><body><main>
<h1>Uutiskooste</h1><p class="paiva">{nyt} · {len(uutiset)} uutista, {len(jutut)} juttua</p>
<div class="laatikko">{e(tulos.get('yleiskatsaus',''))}</div>
<div class="laatikko"><h3>Klikkimittari – klikkiotsikoiden osuus</h3>
<div class="mittari">{mittari}</div>
<p class="selite">Gemini arvioi jokaisen otsikon. Viimeiset 24 tuntia.</p></div>
<div class="suodattimet"><button class="on" data-l="Kaikki">Kaikki</button>{napit}</div>
{''.join(kortit)}
</main><script>{JS}</script></body></html>"""


# --- Pääohjelma --------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Uutiskooste – tiivistää uutisotsikot.")
    p.add_argument("--tunnit", type=int, default=24, help="aikaikkuna tunteina (oletus 24)")
    p.add_argument("--malli", help=f"Gemini-malli (oletus: {', '.join(MALLIT)})")
    p.add_argument("--ulos", default="uutiskooste.html", help="tulostiedosto")
    p.add_argument("--ei-avausta", action="store_true", help="älä avaa selainta")
    a = p.parse_args()

    avain = os.environ.get("GEMINI_API_KEY")
    if not avain:
        sys.exit("Aseta ensin ympäristömuuttuja GEMINI_API_KEY.")
    mallit = [a.malli] if a.malli else MALLIT

    print("Haetaan syötteitä…")
    uutiset = hae_kaikki(a.tunnit)
    if not uutiset:
        sys.exit("Yhtään uutista ei saatu haettua.")
    print(f"Tiivistetään {len(uutiset)} uutista…")
    tulos = kutsu_gemini(uutiset, mallit, avain)

    polku = Path(a.ulos).resolve()
    polku.parent.mkdir(parents=True, exist_ok=True)
    polku.write_text(tee_html(tulos, uutiset), encoding="utf-8")
    print(f"Valmis: {polku}")
    if not a.ei_avausta:
        avaa(polku)


def avaa(polku):
    """Avaa tiedoston selaimessa. Androidin Termuxissa käytetään termux-openia."""
    if "com.termux" in os.environ.get("PREFIX", ""):
        import shutil
        import subprocess
        if shutil.which("termux-open"):
            subprocess.run(["termux-open", "--content-type", "text/html", str(polku)])
            return
        print("Asenna termux-tools (pkg install termux-tools), niin sivu aukeaa automaattisesti.")
        return
    webbrowser.open(polku.as_uri())


if __name__ == "__main__":
    main()

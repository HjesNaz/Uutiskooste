#!/usr/bin/env python3
"""
Uutiskooste.

Hakee suomalaisten medioiden uutisotsikot RSS-syötteistä, niputtaa saman uutisen
eri medioista yhdeksi, kirjoittaa jokaiselle asiallisen otsikon ja tiivistelmän
Googlen Gemini-API:lla (ilmaistaso) ja tekee tuloksesta sovelluksena asennettavan
HTML-sivun. Mukana: klikkimittari, Ylen maakuntauutiset, pörssisähkö, sää,
ääneen luku ja päiväkohtainen arkisto.

Ei riippuvuuksia – pelkkä Python 3.9+.

Käyttö:
    export GEMINI_API_KEY=...              # https://aistudio.google.com/apikey
    python3 uutiskooste.py --ulos site/index.html --arkisto arkisto --ei-avausta
"""

import argparse
import html
import json
import os
import re
import shutil
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HKI = ZoneInfo("Europe/Helsinki")

# --- Asetukset -------------------------------------------------------------

# Valtakunnalliset lähteet. Saman median useampi syöte voi käyttää samaa nimeä.
# Jos osoite ei ole toimiva RSS-syöte, skripti etsii syötteen median etusivulta
# ja viimeisenä keinona Google Uutisista. Lokista näkee, mitä tapaa käytettiin.
SYOTTEET = [
    ("Yle", "https://yle.fi/rss/uutiset/paauutiset"),
    ("Yle", "https://yle.fi/rss/uutiset/tuoreimmat"),
    ("Helsingin Sanomat", "https://www.hs.fi/rss/tuoreimmat.xml"),
    ("Ilta-Sanomat", "https://www.is.fi/rss/tuoreimmat.xml"),
    ("Iltalehti", "https://www.iltalehti.fi/rss/uutiset.xml"),
    ("MTV Uutiset", "https://www.mtvuutiset.fi/api/feed/rss/uutiset_uusimmat"),
    ("Kauppalehti", "https://feeds.kauppalehti.fi/rss/main"),
    ("Aamulehti", "https://www.aamulehti.fi/rss"),
    ("Turun Sanomat", "https://www.ts.fi/rss"),
    ("Kaleva", "https://www.kaleva.fi/rss"),
    ("Uusi Suomi", "https://www.uusisuomi.fi/rss"),
    ("Maaseudun Tulevaisuus", "https://www.maaseuduntulevaisuus.fi/rss"),
]

# Ylen maakuntauutiset. Näitä ei tiivistetä, vaan ne näytetään omassa
# laatikossaan, josta valitset maakunnan (valinta tallentuu vain selaimeesi).
MAAKUNNAT = {
    "Etelä-Karjala": "18-141372", "Etelä-Pohjanmaa": "18-146311",
    "Etelä-Savo": "18-141852", "Kainuu": "18-141399", "Kanta-Häme": "18-138727",
    "Keski-Pohjanmaa": "18-135629", "Keski-Suomi": "18-148148",
    "Kymenlaakso": "18-131408", "Lappi": "18-139752", "Pirkanmaa": "18-146831",
    "Pohjanmaa": "18-148149", "Pohjois-Karjala": "18-141936",
    "Pohjois-Pohjanmaa": "18-148154", "Pohjois-Savo": "18-141764",
    "Päijät-Häme": "18-141401", "Satakunta": "18-139772", "Uusimaa": "18-147345",
    "Varsinais-Suomi": "18-135507",
}
MAAKUNTA_URL = "https://yle.fi/rss/t/{}/fi"
MAAKUNTA_MAX = 8

# Mallit kokeillaan järjestyksessä: jos ensimmäinen on ruuhkassa tai poistunut,
# siirrytään seuraavaan. Kaikilla on Gemini-API:n ilmaistaso.
MALLIT = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{malli}:generateContent"
PER_LAHDE = 14          # montako tuoreinta uutista per media
MAX_UUTISIA = 170       # montako otsikkoa lähetetään mallille yhteensä
ARKISTO_PAIVAA = 90     # montako päivää arkistossa säilytetään

SAHKO_LAHTEET = [
    "https://api.porssisahko.net/v2/latest-prices.json",
    "https://api.porssisahko.net/v1/latest-prices.json",
    "https://api.spot-hinta.fi/TodayAndDayForward",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; Uutiskooste/1.0)"}

# --- Apufunktiot -------------------------------------------------------------

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
            dt = datetime.fromisoformat(arvo.strip().replace("Z", "+00:00"))  # Atom
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def lataa(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --- RSS-haku ----------------------------------------------------------------

MEDIA_NS = "{http://search.yahoo.com/mrss/}"
ATOM = {"a": "http://www.w3.org/2005/Atom"}


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


def jasenna(data, nimi):
    """Palauttaa uutislistan tai None, jos data ei ole RSS/Atom-syöte."""
    try:
        juuri = ET.fromstring(data)
    except ET.ParseError:
        return None
    uutiset = []
    for item in juuri.iter("item"):
        uutiset.append({
            "lahde": nimi,
            "otsikko": siisti(item.findtext("title")),
            "kuvaus": siisti(item.findtext("description"))[:600],
            "linkki": (item.findtext("link") or "").strip(),
            "aika": lue_aika(item.findtext("pubDate")),
            "kuva": etsi_kuva(item),
        })
    for e in juuri.findall("a:entry", ATOM):
        linkki = e.find("a:link", ATOM)
        uutiset.append({
            "lahde": nimi,
            "otsikko": siisti(e.findtext("a:title", namespaces=ATOM)),
            "kuvaus": siisti(e.findtext("a:summary", namespaces=ATOM)
                             or e.findtext("a:content", namespaces=ATOM))[:600],
            "linkki": linkki.get("href", "") if linkki is not None else "",
            "aika": lue_aika(e.findtext("a:updated", namespaces=ATOM)
                             or e.findtext("a:published", namespaces=ATOM)),
            "kuva": etsi_kuva(e),
        })
    return uutiset or None


ALT_RE = re.compile(r"<link\b[^>]*>", re.I)


def etsi_syote_sivulta(sivu_html, perus):
    """Etsii HTML-sivulta <link rel="alternate" type="application/rss+xml">."""
    for tagi in ALT_RE.findall(sivu_html):
        if "alternate" in tagi.lower() and re.search(r"(rss|atom)\+xml", tagi, re.I):
            m = re.search(r'href=["\']([^"\']+)', tagi, re.I)
            if m:
                return urllib.parse.urljoin(perus, html.unescape(m.group(1)))
    return None


def google_uutiset(nimi, domain):
    url = ("https://news.google.com/rss/search?q=" +
           urllib.parse.quote(f"site:{domain} when:1d") + "&hl=fi&gl=FI&ceid=FI:fi")
    uutiset = jasenna(lataa(url), nimi) or []
    for u in uutiset:
        u["otsikko"] = re.sub(r"\s+[-–|]\s+[^-–|]+$", "", u["otsikko"])  # " - Lähde" pois
        if u["kuvaus"].startswith(u["otsikko"][:30]):
            u["kuvaus"] = ""
    return uutiset


def hae_syote(nimi, url):
    """Hakee syötteen kolmella tavalla: suora osoite, etusivun syötelinkki, Google Uutiset."""
    domain = urllib.parse.urlparse(url).netloc
    data = None
    try:
        data = lataa(url)
        uutiset = jasenna(data, nimi)
        if uutiset:
            print(f"  {nimi}: {len(uutiset)} uutista")
            return uutiset
    except Exception as e:
        print(f"  · {nimi}: {url} ei vastannut ({e})")
    try:
        etusivu = f"https://{domain}/"
        sivu = data.decode("utf-8", "replace") if data else lataa(etusivu).decode("utf-8", "replace")
        if "<rss" in sivu[:500] or "<feed" in sivu[:500]:
            raise ValueError("tyhjä syöte")
        loydetty = etsi_syote_sivulta(sivu, url) or etsi_syote_sivulta(
            lataa(etusivu).decode("utf-8", "replace"), etusivu)
        if loydetty:
            uutiset = jasenna(lataa(loydetty), nimi)
            if uutiset:
                print(f"  {nimi}: {len(uutiset)} uutista (syöte löytyi: {loydetty})")
                return uutiset
    except Exception as e:
        print(f"  · {nimi}: syötettä ei löytynyt etusivulta ({e})")
    try:
        uutiset = google_uutiset(nimi, domain.removeprefix("www.").removeprefix("feeds."))
        if uutiset:
            print(f"  {nimi}: {len(uutiset)} uutista (Google Uutisten kautta)")
            return uutiset
    except Exception as e:
        print(f"  · {nimi}: Google Uutiset ei vastannut ({e})")
    print(f"  ! {nimi}: ohitetaan, uutisia ei saatu", file=sys.stderr)
    return []


def hae_kaikki(tunnit):
    raja = datetime.now(timezone.utc) - timedelta(hours=tunnit)
    vanha = datetime.min.replace(tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as ex:
        tulokset = list(ex.map(lambda kv: hae_syote(*kv), SYOTTEET))
    per_lahde, nahdyt = {}, set()
    for lista in tulokset:
        for u in lista:
            avain = u["linkki"] or u["otsikko"]
            if not u["otsikko"] or avain in nahdyt:
                continue
            if u["aika"] and u["aika"] < raja:
                continue
            nahdyt.add(avain)
            per_lahde.setdefault(u["lahde"], []).append(u)
    kaikki = []
    for lista in per_lahde.values():
        lista.sort(key=lambda u: u["aika"] or vanha, reverse=True)
        kaikki.extend(lista[:PER_LAHDE])
    kaikki.sort(key=lambda u: u["aika"] or vanha, reverse=True)
    return kaikki[:MAX_UUTISIA]


def hae_maakunnat():
    def yksi(kv):
        nimi, tunnus = kv
        try:
            uutiset = jasenna(lataa(MAAKUNTA_URL.format(tunnus)), nimi) or []
        except Exception:
            return nimi, []
        uutiset.sort(key=lambda u: u["aika"] or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
        return nimi, [{"o": u["otsikko"], "l": u["linkki"],
                       "a": u["aika"].isoformat() if u["aika"] else ""}
                      for u in uutiset[:MAAKUNTA_MAX] if u["otsikko"]]
    with ThreadPoolExecutor(max_workers=8) as ex:
        tulos = dict(ex.map(yksi, MAAKUNNAT.items()))
    print(f"  Maakuntauutiset: {sum(1 for v in tulos.values() if v)}/{len(MAAKUNNAT)} maakuntaa")
    return {k: v for k, v in tulos.items() if v}


# --- Pörssisähkö -------------------------------------------------------------

def hae_sahko():
    """Palauttaa {"YYYY-MM-DD": [24 tuntihintaa c/kWh sis. alv tai None]}."""
    for url in SAHKO_LAHTEET:
        try:
            data = json.loads(lataa(url))
        except Exception as e:
            print(f"  · Sähkö: {url} ei vastannut ({e})")
            continue
        rivit = data.get("prices") if isinstance(data, dict) else data
        if not isinstance(rivit, list) or not rivit:
            continue
        tunnit = {}
        for r in rivit:
            alku = r.get("startDate") or r.get("DateTime") or r.get("date")
            if "price" in r:
                hinta = float(r["price"])                     # c/kWh sis. alv
            elif "PriceWithTax" in r:
                hinta = float(r["PriceWithTax"]) * 100        # €/kWh -> c/kWh
            else:
                continue
            aika = lue_aika(alku)
            if not aika:
                continue
            t = aika.astimezone(HKI)
            tunnit.setdefault((t.date().isoformat(), t.hour), []).append(hinta)
        paivat = {}
        for (pv, h), hinnat in tunnit.items():
            paivat.setdefault(pv, [None] * 24)[h] = round(sum(hinnat) / len(hinnat), 2)
        if paivat:
            print(f"  Sähkö: {', '.join(sorted(paivat))} ({url})")
            return paivat
    print("  ! Sähkön hintoja ei saatu", file=sys.stderr)
    return {}


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
            with urllib.request.urlopen(req, timeout=300) as r:
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
--accent:#0b6e4f;--warn:#b54708;--warnbg:#fff4e5;--bar:#ecebe7;--new:#1d4ed8;--newbg:#e6edff;
--cheap:#2f9e6b;--mid:#c9a227}
@media(prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#ececee;
--muted:#9a9aa1;--line:#2c2c30;--accent:#4cc39a;--warn:#f5a35c;--warnbg:#3a2a1a;--bar:#2a2a2e;
--new:#8fb0ff;--newbg:#1c2640;--cheap:#4cc39a;--mid:#e0bd4a}}
[hidden]{display:none!important}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit}
main{max-width:720px;margin:0 auto;padding:max(24px,env(safe-area-inset-top)) 16px 64px}
h1{font-size:28px;margin:0 0 4px}.paiva{color:var(--muted);margin:0 0 10px}
.tyokalut{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px}
.laatikko{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin-bottom:16px}
.laatikko h3{font-size:15px;margin:0 0 12px;display:flex;justify-content:space-between;
align-items:center;gap:8px}
.mittari{display:grid;grid-template-columns:auto 1fr auto;gap:8px 10px;align-items:center;font-size:14px}
.palkki{height:10px;background:var(--bar);border-radius:5px;overflow:hidden}
.palkki i{display:block;height:100%;background:var(--warn);border-radius:5px}
.luku{color:var(--muted);font-variant-numeric:tabular-nums;text-align:right}
.selite{font-size:12px;color:var(--muted);margin:10px 0 0}
.nappi{border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none;
border-radius:8px;padding:7px 12px;font-size:14px;cursor:pointer;display:inline-block}
.nappi.on{background:var(--accent);border-color:var(--accent);color:#fff}
.linkkinappi{background:none;border:0;color:var(--muted);font-size:12px;cursor:pointer;
padding:0;text-decoration:underline}
.pieni{font-size:12px;padding:4px 9px}
select{padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);
color:var(--ink);font-size:14px;max-width:60%}
/* sää */
.saa-nyt{display:flex;align-items:center;gap:14px}
.saa-ikoni{font-size:40px;line-height:1}
.saa-lampo{font-size:30px;font-weight:600;line-height:1}
.saa-teksti{font-size:14px;color:var(--muted)}
.saa-paivat{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px;
font-size:13px;text-align:center}
.saa-paivat div{background:var(--bg);border-radius:8px;padding:6px 4px}
.saa-paivat b{display:block;font-size:18px;font-weight:400}
.saa-lomake{display:flex;gap:6px;flex-wrap:wrap}
.saa-lomake input{flex:1;min-width:140px;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink);font-size:15px}
/* sähkö */
.sahko-nyt{font-size:26px;font-weight:600;line-height:1.1}
.sahko-nyt small{font-size:13px;font-weight:400;color:var(--muted)}
.kaavio{display:flex;align-items:flex-end;gap:2px;height:80px;margin:12px 0 4px}
.kaavio i{flex:1;background:var(--cheap);border-radius:2px 2px 0 0;min-height:2px;position:relative}
.kaavio i.k{background:var(--mid)}.kaavio i.x{background:var(--warn)}
.kaavio i.nyt{outline:2px solid var(--ink);outline-offset:1px}
.akseli{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}
.sahko-tiedot{font-size:13px;color:var(--muted);margin-top:8px}
/* paikalliset */
.paik li{margin-bottom:6px;font-size:15px}.paik ul{margin:0;padding-left:18px}
.paik time{color:var(--muted);font-size:12px;margin-right:6px}
.paik a{text-decoration:none}.paik a:hover{text-decoration:underline}
/* suodattimet */
.suodattimet{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}
.suodattimet button{border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:999px;padding:6px 12px;font-size:14px;cursor:pointer}
.suodattimet button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.suodattimet button.piilota.on{background:var(--ink);border-color:var(--ink);color:var(--bg)}
/* kortit */
article{background:var(--card);border:1px solid var(--line);border-radius:12px;
margin-bottom:12px;overflow:hidden;transition:opacity .2s}
article.luettu{opacity:.55}
article .sisus{padding:14px 16px}
article .ylaosa{display:flex;gap:12px;align-items:flex-start}
article .ylaosa>div{flex:1;min-width:0}
article .peukku{width:84px;height:84px;object-fit:cover;border-radius:8px;flex:none;background:var(--bar)}
article h2{font-size:17px;margin:0 0 6px;line-height:1.3}
article h2 a{text-decoration:none}article h2 a:hover{text-decoration:underline}
article p{margin:0 0 10px}
.meta{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;font-size:13px;color:var(--muted);margin-bottom:8px}
.tag{border:1px solid var(--line);border-radius:6px;padding:1px 7px}
.uusi{background:var(--newbg);color:var(--new);border-color:transparent;font-weight:600}
.vs{background:var(--warnbg);border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:14px}
.vs .lbl{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;color:var(--warn);margin-bottom:2px}
.vs s{color:var(--muted);text-decoration-color:var(--warn)}
.oikea{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;color:var(--accent);margin-bottom:2px}
.linkit{display:flex;flex-wrap:wrap;gap:6px}
.linkit a{font-size:13px;text-decoration:none;border:1px solid var(--line);border-radius:999px;
padding:4px 10px;background:var(--bg)}
.linkit a:hover{border-color:var(--accent)}
article.paa{border:2px solid var(--accent)}
article.paa .iso{display:block;width:100%;aspect-ratio:2/1;object-fit:cover;background:var(--bar)}
article.paa h2{font-size:22px}
.paa-lbl{display:inline-block;background:var(--accent);color:#fff;font-size:11px;font-weight:700;
letter-spacing:.05em;text-transform:uppercase;border-radius:6px;padding:2px 8px}
.tyhja{color:var(--muted);text-align:center;padding:24px 0;display:none}
.arkisto-lista a{display:block;padding:10px 0;border-bottom:1px solid var(--line);text-decoration:none}
footer{font-size:12px;color:var(--muted);text-align:center;margin-top:24px}
"""

JS = r"""
const ls={get(k){try{return localStorage.getItem(k)}catch(e){return null}},
set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const jget=(k,d)=>{try{return JSON.parse(ls.get(k))??d}catch(e){return d}};
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const avaimet=a=>a.dataset.k.split(' ');

// --- Suodatus: luokka + piilota luetut ---
let luokka='Kaikki',piilota=jget('uk_piilota',false);
const luetut=new Set(jget('uk_luetut',[]));
function paivita(){let n=0;$$('article').forEach(a=>{const nak=(luokka==='Kaikki'||a.dataset.l===luokka)
&&!(piilota&&a.classList.contains('luettu'));a.style.display=nak?'':'none';if(nak)n++});
const t=$('.tyhja');if(t)t.style.display=n?'none':'block';
const p=$('.piilota');if(p)p.classList.toggle('on',piilota)}
$$('.suodattimet button[data-l]').forEach(b=>b.onclick=()=>{luokka=b.dataset.l;
$$('.suodattimet button[data-l]').forEach(x=>x.classList.toggle('on',x===b));paivita()});
const pb=$('.piilota');if(pb)pb.onclick=()=>{piilota=!piilota;ls.set('uk_piilota',JSON.stringify(piilota));paivita()};

// --- Luetut: linkin avaaminen merkitsee jutun luetuksi ---
$$('article').forEach(a=>{if(avaimet(a).some(k=>luetut.has(k)))a.classList.add('luettu');
a.querySelectorAll('a').forEach(l=>l.addEventListener('click',()=>{avaimet(a).forEach(k=>luetut.add(k));
ls.set('uk_luetut',JSON.stringify([...luetut].slice(-1500)));a.classList.add('luettu');
setTimeout(paivita,400)}))});

// --- Uusi-merkinnät: juttu on uusi, jos siinä on linkki, jota et ole ennen nähnyt ---
(()=>{const raw=ls.get('uk_nahdyt');const nahdyt=new Set(raw?JSON.parse(raw):[]);const kaikki=[];let uusia=0;
$$('article').forEach(a=>{const k=avaimet(a);kaikki.push(...k);
if(raw&&k.some(x=>!nahdyt.has(x))){uusia++;a.querySelector('.meta').insertAdjacentHTML('afterbegin',
'<span class="tag uusi">Uusi</span>')}});
if(uusia)$('.paiva').insertAdjacentText('beforeend',` · ${uusia} uutta`);
const tallenna=()=>ls.set('uk_nahdyt',JSON.stringify([...new Set([...nahdyt,...kaikki])].slice(-1500)));
addEventListener('pagehide',tallenna);
document.addEventListener('visibilitychange',()=>{if(document.hidden)tallenna()});})();
paivita();

// --- Kuuntele kooste (selaimen oma puhesynteesi) ---
(()=>{const b=$('#kuuntele');if(!b)return;if(!('speechSynthesis' in window)){b.remove();return}
const s=speechSynthesis;const lepo='🔊 Kuuntele';
b.onclick=()=>{if(s.speaking||s.pending){s.cancel();b.textContent=lepo;return}
const osat=[$('.katsaus').textContent,...$$('article').filter(a=>a.style.display!=='none')
.map(a=>a.querySelector('h2').textContent+'. '+a.querySelector('p').textContent)];
const v=s.getVoices().find(v=>(v.lang||'').toLowerCase().startsWith('fi'));
osat.forEach((t,i)=>{const u=new SpeechSynthesisUtterance(t);u.lang='fi-FI';if(v)u.voice=v;
if(i===osat.length-1)u.onend=()=>{b.textContent=lepo};s.speak(u)});b.textContent='⏹ Lopeta'};
s.getVoices();})();

// --- Asenna sovellukseksi ---
(()=>{let ev=null;const b=$('#asenna');if(!b)return;
addEventListener('beforeinstallprompt',e=>{e.preventDefault();ev=e;b.hidden=false});
b.onclick=async()=>{if(!ev)return;ev.prompt();await ev.userChoice;ev=null;b.hidden=true};
if('serviceWorker' in navigator&&document.body.dataset.sw)navigator.serviceWorker.register(document.body.dataset.sw).catch(()=>{});})();

// --- Pörssisähkö ---
(()=>{const el=$('#sahko'),dEl=$('#sahko-data');if(!el||!dEl)return;const D=JSON.parse(dEl.textContent);
const nyt=new Date();const f=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Helsinki',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hour12:false});
const osa=Object.fromEntries(f.formatToParts(nyt).map(p=>[p.type,p.value]));
const tanaan=`${osa.year}-${osa.month}-${osa.day}`,tunti=+osa.hour%24;
const huom=new Date(Date.parse(tanaan+'T12:00:00Z')+864e5).toISOString().slice(0,10);
const pv=[[tanaan,'Tänään'],[huom,'Huomenna']].filter(([d])=>D[d]);
if(!pv.length){el.remove();return}
const fmt=x=>x.toFixed(1).replace('.',',');
let valittu=pv[0][0];
function piirra(){const h=D[valittu];const ok=h.map((p,i)=>[p,i]).filter(([p])=>p!==null);
const max=Math.max(1,...ok.map(([p])=>p));const ka=ok.reduce((s,[p])=>s+p,0)/ok.length;
const min=ok.reduce((a,b)=>b[0]<a[0]?b:a),mx=ok.reduce((a,b)=>b[0]>a[0]?b:a);
const onTanaan=valittu===tanaan&&h[tunti]!==null;
const nappi=pv.length>1?pv.map(([d,n])=>`<button class="nappi pieni ${d===valittu?'on':''}" data-d="${d}">${n}</button>`).join(' '):'';
el.innerHTML=`<h3><span>⚡ Pörssisähkö</span><span>${nappi}</span></h3>
${onTanaan?`<div class="sahko-nyt">${fmt(h[tunti])} <small>c/kWh nyt</small></div>`:`<div class="sahko-nyt">${fmt(ka)} <small>c/kWh keskihinta</small></div>`}
<div class="kaavio">${h.map((p,i)=>p===null?'<i style="height:0"></i>':
`<i class="${p<5?'':p<15?'k':'x'} ${onTanaan&&i===tunti?'nyt':''}" style="height:${Math.max(2,Math.max(0,p)/max*100)}%" title="klo ${i}: ${fmt(p)} c"></i>`).join('')}</div>
<div class="akseli"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
<div class="sahko-tiedot">Halvin klo ${min[1]} (${fmt(min[0])} c) · Kallein klo ${mx[1]} (${fmt(mx[0])} c) · Keskihinta ${fmt(ka)} c</div>
<p class="selite">Tuntikeskiarvot, c/kWh sis. alv. Huomisen hinnat julkaistaan yleensä iltapäivällä.</p>`;
el.querySelectorAll('button[data-d]').forEach(b=>b.onclick=()=>{valittu=b.dataset.d;piirra()})}
piirra()})();

// --- Maakuntauutiset ---
(()=>{const el=$('#paik'),dEl=$('#paik-data');if(!el||!dEl)return;const D=JSON.parse(dEl.textContent);
const alueet=Object.keys(D).sort((a,b)=>a.localeCompare(b,'fi'));let alue=ls.get('uk_alue');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function piirra(){const opt=alueet.map(a=>`<option ${a===alue?'selected':''}>${esc(a)}</option>`).join('');
const lista=alue&&D[alue]?'<ul>'+D[alue].map(u=>{const t=u.a?new Date(u.a).toLocaleTimeString('fi-FI',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Helsinki'}):'';
return `<li><time>${t}</time><a href="${esc(u.l)}" target="_blank" rel="noopener">${esc(u.o)}</a></li>`}).join('')+'</ul>'
:'<p class="selite">Valitse maakunta, niin näet Ylen alueuutiset. Valinta tallentuu vain tähän selaimeen.</p>';
el.innerHTML=`<h3><span>📍 Lähiuutiset</span><select><option value="">Valitse maakunta</option>${opt}</select></h3>${lista}`;
el.querySelector('select').onchange=e=>{alue=e.target.value;ls.set('uk_alue',alue);piirra()}}
piirra()})();

// --- Sää (Open-Meteo). Paikka tallentuu vain tähän selaimeen. ---
const WMO={0:['☀️','Selkeää'],1:['🌤️','Enimmäkseen selkeää'],2:['⛅','Puolipilvistä'],
3:['☁️','Pilvistä'],45:['🌫️','Sumua'],48:['🌫️','Huurresumua'],51:['🌦️','Heikkoa tihkua'],
53:['🌦️','Tihkusadetta'],55:['🌧️','Voimakasta tihkua'],56:['🌧️','Jäätävää tihkua'],
57:['🌧️','Jäätävää tihkua'],61:['🌦️','Heikkoa sadetta'],63:['🌧️','Sadetta'],
65:['🌧️','Voimakasta sadetta'],66:['🌧️','Jäätävää sadetta'],67:['🌧️','Jäätävää sadetta'],
71:['🌨️','Heikkoa lumisadetta'],73:['🌨️','Lumisadetta'],75:['❄️','Runsasta lumisadetta'],
77:['🌨️','Lumijyväsiä'],80:['🌦️','Sadekuuroja'],81:['🌧️','Sadekuuroja'],
82:['⛈️','Rankkoja kuuroja'],85:['🌨️','Lumikuuroja'],86:['❄️','Runsaita lumikuuroja'],
95:['⛈️','Ukkosta'],96:['⛈️','Ukkosta ja raetta'],99:['⛈️','Ukkosta ja raetta']};
const PV=['su','ma','ti','ke','to','pe','la'];
const saa=$('#saa');
function lomake(virhe){if(!saa)return;saa.innerHTML=`<h3>Sää</h3><form class="saa-lomake">
<input name="p" placeholder="Paikkakunta" autocomplete="off">
<button class="nappi">Hae</button><button type="button" class="nappi" id="gps">📍 Sijainti</button></form>
${virhe?`<p class="selite">${virhe}</p>`:''}`;
saa.querySelector('form').onsubmit=async ev=>{ev.preventDefault();const n=ev.target.p.value.trim();if(!n)return;
try{const r=await(await fetch('https://geocoding-api.open-meteo.com/v1/search?count=1&language=fi&name='+encodeURIComponent(n))).json();
if(!r.results||!r.results.length)return lomake('Paikkakuntaa ei löytynyt.');
const x=r.results[0];tallennaPaikka({lat:x.latitude,lon:x.longitude,nimi:x.name})}catch(e){lomake('Haku epäonnistui.')}};
saa.querySelector('#gps').onclick=()=>{if(!navigator.geolocation)return lomake('Sijainti ei ole käytettävissä.');
navigator.geolocation.getCurrentPosition(p=>tallennaPaikka({lat:p.coords.latitude,lon:p.coords.longitude,nimi:'Oma sijainti'}),
()=>lomake('Sijaintia ei saatu. Kirjoita paikkakunta.'))};}
function tallennaPaikka(p){ls.set('uk_paikka',JSON.stringify(p));naytaSaa(p)}
async function naytaSaa(p){saa.innerHTML='<h3>Sää</h3><p class="selite">Haetaan…</p>';
try{const u=`https://api.open-meteo.com/v1/forecast?latitude=${p.lat}&longitude=${p.lon}`+
'&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m'+
'&daily=weather_code,temperature_2m_max,temperature_2m_min&forecast_days=4&timezone=auto&wind_speed_unit=ms';
const d=await(await fetch(u)).json();const c=d.current;const [ik,tx]=WMO[c.weather_code]||['🌡️',''];
const paivat=d.daily.time.slice(1,4).map((t,i)=>{const w=WMO[d.daily.weather_code[i+1]]||['🌡️'];
return `<div>${PV[new Date(t).getDay()]}<b>${w[0]}</b>${Math.round(d.daily.temperature_2m_max[i+1])}° / ${Math.round(d.daily.temperature_2m_min[i+1])}°</div>`}).join('');
saa.innerHTML=`<h3>Sää · ${p.nimi.replace(/[<>&]/g,'')}</h3><div class="saa-nyt"><span class="saa-ikoni">${ik}</span>
<div><div class="saa-lampo">${Math.round(c.temperature_2m)}°</div>
<div class="saa-teksti">${tx} · tuntuu ${Math.round(c.apparent_temperature)}° · tuuli ${Math.round(c.wind_speed_10m)} m/s</div></div></div>
<div class="saa-paivat">${paivat}</div>
<p class="selite"><button class="linkkinappi" id="vaihda">Vaihda paikkaa</button> · Lähde: Open-Meteo</p>`;
saa.querySelector('#vaihda').onclick=()=>lomake();}catch(e){lomake('Säätä ei saatu haettua.')}}
if(saa){const p=jget('uk_paikka',null);p?naytaSaa(p):lomake()}
"""


def klikkimittari(uutiset, klikit):
    """Laskee kunkin median klikkiotsikoiden osuuden."""
    tilasto = {}
    for i, u in enumerate(uutiset):
        kaikki, klikki = tilasto.get(u["lahde"], (0, 0))
        tilasto[u["lahde"]] = (kaikki + 1, klikki + (i in klikit))
    rivit = sorted(tilasto.items(), key=lambda kv: (-kv[1][1] / kv[1][0], kv[0]))
    return [(nimi, k, n, round(100 * k / n)) for nimi, (n, k) in rivit]


def json_skripti(tunnus, data):
    teksti = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="{tunnus}">{teksti}</script>'


def tee_html(tulos, uutiset, maakunnat=None, sahko=None, juuri="", arkistoitu=None):
    """juuri = suhteellinen polku sivuston juureen ('' etusivulle, '../' arkistolle).
    arkistoitu = päivämääräteksti, jos sivu on arkistokopio."""
    e = html.escape
    jutut = sorted(tulos.get("jutut", []),
                   key=lambda j: (-int(j.get("tarkeys", 0) or 0), -len(j.get("uutiset", []))))
    klikit = {i for i in tulos.get("klikkiotsikot", []) if isinstance(i, int)}
    luokat = sorted({j.get("luokka", "Muut") for j in jutut})
    t = datetime.now(HKI)
    nyt = f"{t.day}.{t.month}.{t.year} klo {t:%H.%M}"

    mittari = "".join(
        f'<span>{e(nimi)}</span><div class="palkki"><i style="width:{pros}%"></i></div>'
        f'<span class="luku">{pros} % <small>({k}/{n})</small></span>'
        for nimi, k, n, pros in klikkimittari(uutiset, klikit))

    kortit = []
    for nro, j in enumerate(jutut):
        idt = [i for i in j.get("uutiset", []) if isinstance(i, int) and 0 <= i < len(uutiset)]
        if not idt:
            continue
        paa = not kortit
        paalinkki = uutiset[next((i for i in idt if i not in klikit), idt[0])]["linkki"]
        linkit = "".join(
            f'<a href="{e(uutiset[i]["linkki"])}" target="_blank" rel="noopener">'
            f'{e(uutiset[i]["lahde"])} ↗</a>' for i in idt if uutiset[i]["linkki"])
        kuva = next((uutiset[i]["kuva"] for i in idt if uutiset[i].get("kuva")), "")

        def img(cls):
            return (f'<img class="{cls}" src="{e(kuva)}" alt="" loading="lazy" '
                    f'referrerpolicy="no-referrer" onerror="this.remove()">') if kuva else ""
        klikki_id = next((i for i in idt if i in klikit), None)
        vs = ""
        if klikki_id is not None:
            u = uutiset[klikki_id]
            vs = (f'<div class="vs"><span class="lbl">Klikkiotsikko · {e(u["lahde"])}</span>'
                  f'<s>{e(u["otsikko"])}</s></div><span class="oikea">Mitä oikeasti tapahtui</span>')
        ajat = [uutiset[i]["aika"] for i in idt if uutiset[i]["aika"]]
        aika = f'<span>klo {max(ajat).astimezone(HKI):%H.%M}</span>' if ajat else ""
        n = len(idt)
        meta = (f'<div class="meta">{"<span class=paa-lbl>Päivän pääuutinen</span>" if paa else ""}'
                f'<span class="tag">{e(j.get("luokka","Muut"))}</span>{aika}'
                f'<span>{n} {"lähde" if n == 1 else "lähdettä"}</span></div>')
        otsikko = (f'<h2><a href="{e(paalinkki)}" target="_blank" rel="noopener">'
                   f'{e(j.get("otsikko",""))}</a></h2>')
        teksti = f'<p>{e(j.get("tiivistelma",""))}</p>'
        avaimet = " ".join(e(uutiset[i]["linkki"] or uutiset[i]["otsikko"]).replace(" ", "_") for i in idt)
        if paa:
            runko = f'{img("iso")}<div class="sisus">{meta}{vs}{otsikko}{teksti}<div class="linkit">{linkit}</div></div>'
        else:
            runko = (f'<div class="sisus">{meta}{vs}<div class="ylaosa"><div>{otsikko}{teksti}</div>'
                     f'{img("peukku")}</div><div class="linkit">{linkit}</div></div>')
        kortit.append(f'<article class="{"paa" if paa else ""}" data-l="{e(j.get("luokka","Muut"))}" '
                      f'data-k="{avaimet}">{runko}</article>')

    napit = "".join(f'<button data-l="{e(l)}">{e(l)}</button>' for l in luokat)
    lahteet = ", ".join(dict.fromkeys(u["lahde"] for u in uutiset))
    otsake = (f'<p class="paiva">Arkistoitu kooste {e(arkistoitu)}</p>'
              f'<div class="tyokalut"><a class="nappi" href="{juuri}">← Tuorein kooste</a>'
              f'<a class="nappi" href="{juuri}arkisto/">Arkisto</a></div>') if arkistoitu else (
              f'<p class="paiva">{nyt} · {len(uutiset)} uutista, {len(jutut)} juttua</p>'
              f'<div class="tyokalut"><button class="nappi" id="kuuntele">🔊 Kuuntele</button>'
              f'<a class="nappi" href="{juuri}arkisto/">🗂 Arkisto</a>'
              f'<button class="nappi" id="asenna" hidden>📲 Asenna sovellukseksi</button></div>')
    sahko_html = (f'<div class="laatikko" id="sahko"></div>{json_skripti("sahko-data", sahko)}'
                  if sahko and not arkistoitu else "")
    paik_html = (f'<div class="laatikko paik" id="paik"></div>{json_skripti("paik-data", maakunnat)}'
                 if maakunnat else "")
    saa_html = '<div class="laatikko" id="saa"></div>' if not arkistoitu else ""

    return f"""<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b6e4f">
<link rel="manifest" href="{juuri}manifest.webmanifest">
<link rel="icon" href="{juuri}icon-192.png"><link rel="apple-touch-icon" href="{juuri}icon-192.png">
<title>Uutiskooste{' ' + e(arkistoitu) if arkistoitu else ''}</title><style>{CSS}</style></head>
<body data-sw="{'' if arkistoitu else 'sw.js'}"><main>
<h1>Uutiskooste</h1>{otsake}
{saa_html}{sahko_html}
<div class="laatikko katsaus">{e(tulos.get('yleiskatsaus',''))}</div>
<div class="suodattimet"><button class="on" data-l="Kaikki">Kaikki</button>{napit}
<button class="piilota">Piilota luetut</button></div>
{''.join(kortit[:1])}
<div class="laatikko"><h3>Klikkimittari – klikkiotsikoiden osuus</h3>
<div class="mittari">{mittari}</div>
<p class="selite">Gemini arvioi jokaisen otsikon. Viimeiset 24 tuntia.</p></div>
{paik_html}
{''.join(kortit[1:])}
<p class="tyhja">Ei näytettäviä juttuja. Kaikki on luettu! 🎉</p>
<footer>Lähteet: {e(lahteet)}, Ylen alueuutiset · Tiivistelmät: Google Gemini ·
Sää: Open-Meteo · Sähkö: porssisahko.net</footer>
</main><script>{JS}</script></body></html>"""


# --- Sovelluksen tiedostot (PWA) ---------------------------------------------

MANIFEST = {
    "name": "Uutiskooste", "short_name": "Uutiskooste",
    "description": "Päivän uutiset ilman klikkiotsikoita",
    "start_url": "./", "scope": "./", "display": "standalone", "lang": "fi",
    "background_color": "#f6f5f2", "theme_color": "#0b6e4f",
    "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
               "purpose": "any maskable"}],
}

SW = """const C='uutiskooste-v1';
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const r=e.request;
if(r.method!=='GET'||new URL(r.url).origin!==location.origin)return;
e.respondWith(fetch(r).then(res=>{const c=res.clone();caches.open(C).then(x=>x.put(r,c));return res})
.catch(()=>caches.match(r)))});
"""


def tee_png(koko):
    """Piirtää sovelluskuvakkeen: vihreä tausta ja kolme valkoista 'tekstiriviä'."""
    s = koko / 512
    vihrea, valk = (11, 110, 79, 255), (255, 255, 255, 255)
    rivit = [(150, 190, 120, 392), (236, 276, 120, 392), (322, 362, 120, 300)]
    raaka = bytearray()
    for y in range(koko):
        raaka.append(0)
        yy = y / s
        for x in range(koko):
            xx = x / s
            vari = vihrea
            for y0, y1, x0, x1 in rivit:
                if y0 <= yy < y1 and x0 <= xx < x1:
                    vari = valk
                    break
            raaka.extend(vari)

    def lohko(tyyppi, data):
        return (struct.pack(">I", len(data)) + tyyppi + data +
                struct.pack(">I", zlib.crc32(tyyppi + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + lohko(b"IHDR", struct.pack(">IIBBBBB", koko, koko, 8, 6, 0, 0, 0))
            + lohko(b"IDAT", zlib.compress(bytes(raaka), 9)) + lohko(b"IEND", b""))


def kirjoita_sovellustiedostot(kansio):
    (kansio / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (kansio / "sw.js").write_text(SW, encoding="utf-8")
    for koko in (192, 512):
        (kansio / f"icon-{koko}.png").write_bytes(tee_png(koko))


# --- Arkisto -----------------------------------------------------------------

def paivita_arkisto(arkisto, tulos, uutiset, maakunnat):
    """Tallentaa päivän koosteen arkistoon (päivän viimeinen ajo jää voimaan)."""
    arkisto.mkdir(parents=True, exist_ok=True)
    t = datetime.now(HKI)
    teksti = f"{t.day}.{t.month}.{t.year}"
    (arkisto / f"{t.date().isoformat()}.html").write_text(
        tee_html(tulos, uutiset, maakunnat, juuri="../", arkistoitu=teksti), encoding="utf-8")
    raja = (t.date() - timedelta(days=ARKISTO_PAIVAA)).isoformat()
    tiedostot = []
    for p in sorted(arkisto.glob("????-??-??.html"), reverse=True):
        if p.stem < raja:
            p.unlink()
        else:
            tiedostot.append(p)
    VK = ["ma", "ti", "ke", "to", "pe", "la", "su"]
    rivit = "".join(
        f'<a href="{p.name}">{VK[datetime.fromisoformat(p.stem).weekday()]} '
        f'{int(p.stem[8:])}.{int(p.stem[5:7])}.{p.stem[:4]}</a>' for p in tiedostot)
    (arkisto / "index.html").write_text(f"""<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="manifest" href="../manifest.webmanifest"><link rel="icon" href="../icon-192.png">
<title>Uutiskooste – arkisto</title><style>{CSS}</style></head><body><main>
<h1>Arkisto</h1><p class="paiva">Kunkin päivän viimeisin kooste, {ARKISTO_PAIVAA} päivää taaksepäin.</p>
<div class="tyokalut"><a class="nappi" href="../">← Tuorein kooste</a></div>
<div class="laatikko arkisto-lista">{rivit}</div></main></body></html>""", encoding="utf-8")


# --- Pääohjelma --------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Uutiskooste – tiivistää uutisotsikot.")
    p.add_argument("--tunnit", type=int, default=24, help="aikaikkuna tunteina (oletus 24)")
    p.add_argument("--malli", help=f"Gemini-malli (oletus: {', '.join(MALLIT)})")
    p.add_argument("--ulos", default="site/index.html", help="tulostiedosto")
    p.add_argument("--arkisto", help="arkistokansio repossa (esim. arkisto)")
    p.add_argument("--ei-avausta", action="store_true", help="älä avaa selainta")
    a = p.parse_args()

    avain = os.environ.get("GEMINI_API_KEY")
    if not avain:
        sys.exit("Aseta ensin ympäristömuuttuja GEMINI_API_KEY.")
    mallit = [a.malli] if a.malli else MALLIT

    print("Haetaan syötteitä…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_uutiset = ex.submit(hae_kaikki, a.tunnit)
        f_maakunnat = ex.submit(hae_maakunnat)
        f_sahko = ex.submit(hae_sahko)
        uutiset, maakunnat, sahko = f_uutiset.result(), f_maakunnat.result(), f_sahko.result()
    if not uutiset:
        sys.exit("Yhtään uutista ei saatu haettua.")
    print(f"Tiivistetään {len(uutiset)} uutista…")
    tulos = kutsu_gemini(uutiset, mallit, avain)

    polku = Path(a.ulos).resolve()
    polku.parent.mkdir(parents=True, exist_ok=True)
    polku.write_text(tee_html(tulos, uutiset, maakunnat, sahko), encoding="utf-8")
    kirjoita_sovellustiedostot(polku.parent)
    if a.arkisto:
        arkisto = Path(a.arkisto).resolve()
        paivita_arkisto(arkisto, tulos, uutiset, maakunnat)
        shutil.copytree(arkisto, polku.parent / "arkisto", dirs_exist_ok=True)
    print(f"Valmis: {polku}")
    if not a.ei_avausta:
        avaa(polku)


def avaa(polku):
    """Avaa tiedoston selaimessa. Androidin Termuxissa käytetään termux-openia."""
    if "com.termux" in os.environ.get("PREFIX", ""):
        import subprocess
        if shutil.which("termux-open"):
            subprocess.run(["termux-open", "--content-type", "text/html", str(polku)])
        return
    webbrowser.open(polku.as_uri())


if __name__ == "__main__":
    main()

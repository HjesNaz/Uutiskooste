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

# Gemini-kiintiön säästö: uusi tiivistys tehdään vain, kun uusia otsikoita on
# kertynyt tarpeeksi, eikä koskaan useammin kuin PAIVARAJA kertaa vuorokaudessa.
# Muulloin sivu päivitetään edellisellä tiivistyksellä (sää, sähkö ja
# lähiuutiset päivittyvät silti joka kerta).
UUSIA_VAHINTAAN = 5     # montako uutta otsikkoa tarvitaan uuteen tiivistykseen
PAIVARAJA = 18          # tiivistyksiä enintään vuorokaudessa (Suomen aikaa)

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


# --- Liikennetiedotteet ja urheilu -------------------------------------------

LIIKENNE_URL = ("https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements"
                "?includeAreaGeometry=false")
URHEILU_URL = "https://yle.fi/rss/urheilu"


def eka_piste(geom):
    """Palauttaa GeoJSON-geometrian ensimmäisen pisteen [lon, lat] tai None."""
    if not isinstance(geom, dict):
        return None
    c = geom.get("coordinates")
    while isinstance(c, list) and c and isinstance(c[0], list):
        c = c[0]
    if isinstance(c, list) and len(c) >= 2 and all(isinstance(x, (int, float)) for x in c[:2]):
        return [round(c[0], 4), round(c[1], 4)]
    return None


def hae_liikenne():
    """Voimassa olevat liikennetiedotteet (Fintraffic Digitraffic, avoin data)."""
    try:
        req = urllib.request.Request(LIIKENNE_URL, headers={**UA, "Digitraffic-User": "Uutiskooste"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        print(f"  ! Liikennetiedotteita ei saatu ({e})", file=sys.stderr)
        return []
    tulos = []
    for f in data.get("features", []) if isinstance(data, dict) else []:
        p = f.get("properties", {})
        ilmot = p.get("announcements") or []
        a = next((x for x in ilmot if x.get("language") == "fi"), ilmot[0] if ilmot else None)
        if not a:
            continue
        sij = a.get("location") or {}
        alueet = [x.get("name", "") for x in
                  ((a.get("locationDetails") or {}).get("areaLocation") or {}).get("areas") or []]
        teksti = siisti(sij.get("description") or a.get("comment") or "")[:220]
        tulos.append({"o": siisti(a.get("title", ""))[:160], "t": teksti,
                      "al": [x for x in alueet if x], "p": eka_piste(f.get("geometry")),
                      "a": p.get("releaseTime") or ""})
    print(f"  Liikennetiedotteet: {len(tulos)}")
    return tulos


def hae_urheilu():
    try:
        uutiset = jasenna(lataa(URHEILU_URL), "Yle Urheilu") or []
    except Exception as e:
        print(f"  ! Urheiluotsikoita ei saatu ({e})", file=sys.stderr)
        return []
    uutiset.sort(key=lambda u: u["aika"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [{"o": u["otsikko"], "l": u["linkki"], "a": u["aika"].isoformat() if u["aika"] else ""}
            for u in uutiset[:6] if u["otsikko"]]


# --- Historia: viikon trendi, liittyvät jutut ja haku --------------------------

STOP = {"että", "joka", "jotka", "mutta", "myös", "sekä", "kuin", "ovat", "ollut", "olla",
        "tämä", "siitä", "sitä", "mikä", "miten", "kertoo", "kertoi", "uusi", "uuden",
        "vuoden", "vuonna", "jälkeen", "mukaan", "aikana", "otsikosta", "selviä", "tapahtui"}


def sanat(teksti):
    """Karkea suomen 'stemmaus': sanojen 5 ensimmäistä kirjainta."""
    return {w[:5] for w in re.findall(r"[a-zåäö0-9]+", (teksti or "").lower())
            if len(w) >= 4 and w not in STOP}


def paa_linkki(j, uutiset, klikit):
    idt = [i for i in j.get("uutiset", []) if isinstance(i, int) and 0 <= i < len(uutiset)]
    if not idt:
        return ""
    return uutiset[next((i for i in idt if i not in klikit), idt[0])]["linkki"]


def paivita_historia(historia, tulos, uutiset, paiva):
    klikit = {i for i in tulos.get("klikkiotsikot", []) if isinstance(i, int)}
    mittari = {nimi: [n, k] for nimi, k, n, _ in klikkimittari(uutiset, klikit)}
    jutut = [{"o": j.get("otsikko", ""), "t": j.get("tiivistelma", "")[:300],
              "l": paa_linkki(j, uutiset, klikit), "c": j.get("luokka", "Muut")}
             for j in tulos.get("jutut", [])]
    historia.setdefault("paivat", {})[paiva] = {"mittari": mittari, "jutut": jutut}
    raja = (datetime.fromisoformat(paiva) - timedelta(days=ARKISTO_PAIVAA)).date().isoformat()
    historia["paivat"] = {d: v for d, v in historia["paivat"].items() if d >= raja}
    return historia


def liittyvat(jutt, historia, tanaan, paivia=14, enintaan=3):
    A = sanat(jutt.get("otsikko", "") + " " + jutt.get("tiivistelma", ""))
    if len(A) < 3:
        return []
    raja = (datetime.fromisoformat(tanaan) - timedelta(days=paivia)).date().isoformat()
    ehdokkaat = []
    for d, v in (historia or {}).get("paivat", {}).items():
        if not (raja <= d < tanaan):
            continue
        for h in v.get("jutut", []):
            B = sanat(h["o"] + " " + h["t"])
            yht = len(A & B)
            if yht >= 3 and yht / max(1, min(len(A), len(B))) >= 0.3:
                ehdokkaat.append((yht, d, h))
    ehdokkaat.sort(key=lambda x: (x[0], x[1]), reverse=True)
    nahdyt, tulos = set(), []
    for _, d, h in ehdokkaat:
        if h["l"] in nahdyt or h["o"] in nahdyt:
            continue
        nahdyt.update((h["l"], h["o"]))
        tulos.append((d, h))
        if len(tulos) >= enintaan:
            break
    return tulos


def viikon_trendi(historia, tanaan):
    paivat = sorted(d for d in (historia or {}).get("paivat", {}) if d <= tanaan)[-7:]
    if len(paivat) < 2:
        return [], []
    lahteet = sorted({s for d in paivat for s in historia["paivat"][d]["mittari"]})
    rivit = []
    for s in lahteet:
        arvot = []
        for d in paivat:
            nk = historia["paivat"][d]["mittari"].get(s)
            arvot.append(round(100 * nk[1] / nk[0]) if nk and nk[0] else None)
        ok = [a for a in arvot if a is not None]
        if ok:
            rivit.append((s, arvot, round(sum(ok) / len(ok))))
    rivit.sort(key=lambda r: -r[2])
    return paivat, rivit


def kirjoita_haku(arkisto, historia):
    nahdyt, rivit = set(), []
    for d in sorted(historia.get("paivat", {}), reverse=True):
        for h in historia["paivat"][d]["jutut"]:
            k = h["l"] or h["o"]
            if k in nahdyt:
                continue
            nahdyt.add(k)
            rivit.append({"p": d, "o": h["o"], "t": h["t"], "l": h["l"]})
    (arkisto / "haku.json").write_text(json.dumps(rivit, ensure_ascii=False), encoding="utf-8")


# --- Push-ilmoitukset (ntfy.sh, valinnainen) ---------------------------------

def laheta_ilmoitukset(tulos, uutiset, tila, aihe):
    klikit = {i for i in tulos.get("klikkiotsikot", []) if isinstance(i, int)}
    ilmoitetut = tila.get("ilmoitetut", [])
    for j in tulos.get("jutut", []):
        if int(j.get("tarkeys", 0) or 0) < 5:
            continue
        linkki = paa_linkki(j, uutiset, klikit)
        avain_ = linkki or j.get("otsikko", "")
        if avain_ in ilmoitetut or j.get("otsikko", "") in ilmoitetut:
            continue
        viesti = {"topic": aihe, "title": j.get("otsikko", "")[:200],
                  "message": j.get("tiivistelma", "")[:900] or j.get("otsikko", ""),
                  "tags": ["newspaper"], "priority": 4}
        if linkki:
            viesti["click"] = linkki
        try:
            req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(viesti).encode(),
                                         headers={"content-type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            print(f"  Ilmoitus lähetetty: {viesti['title']}")
            ilmoitetut += [avain_, j.get("otsikko", "")]
        except Exception as e:
            print(f"  ! Ilmoitusta ei saatu lähetettyä ({e})", file=sys.stderr)
    tila["ilmoitetut"] = ilmoitetut[-300:]


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

7. Valitse klikkiotsikoista päivän pahin (eniten asiaa piilottava tai harhaanjohtava)
   ja perustele valinta yhdellä lyhyellä, hauskalla mutta asiallisella lauseella.

8. Kirjoita jokaiselle jutulle myös selkokielinen tiivistelmä kenttään "selko":
   1–3 lyhyttä lausetta, yleisiä sanoja, ei vierasperäisiä termejä eikä
   lyhenteitä ilman selitystä. Sisältö sama kuin tavallisessa tiivistelmässä.
9. Kirjoita jutuille, joiden tärkeys on 3 tai enemmän, kenttään "tausta"
   1–2 lausetta siitä, miksi asia on uutinen ja mihin laajempaan asiayhteyteen
   se liittyy. Käytä vain varmaa, yleisesti tunnettua taustatietoa äläkä keksi
   tapahtumasta mitään uutta. Jos et ole varma, jätä kenttä tyhjäksi.
   Muille jutuille "tausta" on tyhjä merkkijono.

Kirjoita lopuksi 3–5 lauseen yleiskatsaus päivän tärkeimmistä uutisista ja
sama selkokielellä kenttään "yleiskatsaus_selko".

Vastaa PELKKÄNÄ JSONina tässä muodossa, ei muuta tekstiä:
{
  "yleiskatsaus": "...",
  "yleiskatsaus_selko": "...",
  "jutut": [
    {"otsikko": "...", "tiivistelma": "...", "selko": "...", "tausta": "...",
     "luokka": "...", "tarkeys": 3, "uutiset": [0, 4]}
  ],
  "klikkiotsikot": [4, 7],
  "pahin": {"id": 7, "miksi": "..."}
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
    raise RuntimeError(f"Tiivistys epäonnistui kaikilla malleilla. Viimeisin virhe: {viimeisin}")


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
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141416;--card:#1e1e21;--ink:#ececee;
--muted:#9a9aa1;--line:#2c2c30;--accent:#4cc39a;--warn:#f5a35c;--warnbg:#3a2a1a;--bar:#2a2a2e;
--new:#8fb0ff;--newbg:#1c2640;--cheap:#4cc39a;--mid:#e0bd4a}}
:root[data-theme=dark]{--bg:#141416;--card:#1e1e21;--ink:#ececee;
--muted:#9a9aa1;--line:#2c2c30;--accent:#4cc39a;--warn:#f5a35c;--warnbg:#3a2a1a;--bar:#2a2a2e;
--new:#8fb0ff;--newbg:#1c2640;--cheap:#4cc39a;--mid:#e0bd4a}
[hidden]{display:none!important}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit}
body{overflow-x:hidden}main{zoom:var(--z,1);max-width:720px;margin:0 auto;padding:max(24px,env(safe-area-inset-top)) 16px 64px}
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
article{touch-action:pan-y;background:var(--card);border:1px solid var(--line);border-radius:12px;
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
.vs .alkup{color:var(--ink);font-style:italic}
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
#haku{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;
background:var(--card);color:var(--ink);font-size:15px;margin-bottom:16px}
.laatikko h4{font-size:13px;margin:16px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.as-rivi{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:14px}
.seg{display:flex;gap:4px;align-items:center}#as-koko{min-width:48px;text-align:center;font-size:13px}
.chipit{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:999px;
padding:5px 11px;font-size:13px;cursor:pointer}
.chip.fav{background:var(--newbg);color:var(--new);border-color:transparent;font-weight:600}
.chip.pois{opacity:.55;text-decoration:line-through}
.chip.on{border-color:var(--accent)}
.pahin{background:var(--warnbg);border-radius:8px;padding:10px 12px;margin-top:14px;font-size:14px}
.pahin .lbl{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
color:var(--warn);margin-bottom:4px}
.pahin a{font-style:italic;text-decoration:none}.pahin p{margin:6px 0 0;color:var(--muted)}
.spark{display:flex;align-items:flex-end;gap:3px;height:22px}
.spark i{flex:1;background:var(--warn);border-radius:2px 2px 0 0;opacity:.8}
.spark i.tyhja-pv{background:var(--bar);height:2px}
.aiemmin{margin-top:12px;border-top:1px solid var(--line);padding-top:10px;font-size:14px}
.aiemmin>span{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.aiemmin ul{margin:6px 0 0;padding-left:18px}.aiemmin li{margin-bottom:4px}
.aiemmin time{color:var(--muted);font-size:12px;margin-right:6px}
.aiemmin a{text-decoration:none}.aiemmin a:hover{text-decoration:underline}
.liikenne li{font-size:14px}.liikenne span{color:var(--muted);font-size:13px}
.tausta{font-size:14px;color:var(--muted);margin:-4px 0 10px}
.tausta b{color:var(--ink);font-weight:600}.tausta .ai{font-size:11px}
.oikeasti{margin-top:8px;padding-top:8px;border-top:1px dashed var(--warn);font-size:14px}
.lbl2{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
color:var(--accent);margin-bottom:2px}
footer{font-size:12px;color:var(--muted);text-align:center;margin-top:24px}
"""

JS = r"""
const ls={get(k){try{return localStorage.getItem(k)}catch(e){return null}},
set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const jget=(k,d)=>{try{return JSON.parse(ls.get(k))??d}catch(e){return d}};
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const avaimet=a=>a.dataset.k.split(' ');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pvm=d=>{const [y,m,p]=d.split('-');return `${+p}.${+m}.`};
const data=id=>{const e=document.getElementById(id);return e?JSON.parse(e.textContent):null};

// --- Asetukset (tallentuvat vain tähän selaimeen) ---
const AS={teema:ls.get('uk_teema')||'auto',zoom:+(ls.get('uk_zoom')||1),
aiheet:jget('uk_aiheet',{}),pois:new Set(jget('uk_mediat_pois',[])),selko:ls.get('uk_selko')==='1'};
function sovellaSelko(){$$('.selko').forEach(x=>{x.hidden=!AS.selko;const t=x.previousElementSibling;
if(t&&t.classList.contains('tiiv'))t.hidden=AS.selko})}
sovellaSelko();

// --- Suodatus: luokka, piilotetut aiheet ja mediat, luetut, haku ---
let luokka='Kaikki',piilota=jget('uk_piilota',false),haku='';
const luetut=new Set(jget('uk_luetut',[]));
function jarjesta(){const ank=$('#kortit');if(!ank)return;const k=$$('#kortit article');
const v=x=>AS.aiheet[x.dataset.l]==='fav'?0:1;k.sort((a,b)=>v(a)-v(b)||(+a.dataset.i)-(+b.dataset.i));
k.forEach(x=>ank.appendChild(x))}
function paivita(){let n=0;const q=haku.toLowerCase();
$$('article').forEach(a=>{const m=a.dataset.m.split('|');
const nak=(luokka==='Kaikki'||a.dataset.l===luokka)&&AS.aiheet[a.dataset.l]!=='pois'
&&m.some(x=>!AS.pois.has(x))&&!(piilota&&a.classList.contains('luettu'))
&&(!q||a.textContent.toLowerCase().includes(q));
a.style.display=nak?'':'none';if(nak)n++});
const t=$('.tyhja');if(t)t.style.display=n?'none':'block';
const p=$('.piilota');if(p)p.classList.toggle('on',piilota);
$$('.suodattimet button[data-l]').forEach(b=>b.hidden=AS.aiheet[b.dataset.l]==='pois')}
$$('.suodattimet button[data-l]').forEach(b=>b.onclick=()=>{luokka=b.dataset.l;
$$('.suodattimet button[data-l]').forEach(x=>x.classList.toggle('on',x===b));paivita()});
const pb=$('.piilota');if(pb)pb.onclick=()=>{piilota=!piilota;ls.set('uk_piilota',JSON.stringify(piilota));paivita()};

// --- Luetut: linkin avaaminen tai pyyhkäisy merkitsee jutun luetuksi ---
function merkitse(a,luettu){avaimet(a).forEach(k=>luettu?luetut.add(k):luetut.delete(k));
ls.set('uk_luetut',JSON.stringify([...luetut].slice(-1500)));a.classList.toggle('luettu',luettu)}
$$('article').forEach(a=>{if(avaimet(a).some(k=>luetut.has(k)))a.classList.add('luettu');
a.querySelectorAll('a').forEach(l=>l.addEventListener('click',()=>{merkitse(a,true);setTimeout(paivita,400)}));
let x0=null,y0=0,dx=0,vaaka=false;
a.addEventListener('touchstart',e=>{const t=e.touches[0];x0=t.clientX;y0=t.clientY;dx=0;vaaka=false;
a.style.transition='none'},{passive:true});
a.addEventListener('touchmove',e=>{if(x0===null)return;const t=e.touches[0];dx=t.clientX-x0;
if(!vaaka&&Math.abs(dx)>12&&Math.abs(dx)>Math.abs(t.clientY-y0)*1.5)vaaka=true;
if(vaaka)a.style.transform=`translateX(${dx}px)`},{passive:true});
a.addEventListener('touchend',()=>{a.style.transition='transform .2s';
if(vaaka&&Math.abs(dx)>90){merkitse(a,!a.classList.contains('luettu'));
a.style.transform=`translateX(${dx>0?110:-110}%)`;
setTimeout(()=>{a.style.transition='none';a.style.transform='';paivita()},220)}
else a.style.transform='';x0=null});});

// --- Uusi-merkinnät: juttu on uusi, jos siinä on linkki, jota et ole ennen nähnyt ---
(()=>{const raw=ls.get('uk_nahdyt');const nahdyt=new Set(raw?JSON.parse(raw):[]);const kaikki=[];let uusia=0;
$$('article').forEach(a=>{const k=avaimet(a);kaikki.push(...k);
if(raw&&k.some(x=>!nahdyt.has(x))){uusia++;a.querySelector('.meta').insertAdjacentHTML('afterbegin',
'<span class="tag uusi">Uusi</span>')}});
if(uusia)$('.paiva').insertAdjacentText('beforeend',` · ${uusia} uutta`);
const tallenna=()=>ls.set('uk_nahdyt',JSON.stringify([...new Set([...nahdyt,...kaikki])].slice(-1500)));
addEventListener('pagehide',tallenna);
document.addEventListener('visibilitychange',()=>{if(document.hidden)tallenna()});})();
jarjesta();paivita();

// --- Asetuspaneeli ---
(()=>{const el=$('#asetukset'),D=data('asetus-data');if(!el||!D)return;const root=document.documentElement;
function sovella(){if(AS.teema==='auto')delete root.dataset.theme;else root.dataset.theme=AS.teema;
root.style.setProperty('--z',AS.zoom)}
function piirra(){el.querySelectorAll('[data-selko]').forEach(b=>b.classList.toggle('on',(b.dataset.selko==='1')===AS.selko));
el.querySelectorAll('[data-teema]').forEach(b=>b.classList.toggle('on',b.dataset.teema===AS.teema));
$('#as-koko').textContent=Math.round(AS.zoom*100)+' %';
$('#as-aiheet').innerHTML=D.luokat.map(l=>{const t=AS.aiheet[l]||'';
return `<button class="chip ${t}" data-a="${esc(l)}">${t==='fav'?'⭐ ':t==='pois'?'🚫 ':''}${esc(l)}</button>`}).join('');
$('#as-mediat').innerHTML=D.mediat.map(m=>{const pois=AS.pois.has(m);
return `<button class="chip ${pois?'pois':'on'}" data-m="${esc(m)}">${pois?'🚫 ':'✓ '}${esc(m)}</button>`}).join('');
el.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>{const l=b.dataset.a,t=AS.aiheet[l]||'';
const s={'':'fav',fav:'pois',pois:''}[t];if(s)AS.aiheet[l]=s;else delete AS.aiheet[l];
ls.set('uk_aiheet',JSON.stringify(AS.aiheet));jarjesta();paivita();piirra()});
el.querySelectorAll('[data-m]').forEach(b=>b.onclick=()=>{const m=b.dataset.m;
AS.pois.has(m)?AS.pois.delete(m):AS.pois.add(m);ls.set('uk_mediat_pois',JSON.stringify([...AS.pois]));paivita();piirra()})}
el.querySelectorAll('[data-selko]').forEach(b=>b.onclick=()=>{AS.selko=b.dataset.selko==='1';
ls.set('uk_selko',AS.selko?'1':'0');sovellaSelko();piirra()});
el.querySelectorAll('[data-teema]').forEach(b=>b.onclick=()=>{AS.teema=b.dataset.teema;ls.set('uk_teema',AS.teema);sovella();piirra()});
$('#as-pienempi').onclick=()=>{AS.zoom=Math.max(.8,+(AS.zoom-.1).toFixed(1));ls.set('uk_zoom',AS.zoom);sovella();piirra()};
$('#as-isompi').onclick=()=>{AS.zoom=Math.min(1.6,+(AS.zoom+.1).toFixed(1));ls.set('uk_zoom',AS.zoom);sovella();piirra()};
const avaa=$('#avaa-asetukset');if(avaa)avaa.onclick=()=>{el.hidden=!el.hidden;if(!el.hidden)el.scrollIntoView({behavior:'smooth',block:'start'})};
$('#sulje-asetukset').onclick=()=>{el.hidden=true};
sovella();piirra()})();

// --- Haku: tämän päivän jutut ja arkisto ---
(()=>{const i=$('#haku'),out=$('#hakutulokset');if(!i)return;let D=null,ajastin;
const sivulla=new Set($$('article a').map(a=>a.getAttribute('href')));
i.addEventListener('input',()=>{clearTimeout(ajastin);ajastin=setTimeout(async()=>{haku=i.value.trim();paivita();
const q=haku.toLowerCase();if(q.length<3){out.hidden=true;return}
if(!D){try{D=await(await fetch(i.dataset.src)).json()}catch(e){D=[]}}
const os=D.filter(r=>!sivulla.has(r.l)&&(r.o+' '+r.t).toLowerCase().includes(q)).slice(0,15);
out.hidden=false;out.innerHTML=`<h3>Arkistosta (${os.length})</h3>`+(os.length?'<ul>'+os.map(r=>
`<li><time>${pvm(r.p)}</time><a href="${esc(r.l)}" target="_blank" rel="noopener">${esc(r.o)}</a></li>`).join('')+'</ul>'
:'<p class="selite">Ei osumia arkistossa.</p>')},250)})})();

// --- Kuuntele: koko kooste tai viisi tärkeintä ---
(()=>{const bs=[$('#kuuntele'),$('#top5')].filter(Boolean);if(!bs.length)return;
if(!('speechSynthesis' in window)){bs.forEach(b=>b.remove());return}
const s=speechSynthesis;const lepo=new Map(bs.map(b=>[b,b.textContent]));
const nollaa=()=>bs.forEach(b=>{b.textContent=lepo.get(b)});
const nakyvat=()=>$$('article').filter(a=>a.style.display!=='none');
const naky=sel=>[...document.querySelectorAll(sel)].filter(x=>!x.hidden).map(x=>x.textContent).join(' ');
const teksti=a=>a.querySelector('h2').textContent+'. '+[...a.querySelectorAll('p.teksti')].filter(x=>!x.hidden).map(x=>x.textContent).join(' ');
bs.forEach(b=>b.onclick=()=>{if(s.speaking||s.pending){s.cancel();nollaa();return}
const osat=b.id==='top5'?['Päivän viisi tärkeintä uutista.',...nakyvat().slice(0,5).map(teksti)]
:[naky('.katsaus>span'),...nakyvat().map(teksti)];
const v=s.getVoices().find(v=>(v.lang||'').toLowerCase().startsWith('fi'));
osat.forEach((t,i)=>{const u=new SpeechSynthesisUtterance(t);u.lang='fi-FI';if(v)u.voice=v;
if(i===osat.length-1)u.onend=nollaa;s.speak(u)});b.textContent='⏹ Lopeta'});
s.getVoices();})();

// --- Asenna sovellukseksi ---
(()=>{let ev=null;const b=$('#asenna');if(!b)return;
addEventListener('beforeinstallprompt',e=>{e.preventDefault();ev=e;b.hidden=false});
b.onclick=async()=>{if(!ev)return;ev.prompt();await ev.userChoice;ev=null;b.hidden=true};
if('serviceWorker' in navigator&&document.body.dataset.sw)navigator.serviceWorker.register(document.body.dataset.sw).catch(()=>{});})();

// --- Pörssisähkö ---
(()=>{const el=$('#sahko'),D=data('sahko-data');if(!el||!D)return;
const f=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Helsinki',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hour12:false});
const osa=Object.fromEntries(f.formatToParts(new Date()).map(p=>[p.type,p.value]));
const tanaan=`${osa.year}-${osa.month}-${osa.day}`,tunti=+osa.hour%24;
const huom=new Date(Date.parse(tanaan+'T12:00:00Z')+864e5).toISOString().slice(0,10);
const pv=[[tanaan,'Tänään'],[huom,'Huomenna']].filter(([d])=>D[d]);
if(!pv.length){el.remove();const b=$('#nayta-sahko');if(b)b.remove();return}
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

// --- Lähiuutiset ja liikennetiedotteet ---
(()=>{const el=$('#paik'),D=data('paik-data')||{},L=data('liikenne-data')||[];if(!el)return;
const alueet=Object.keys(D).sort((a,b)=>a.localeCompare(b,'fi'));let alue=ls.get('uk_alue');
const VARTALO={'Uusimaa':'Uudenmaa','Lappi':'Lapin','Satakunta':'Satakunn','Varsinais-Suomi':'Varsinais-Suom','Keski-Suomi':'Keski-Suom'};
const km=(a,b,c,d)=>{const r=Math.PI/180,x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return 12742*Math.asin(Math.sqrt(x))};
const klo=a=>a?new Date(a).toLocaleTimeString('fi-FI',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Helsinki'}):'';
function lahella(x){const v=alue&&(VARTALO[alue]||alue);
if(v&&x.al.some(n=>n.startsWith(alue)||n.startsWith(v)))return true;
const p=jget('uk_paikka',null);return !!(p&&x.p&&km(p.lat,p.lon,x.p[1],x.p[0])<50)}
function piirra(){const opt=alueet.map(a=>`<option ${a===alue?'selected':''}>${esc(a)}</option>`).join('');
const lista=alue&&D[alue]?'<ul>'+D[alue].map(u=>`<li><time>${klo(u.a)}</time><a href="${esc(u.l)}" target="_blank" rel="noopener">${esc(u.o)}</a></li>`).join('')+'</ul>'
:'<p class="selite">Valitse maakunta, niin näet Ylen alueuutiset ja alueen liikennetiedotteet. Valinta tallentuu vain tähän selaimeen.</p>';
const lt=L.filter(lahella).slice(0,8);
const liik=(alue||jget('uk_paikka',null))?`<h4>🚧 Liikennetiedotteet</h4>`+(lt.length?'<ul class="liikenne">'+lt.map(x=>
`<li><b>${esc(x.o)}</b>${x.t?`<br><span>${esc(x.t)}</span>`:''}</li>`).join('')+'</ul>'
:'<p class="selite">Ei voimassa olevia liikennetiedotteita alueella.</p>')+'<p class="selite">Lähde: Fintraffic / Digitraffic</p>':'';
el.innerHTML=`<h3><span>📍 Lähiuutiset</span><select><option value="">Valitse maakunta</option>${opt}</select></h3>${lista}${liik}`;
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
saa.innerHTML=`<h3>Sää · ${esc(p.nimi)}</h3><div class="saa-nyt"><span class="saa-ikoni">${ik}</span>
<div><div class="saa-lampo">${Math.round(c.temperature_2m)}°</div>
<div class="saa-teksti">${tx} · tuntuu ${Math.round(c.apparent_temperature)}° · tuuli ${Math.round(c.wind_speed_10m)} m/s</div></div></div>
<div class="saa-paivat">${paivat}</div>
<p class="selite"><button class="linkkinappi" id="vaihda">Vaihda paikkaa</button> · Lähde: Open-Meteo</p>`;
saa.querySelector('#vaihda').onclick=()=>lomake();}catch(e){lomake('Säätä ei saatu haettua.')}}
// --- Sää- ja sähkönapit: laatikot aukeavat napista, auki-tila muistetaan ---
(()=>{let saaHaettu=false;
function avaa(id,nappi,auki){const el=$('#'+id),b=$('#'+nappi);if(!el||!b)return;
el.hidden=!auki;b.classList.toggle('on',auki);ls.set('uk_auki_'+id,auki?'1':'0');
if(auki&&id==='saa'&&!saaHaettu){saaHaettu=true;const p=jget('uk_paikka',null);p?naytaSaa(p):lomake()}}
[['saa','nayta-saa'],['sahko','nayta-sahko'],['mittari','nayta-mittari'],['paik','nayta-paik']].forEach(([id,n])=>{const b=$('#'+n),el=$('#'+id);if(!b||!el)return;
b.onclick=()=>avaa(id,n,el.hidden);if(ls.get('uk_auki_'+id)==='1')avaa(id,n,true)})})();
"""

ALKUSKRIPTI = ("<script>try{const t=localStorage.getItem('uk_teema');if(t&&t!=='auto')"
               "document.documentElement.dataset.theme=t;const z=localStorage.getItem('uk_zoom');"
               "if(z)document.documentElement.style.setProperty('--z',z)}catch(e){}</script>")

LUOKAT = ["Kotimaa", "Ulkomaat", "Politiikka", "Talous", "Urheilu", "Kulttuuri",
          "Tiede ja teknologia", "Viihde", "Muut"]


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


def klo(iso):
    t = lue_aika(iso)
    return f"{t.astimezone(HKI):%H.%M}" if t else ""


def tee_html(tulos, uutiset, maakunnat=None, sahko=None, juuri="", arkistoitu=None,
             tiivistetty=None, historia=None, liikenne=None, urheilu=None):
    """juuri = suhteellinen polku sivuston juureen ('' etusivulle, '../' arkistolle).
    arkistoitu = päivämääräteksti, jos sivu on arkistokopio."""
    e = html.escape
    jutut = sorted(tulos.get("jutut", []),
                   key=lambda j: (-int(j.get("tarkeys", 0) or 0), -len(j.get("uutiset", []))))
    klikit = {i for i in tulos.get("klikkiotsikot", []) if isinstance(i, int)}
    t = datetime.now(HKI)
    nyt = f"{t.day}.{t.month}.{t.year} klo {t:%H.%M}"
    tanaan = t.date().isoformat()
    VK = ["ma", "ti", "ke", "to", "pe", "la", "su"]

    # Klikkimittari, päivän pahin ja viikon trendi
    mittari = "".join(
        f'<span>{e(nimi)}</span><div class="palkki"><i style="width:{pros}%"></i></div>'
        f'<span class="luku">{pros} % <small>({k}/{n})</small></span>'
        for nimi, k, n, pros in klikkimittari(uutiset, klikit))
    pahin_html = ""
    pahin = tulos.get("pahin") or {}
    if isinstance(pahin, dict) and isinstance(pahin.get("id"), int) and 0 <= pahin["id"] < len(uutiset):
        u = uutiset[pahin["id"]]
        oikea = next((j for j in jutut if pahin["id"] in j.get("uutiset", [])), None)
        oikea_html = (f'<div class="oikeasti"><span class="lbl2">Mistä oikeasti on kyse</span>'
                      f'<b>{e(oikea.get("otsikko",""))}</b> {e(oikea.get("tiivistelma",""))}</div>') if oikea else ""
        pahin_html = (f'<div class="pahin"><span class="lbl">🏆 Päivän pahin klikkiotsikko · {e(u["lahde"])}</span>'
                      f'<a href="{e(u["linkki"])}" target="_blank" rel="noopener">”{e(u["otsikko"])}”</a>'
                      f'{oikea_html}'
                      f'{"<p>" + e(pahin.get("miksi", "")) + "</p>" if pahin.get("miksi") else ""}</div>')
    paivat, trendi = viikon_trendi(historia, tanaan) if historia else ([], [])
    trendi_html = ""
    if trendi:
        rivit = "".join(
            f'<span>{e(s)}</span><div class="spark">' +
            "".join(f'<i style="height:{max(2, a)}%" title="{int(d[8:])}.{int(d[5:7])}.: {a} %"></i>'
                    if a is not None else '<i class="tyhja-pv"></i>' for a, d in zip(arvot, paivat)) +
            f'</div><span class="luku">ka {ka} %</span>' for s, arvot, ka in trendi)
        trendi_html = (f'<h4>Trendi, {len(paivat)} päivää</h4><div class="mittari">{rivit}</div>'
                       f'<p class="selite">Vanhin päivä vasemmalla, tänään oikealla.</p>')

    kortit = []
    for nro, j in enumerate(jutut):
        idt = [i for i in j.get("uutiset", []) if isinstance(i, int) and 0 <= i < len(uutiset)]
        if not idt:
            continue
        paa = not kortit
        paalinkki = paa_linkki(j, uutiset, klikit)
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
                  f'<span class="alkup">”{e(u["otsikko"])}”</span></div>')
        ajat = [uutiset[i]["aika"] for i in idt if uutiset[i]["aika"]]
        aika = f'<span>klo {max(ajat).astimezone(HKI):%H.%M}</span>' if ajat else ""
        n = len(idt)
        meta = (f'<div class="meta">{"<span class=paa-lbl>Päivän pääuutinen</span>" if paa else ""}'
                f'<span class="tag">{e(j.get("luokka","Muut"))}</span>{aika}'
                f'<span>{n} {"lähde" if n == 1 else "lähdettä"}</span></div>')
        otsikko = (f'<h2><a href="{e(paalinkki)}" target="_blank" rel="noopener">'
                   f'{e(j.get("otsikko",""))}</a></h2>')
        teksti = f'<p class="teksti tiiv">{e(j.get("tiivistelma",""))}</p>'
        if j.get("selko"):
            teksti += f'<p class="teksti selko" hidden>{e(j["selko"])}</p>'
        if j.get("tausta"):
            teksti += (f'<p class="tausta"><b>Miksi tämä on uutinen?</b> {e(j["tausta"])}'
                       f' <span class="ai">(tekoälyn taustatieto)</span></p>')
        aiemmin = liittyvat(j, historia, tanaan) if historia else []
        aiemmin_html = ("<div class=aiemmin><span>Aiemmin aiheesta</span><ul>" + "".join(
            f'<li><time>{VK[datetime.fromisoformat(d).weekday()]} {int(d[8:])}.{int(d[5:7])}.</time>'
            f'<a href="{e(h["l"])}" target="_blank" rel="noopener">{e(h["o"])}</a></li>'
            for d, h in aiemmin) + "</ul></div>") if aiemmin else ""
        avaimet = " ".join(e(uutiset[i]["linkki"] or uutiset[i]["otsikko"]).replace(" ", "_") for i in idt)
        mediat = e("|".join(dict.fromkeys(uutiset[i]["lahde"] for i in idt)))
        if paa:
            runko = (f'{img("iso")}<div class="sisus">{meta}{vs}{otsikko}{teksti}'
                     f'<div class="linkit">{linkit}</div>{aiemmin_html}</div>')
        else:
            runko = (f'<div class="sisus">{meta}{vs}<div class="ylaosa"><div>{otsikko}{teksti}</div>'
                     f'{img("peukku")}</div><div class="linkit">{linkit}</div>{aiemmin_html}</div>')
        kortit.append(f'<article class="{"paa" if paa else ""}" data-l="{e(j.get("luokka","Muut"))}" '
                      f'data-i="{nro}" data-m="{mediat}" data-k="{avaimet}">{runko}</article>')

    luokat = sorted({j.get("luokka", "Muut") for j in jutut})
    napit = "".join(f'<button data-l="{e(l)}">{e(l)}</button>' for l in luokat)
    lahteet = list(dict.fromkeys(u["lahde"] for u in uutiset))
    kaikki_luokat = LUOKAT + [l for l in luokat if l not in LUOKAT]

    if arkistoitu:
        otsake = (f'<p class="paiva">Arkistoitu kooste {e(arkistoitu)}</p>'
                  f'<div class="tyokalut"><a class="nappi" href="{juuri}">← Tuorein kooste</a>'
                  f'<a class="nappi" href="{juuri}arkisto/">Arkisto</a></div>')
    else:
        otsake = (f'<p class="paiva">Päivitetty {nyt} · {len(uutiset)} uutista, {len(jutut)} juttua'
                  f'{" · tiivistetty klo " + e(tiivistetty) if tiivistetty else ""}</p>'
                  f'<div class="tyokalut"><button class="nappi" id="kuuntele">🔊 Kuuntele</button>'
                  f'<button class="nappi" id="top5">🔊 Top 5</button>'
                  f'<button class="nappi" id="nayta-saa">🌤 Sää</button>'
                  f'{"<button class=nappi id=nayta-sahko>⚡ Sähkö</button>" if sahko else ""}'
                  f'{"<button class=nappi id=nayta-paik>📍 Lähiuutiset</button>" if (maakunnat or liikenne) else ""}'
                  f'<button class="nappi" id="nayta-mittari">📊 Klikkimittari</button>'
                  f'<a class="nappi" href="{juuri}arkisto/">🗂 Arkisto</a>'
                  f'<button class="nappi" id="avaa-asetukset">⚙️ Asetukset</button>'
                  f'<button class="nappi" id="asenna" hidden>📲 Asenna</button></div>'
                  f'<input id="haku" type="search" placeholder="🔍 Hae uutisista ja arkistosta…" '
                  f'data-src="{juuri}arkisto/haku.json" autocomplete="off">'
                  f'<div class="laatikko paik" id="hakutulokset" hidden></div>'
                  f'<div class="laatikko" id="asetukset" hidden>'
                  f'<h3><span>⚙️ Asetukset</span><button class="linkkinappi" id="sulje-asetukset">Sulje</button></h3>'
                  f'<div class="as-rivi"><span>Teema</span><span class="seg">'
                  f'<button class="nappi pieni" data-teema="auto">Auto</button>'
                  f'<button class="nappi pieni" data-teema="light">Vaalea</button>'
                  f'<button class="nappi pieni" data-teema="dark">Tumma</button></span></div>'
                  f'<div class="as-rivi"><span>Selkokieli</span><span class="seg">'
                  f'<button class="nappi pieni" data-selko="0">Pois</button>'
                  f'<button class="nappi pieni" data-selko="1">Päällä</button></span></div>'
                  f'<div class="as-rivi"><span>Tekstikoko</span><span class="seg">'
                  f'<button class="nappi pieni" id="as-pienempi">A−</button><span id="as-koko"></span>'
                  f'<button class="nappi pieni" id="as-isompi">A+</button></span></div>'
                  f'<h4>Aiheet</h4><p class="selite">Napauta: ⭐ suosikki (nousee ylemmäs) → 🚫 piilota → tavallinen</p>'
                  f'<div class="chipit" id="as-aiheet"></div>'
                  f'<h4>Mediat</h4><p class="selite">Piilotetun median jutut näkyvät vain, jos joku muu media uutisoi samasta.</p>'
                  f'<div class="chipit" id="as-mediat"></div>'
                  f'<p class="selite">Vinkki: pyyhkäise juttua sivulle, niin se merkitään luetuksi (tai takaisin lukemattomaksi).</p></div>'
                  f'{json_skripti("asetus-data", {"luokat": kaikki_luokat, "mediat": lahteet})}')
    sahko_html = (f'<div class="laatikko" id="sahko" hidden></div>{json_skripti("sahko-data", sahko)}'
                  if sahko and not arkistoitu else "")
    paik_html = (f'<div class="laatikko paik" id="paik" hidden></div>{json_skripti("paik-data", maakunnat or {})}'
                 f'{json_skripti("liikenne-data", liikenne or [])}'
                 if (maakunnat or liikenne) and not arkistoitu else "")
    urheilu_html = ("<div class='laatikko paik'><h3>🏆 Urheilu</h3><ul>" + "".join(
        f'<li><time>{klo(u["a"])}</time><a href="{e(u["l"])}" target="_blank" rel="noopener">{e(u["o"])}</a></li>'
        for u in urheilu) + "</ul><p class='selite'>Ylen urheilu-uutiset sellaisenaan.</p></div>"
    ) if urheilu and not arkistoitu else ""
    saa_html = '<div class="laatikko" id="saa" hidden></div>' if not arkistoitu else ""

    return f"""<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b6e4f">{ALKUSKRIPTI}
<link rel="manifest" href="{juuri}manifest.webmanifest">
<link rel="icon" href="{juuri}icon-192.png"><link rel="apple-touch-icon" href="{juuri}icon-192.png">
<title>Uutiskooste{' ' + e(arkistoitu) if arkistoitu else ''}</title><style>{CSS}</style></head>
<body data-sw="{'' if arkistoitu else 'sw.js'}"><main>
<h1>Uutiskooste</h1>{otsake}
{saa_html}{sahko_html}{paik_html}
<div class="laatikko katsaus"><span class="tiiv">{e(tulos.get('yleiskatsaus',''))}</span>{('<span class="selko" hidden>' + e(tulos['yleiskatsaus_selko']) + '</span>') if tulos.get('yleiskatsaus_selko') else ''}</div>
<div class="suodattimet"><button class="on" data-l="Kaikki">Kaikki</button>{napit}
<button class="piilota">Piilota luetut</button></div>
{''.join(kortit[:1])}
<div class="laatikko" id="mittari"{'' if arkistoitu else ' hidden'}><h3>Klikkimittari – klikkiotsikoiden osuus</h3>
<div class="mittari">{mittari}</div>
<p class="selite">Gemini arvioi jokaisen otsikon. Viimeiset 24 tuntia.</p>{pahin_html}{trendi_html}</div>
<div id="kortit">{''.join(kortit[1:])}</div>
{urheilu_html}
<p class="tyhja">Ei näytettäviä juttuja. Kaikki on luettu! 🎉</p>
<footer>Lähteet: {e(", ".join(lahteet))}, Ylen alueuutiset · Tiivistelmät: Google Gemini ·
Sää: Open-Meteo · Sähkö: porssisahko.net · Liikenne: Fintraffic</footer>
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

def paivita_arkisto(arkisto, tulos, uutiset, maakunnat, historia=None):
    """Tallentaa päivän koosteen arkistoon (päivän viimeinen ajo jää voimaan)."""
    arkisto.mkdir(parents=True, exist_ok=True)
    t = datetime.now(HKI)
    teksti = f"{t.day}.{t.month}.{t.year}"
    (arkisto / f"{t.date().isoformat()}.html").write_text(
        tee_html(tulos, uutiset, maakunnat, juuri="../", arkistoitu=teksti, historia=historia), encoding="utf-8")
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

# --- Tila: edellinen tiivistys ------------------------------------------------

def lue_tila(polku):
    try:
        tila = json.loads(polku.read_text(encoding="utf-8"))
        for u in tila.get("uutiset", []):
            u["aika"] = lue_aika(u.get("aika"))
        return tila
    except (OSError, ValueError):
        return {}


def tallenna_tila(polku, tila):
    data = dict(tila)
    data["uutiset"] = [{**u, "aika": u["aika"].isoformat() if u["aika"] else None}
                       for u in tila["uutiset"]]
    polku.parent.mkdir(parents=True, exist_ok=True)
    polku.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def avain(u):
    return u["linkki"] or u["otsikko"]


def main():
    p = argparse.ArgumentParser(description="Uutiskooste – tiivistää uutisotsikot.")
    p.add_argument("--tunnit", type=int, default=24, help="aikaikkuna tunteina (oletus 24)")
    p.add_argument("--malli", help=f"Gemini-malli (oletus: {', '.join(MALLIT)})")
    p.add_argument("--ulos", default="site/index.html", help="tulostiedosto")
    p.add_argument("--arkisto", help="arkistokansio repossa (esim. arkisto); tallentaa myös tilan")
    p.add_argument("--pakota", action="store_true", help="tiivistä aina, ohita säästösäännöt")
    p.add_argument("--ei-avausta", action="store_true", help="älä avaa selainta")
    a = p.parse_args()

    avain_api = os.environ.get("GEMINI_API_KEY")
    if not avain_api:
        sys.exit("Aseta ensin ympäristömuuttuja GEMINI_API_KEY.")
    mallit = [a.malli] if a.malli else MALLIT
    tilapolku = Path(a.arkisto or ".").resolve() / "tila.json"
    tila = lue_tila(tilapolku)

    print("Haetaan syötteitä…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_uutiset = ex.submit(hae_kaikki, a.tunnit)
        f_maakunnat = ex.submit(hae_maakunnat)
        f_sahko = ex.submit(hae_sahko)
        f_liikenne = ex.submit(hae_liikenne)
        f_urheilu = ex.submit(hae_urheilu)
        uutiset, maakunnat, sahko = f_uutiset.result(), f_maakunnat.result(), f_sahko.result()
        liikenne, urheilu = f_liikenne.result(), f_urheilu.result()
    historiapolku = tilapolku.parent / "historia.json"
    try:
        historia = json.loads(historiapolku.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        historia = {}

    # Päätetään, tehdäänkö uusi tiivistys
    nyt = datetime.now(HKI)
    tanaan = nyt.date().isoformat()
    kertoja = tila.get("kertoja", 0) if tila.get("paiva") == tanaan else 0
    edelliset = {avain(u) for u in tila.get("uutiset", [])}
    uusia = sum(1 for u in uutiset if avain(u) not in edelliset)
    onko_vanha = bool(tila.get("tulos"))
    if not uutiset and not onko_vanha:
        sys.exit("Yhtään uutista ei saatu haettua.")
    if a.pakota or not onko_vanha:
        tiivista, syy = True, "pakotettu" if a.pakota else "ei aiempaa tiivistystä"
    elif kertoja >= PAIVARAJA:
        tiivista, syy = False, f"päiväraja {PAIVARAJA} täynnä"
    elif uusia < UUSIA_VAHINTAAN:
        tiivista, syy = False, f"vain {uusia} uutta otsikkoa (raja {UUSIA_VAHINTAAN})"
    else:
        tiivista, syy = True, f"{uusia} uutta otsikkoa"
    print(f"Tiivistys {'tehdään' if tiivista else 'ohitetaan'}: {syy} · tänään {kertoja}/{PAIVARAJA}")

    if tiivista and uutiset:
        try:
            print(f"Tiivistetään {len(uutiset)} uutista…")
            tulos = kutsu_gemini(uutiset, mallit, avain_api)
            ilmoitetut = tila.get("ilmoitetut", [])
            tila = {"paiva": tanaan, "kertoja": kertoja + 1, "tulos": tulos,
                    "uutiset": uutiset, "aika": f"{nyt:%H.%M}", "ilmoitetut": ilmoitetut}
            aihe = os.environ.get("NTFY_TOPIC", "").strip()
            if aihe and onko_vanha:   # ei ilmoituksia ensimmäisellä ajolla
                laheta_ilmoitukset(tulos, uutiset, tila, aihe)
            tallenna_tila(tilapolku, tila)
            historia = paivita_historia(historia, tulos, uutiset, tanaan)
            historiapolku.write_text(json.dumps(historia, ensure_ascii=False), encoding="utf-8")
        except RuntimeError as e:
            if not onko_vanha:
                sys.exit(str(e))
            print(f"  ! {e}\n  Käytetään edellistä tiivistystä.", file=sys.stderr)
    tulos, uutiset_sivulle = tila["tulos"], tila["uutiset"]

    polku = Path(a.ulos).resolve()
    polku.parent.mkdir(parents=True, exist_ok=True)
    polku.write_text(tee_html(tulos, uutiset_sivulle, maakunnat, sahko, tiivistetty=tila.get("aika"),
                              historia=historia, liikenne=liikenne, urheilu=urheilu), encoding="utf-8")
    kirjoita_sovellustiedostot(polku.parent)
    if a.arkisto:
        arkisto = Path(a.arkisto).resolve()
        paivita_arkisto(arkisto, tulos, uutiset_sivulle, maakunnat, historia)
        kirjoita_haku(arkisto, historia)
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

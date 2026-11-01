"""
Teil E: Andys eigene Belege (Ausgangsrechnungen + Auftragsbestätigungen) auslesen und verrechnen.

Die PDFs lädt eine Aufgabe auf Andys PC aus OneDrive hoch (scripts/belege_hochladen.ps1 → Tabelle ausgangsbelege,
Speicher "ausgangsbelege"). Dieses Skript läuft danach im GitHub-Ablauf (.github/workflows/belege.yml) und
  1. liest Belegart, Nummer, Datum und Beträge aus (PDF-Text; reine Bild-PDFs per Texterkennung → immer „bitte prüfen“),
  2. verrechnet: Stornorechnung hebt die genannte Rechnung auf, Schlussrechnung löst die in ihrer Abzugsliste
     genannten Abschläge ab (und bestätigt deren Beträge), bei mehreren Fassungen derselben Nummer zählt die neueste,
  3. trägt je Kunde/Projekt (aus dem Ordnernamen) einen Zuordnungs-VORSCHLAG ein – zugeordnet wird nur, was Andy
     in der App bestätigt (Vorgabe: nie automatisch zuordnen),
  4. legt für bestätigte Zuordnungen die Kundenrechnungen auf der Seite „Rechnungen“ an (Tabelle rechnungen,
     data.quelle = 'beleg'). Schlussrechnung = Restbetrag (Gesamt − Abschläge), damit nichts doppelt zählt.
     Was Andy dort selbst ändert (Betrag, Status, Notiz …), überschreibt das Skript nie.
Die Auftragssumme je Projekt rechnet die App selbst aus diesen Belegen (Schlussrechnung/Rechnungen > Auftragsbestätigung).

WICHTIG: Repository und Ablauf-Protokolle sind öffentlich → nur Zahlen ausgeben, keine Namen/Ordner/Beträge.
"""

import datetime
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_rechnungen import (BETRAG_RE, SB_KEY, SB_URL, _passt, jetzt, norm_tokens,  # noqa: E402
                             sb_request, sb_select_all, zahl)

BUCKET = "ausgangsbelege"
ID_BASIS = 5_000_000_000_000          # Einträge in „rechnungen“: id = ID_BASIS + Beleg-id (App nutzt Date.now())
GEPRUEFT = ("bestaetigt",)            # von Andy bestätigt → Beträge nie automatisch ändern
RECHNUNGSARTEN = ("abschlag", "schluss", "rechnung")
# Ob bezahlt wurde, steht nicht im PDF. Andy (01.11.2026): Rechnungen bis einschließlich 30.09.2026 als bezahlt anlegen,
# neuere als offen. Gilt nur beim Anlegen; danach ändert das Skript den Status nie (außer „storniert“).
BEZAHLT_BIS = "2026-09-30"


# ════════════════════════════════════
# Ordnernamen (Andys feste Ablage)
# ════════════════════════════════════
#   003 Rechnungen\Rechnung 4447-26_Kunde_Projekt_SR        (…_1.AZ · …_SR · …_Storno 4443 · … 2. Rechnung · sonst Einzelrechnung)
#   002 Aufträge\01 Aufträge\AuftragsNr. 3174-26_Kunde_Projekt

RE_ORDNER = re.compile(r"^Rechnung\s+(\d{4})-(\d{2})[_\s]+(.*)$", re.IGNORECASE)
AB_ORDNER = re.compile(r"^AuftragsNr[._\s]*(\d{4})-(\d{2})[_\s-]+(.*)$", re.IGNORECASE)
STORNO_TEIL = re.compile(r"(?:^|[_\s]+)(?:stornorechnung|storno|stornu)\b(.*)$", re.IGNORECASE)
AZ_TEIL = re.compile(r"[_\s]*(\d{1,2})\s*\.\s*AZ\b.*$", re.IGNORECASE)
SR_TEIL = re.compile(r"[_\s]+SR\b.*$", re.IGNORECASE)
FOLGE_TEIL = re.compile(r"[_\s]*\d{1,2}\s*\.\s*Rechnung\b.*$", re.IGNORECASE)


def norm_gruppe(s):
    s = unicodedata.normalize("NFC", s or "").lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def ordner_info(ordner, quelle):
    """Nummer, Belegart laut Ordnername, Gruppe (Kunde+Projekt), Bezug (Storno → stornierte Nummer), Abschlag-Nr."""
    info = {"nummer": None, "art": None, "gruppe": None, "bezug": None, "az_nr": None}
    if quelle == "auftrag":
        m = AB_ORDNER.match(ordner or "")
        if m:
            info.update(nummer=f"{m.group(1)}-{m.group(2)}", art="auftrag", gruppe=norm_gruppe(m.group(3)))
        return info
    m = RE_ORDNER.match(ordner or "")
    if not m:
        return info
    nr, jj, rest = m.group(1), m.group(2), m.group(3)
    info["nummer"] = f"{nr}-{jj}"
    s = STORNO_TEIL.search(rest)
    if s:
        info["art"] = "storno"
        b = re.search(r"(\d{4})(?:-(\d{2}))?", s.group(1))
        if b:
            info["bezug"] = f"{b.group(1)}-{b.group(2) or jj}"
        rest = rest[:s.start()]
    else:
        a = AZ_TEIL.search(rest)
        if a:
            info.update(art="abschlag", az_nr=int(a.group(1)))
            rest = rest[:a.start()]
        elif SR_TEIL.search(rest):
            info["art"] = "schluss"
            rest = rest[:SR_TEIL.search(rest).start()]
        else:
            f = FOLGE_TEIL.search(rest)
            if f:
                rest = rest[:f.start()]
            info["art"] = "rechnung"
    info["gruppe"] = norm_gruppe(rest)
    return info


ALLGEMEIN = {"reihenhaus", "wohnung", "haus", "muenchen", "provision", "planung", "besichtigung", "trockenbau", "trockenbauwand",
             "estrich", "fliesen", "garagentor", "baubegleitung", "baubetreuung", "anteil"}


STRASSE_RE = re.compile(r"(?:str|strasse|weg|platz|allee|ring|gasse|anger)$")


def vorschlag(gruppe, projekte):
    """Nur ein VORSCHLAG (Andy bestätigt). Zählt nur unterscheidende Wörter: Wörter aus Buchstaben (mind. 4),
    die genau EIN Projekt hat – nicht den Generalunternehmer, „Renovierung“ oder Hausnummern. Vorgeschlagen wird nur,
    wenn der Kunde (erstes Wort; bei Kunden mit mehreren Projekten das zweite) oder eine Straße passt,
    oder wenigstens zwei solche Wörter – ein Ort allein („Haar“) reicht nicht."""
    gt = [t for t in norm_tokens(gruppe) if len(t) >= 4 and t.isalpha() and t not in ALLGEMEIN
          and not t.endswith(("arbeiten", "sanierung", "renovierung", "renvoierung"))]
    pt = {pid: norm_tokens(text) for pid, text in projekte}

    def projekte_mit(t):
        return [pid for pid, toks in pt.items() if any(_passt(t, p) for p in toks)]

    stark = set(gt[:1])
    if gt and len(projekte_mit(gt[0])) > 1 and len(gt) > 1:   # z. B. Generalunternehmer → eigentlicher Kunde/BV dahinter
        stark.add(gt[1])
    treffer = {}
    for t in gt:
        bei = projekte_mit(t)
        if len(bei) == 1:
            n, s = treffer.get(bei[0], (0, 0))
            treffer[bei[0]] = (n + 1, s + (t in stark or bool(STRASSE_RE.search(t))))
    kand = sorted(((s, n, pid) for pid, (n, s) in treffer.items() if s >= 1 or n >= 2), reverse=True)
    if not kand or (len(kand) > 1 and kand[1][:2] == kand[0][:2]):
        return None   # nichts Belastbares oder zwei Projekte gleich gut → kein Vorschlag
    return kand[0][2]


def nr4(nummer):
    """„4447-26“ / „04447/26“ → „4447“ (Andys Nummern sind jahresübergreifend fortlaufend)."""
    m = re.search(r"0?(\d{4})\s*[-/]\s*\d{2}", nummer or "") or re.search(r"(\d{4})", nummer or "")
    return m.group(1) if m else None


# ════════════════════════════════════
# PDF-Text auslesen
# ════════════════════════════════════

# pdfplumber verschluckt in Andys fett gedruckten Zeilen oft die Leerzeichen („GesamtsummeRechnungen“,
# „Rechnung04447/26(SchlussrechnungzuAuftragsbestätigung03174/26)“) → zwischen Wörtern überall \s*
TITEL_RE = re.compile(r"(Schlussrechnung|Schlußrechnung|Stornorechnung|\d{1,2}\s*\.\s*Abschlagsrechnung|Abschlagsrechnung|"
                      r"Auftragsbest\S*?tigung|Rechnung)\s*0?(\d{4})\s*/\s*(\d{2})(?!\d)", re.IGNORECASE)
SR_BEZUG_RE = re.compile(r"Schlussrechnung\s*zu\s*(?:Auftragsbest\S*?tigung|Angebot)\s*0?(\d{4})\s*/\s*(\d{2})", re.IGNORECASE)
STORNO_BEZUG_RE = re.compile(r"\(\s*zu\s*Rechnung\s*0?(\d{4})\s*/\s*(\d{2})\s*\)", re.IGNORECASE)
DATUM_RE = re.compile(r"Datum\s*:?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")
FAELLIG_RE = re.compile(r"bis\s*zum\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.IGNORECASE)
UEBERWEISEN_RE = re.compile(r"Betrag\s*von\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", re.IGNORECASE)
P13B_RE = re.compile(r"§\s*13\s*b|13\s*b\s*UStG|Umsatzbesteuerung\s*f\S*r\s*Bauleistungen", re.IGNORECASE)
ABZUG_RE = re.compile(r"^0?(\d{4})\s*/\s*(\d{2})\s*\(\s*(\d{1,2})\s*\.\s*(?:Abschlags|Anzahlungs|Teil)rechnung\s*\)\s*"
                      r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(.*)$", re.IGNORECASE)
LABEL = {
    "netto_sr13b": re.compile(r"^Nettosumme\s*Schlussrechnung", re.IGNORECASE),
    "nettosumme": re.compile(r"^Nettosumme(?![a-zäöüß])", re.IGNORECASE),
    "zwischensumme": re.compile(r"^Zwischensumme(?![a-zäöüß])", re.IGNORECASE),
    "umsatzsteuer": re.compile(r"^(?:Umsatzsteuer|MwSt\.?|Mehrwertsteuer)(?![a-zäöüß])", re.IGNORECASE),
    "gesamt_rechnungen": re.compile(r"^Gesamtsumme\s*Rechnungen", re.IGNORECASE),
    "gesamtsumme": re.compile(r"^Gesamtsumme(?![a-zäöüß])", re.IGNORECASE),
    "restsumme": re.compile(r"^Restsumme(?![a-zäöüß])", re.IGNORECASE),
}


def _d(t, m):
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def text_merkmale(text):
    """Alles, was aus dem Text gelesen wird – ohne Bewertung (die macht betraege())."""
    zeilen = [re.sub(r"\s+", " ", z).strip() for z in (text or "").splitlines()]
    lab = {k: [] for k in LABEL}                    # Label → [(Zeilennr, [Beträge])]
    abzuege, gesehen = [], set()
    for i, z in enumerate(zeilen):
        for k, rx in LABEL.items():
            if rx.match(z):
                werte = [zahl(m.group(0)) for m in BETRAG_RE.finditer(z[rx.match(z).end():])]
                if werte:
                    lab[k].append((i, werte))
                break
        a = ABZUG_RE.match(z)
        if a:
            werte = [zahl(m.group(0)) for m in BETRAG_RE.finditer(a.group(7))]
            nummer = f"{a.group(1)}-{a.group(2)}"
            if werte and abs(werte[0]) > 0.004 and nummer not in gesehen:   # 2. Block „gezahlt 0,00“ überspringen
                gesehen.add(nummer)
                abzuege.append({"nummer": nummer, "az": int(a.group(3)), "netto": abs(werte[0]),
                                "mwst": abs(werte[1]) if len(werte) > 2 else 0.0, "brutto": abs(werte[-1]),
                                "datum": f"{a.group(6)}-{int(a.group(5)):02d}-{int(a.group(4)):02d}"})
    titel = [(m.group(1).lower(), f"{m.group(2)}-{m.group(3)}") for m in TITEL_RE.finditer(text or "")]
    sr = SR_BEZUG_RE.search(text or "")
    st = STORNO_BEZUG_RE.search(text or "")
    ub = UEBERWEISEN_RE.search(text or "")
    return {"labels": lab, "abzuege": abzuege, "titel": titel,
            "sr_bezug": f"{sr.group(1)}-{sr.group(2)}" if sr else None,
            "storno_bezug": f"{st.group(1)}-{st.group(2)}" if st else None,
            "datum": _d(None, DATUM_RE.search(text or "")), "faellig": _d(None, FAELLIG_RE.search(text or "")),
            "ueberweisen": zahl(ub.group(1)) if ub else None, "p13b": bool(P13B_RE.search(text or ""))}


def art_aus_text(mm, nummer):
    """Belegart laut PDF: Überschrift mit der eigenen Nummer bzw. „(Schlussrechnung zu …)“ / „(zu Rechnung …)“."""
    if mm["sr_bezug"]:
        return "schluss"
    eigene = [t for t, n in mm["titel"] if nr4(n) == nr4(nummer)] if nummer else [t for t, _ in mm["titel"]]
    for t in eigene:
        if t.startswith("schlu"):
            return "schluss"
        if t.startswith("storno"):
            return "storno"
        if "abschlag" in t:
            return "abschlag"
        if t.startswith("auftrag"):
            return "auftrag"
    if mm["storno_bezug"]:
        return "storno"
    return "rechnung" if eigene else None


def _ok(a, b, tol=0.02):
    return a is not None and b is not None and abs(a - b) <= tol


def betraege(mm, art):
    """→ dict netto/mwst/brutto/rest_netto/rest_brutto/status/hinweis. 'sicher' nur, wenn die Zahlen zusammenpassen."""
    lab = mm["labels"]

    def erste(k):
        return lab[k][0][1] if lab[k] else None

    erg = {"netto": None, "mwst": None, "brutto": None, "rest_netto": None, "rest_brutto": None,
           "status": "pruefen", "hinweis": None}
    gr = erste("gesamt_rechnungen")
    if art == "schluss" and gr:
        netto = gr[0]
        mwst = gr[1] if len(gr) >= 3 else 0.0
        brutto = gr[-1] if len(gr) >= 2 else netto
        rest = erste("restsumme")
        r13 = erste("netto_sr13b")
        if rest:
            rn, rb = rest[0], rest[-1]
        elif r13:
            rn = rb = r13[0]
        else:
            rn = rb = None
        abz = round(sum(a["netto"] for a in mm["abzuege"]), 2)
        erg.update(netto=netto, mwst=mwst, brutto=brutto, rest_netto=rn, rest_brutto=rb)
        fehler = []
        if not _ok(netto + mwst, brutto):
            fehler.append("Netto + MwSt ≠ Brutto")
        if mwst and not _ok(netto * 0.19, mwst, 0.05):
            fehler.append("MwSt ≠ 19 %")
        if mm["abzuege"] and not _ok(netto - abz, rn, 0.05):
            fehler.append("Gesamt − Abschläge ≠ Restsumme")
        if not mm["abzuege"] and rn is None:
            erg.update(rest_netto=netto, rest_brutto=brutto)
        if fehler:
            erg["hinweis"] = "Schlussrechnung: " + ", ".join(fehler) + " – bitte prüfen"
        else:
            erg.update(status="sicher", hinweis=f"Schlussrechnung, {len(mm['abzuege'])} Abschläge abgezogen")
        return erg

    # Einzel-/Abschlags-/Stornorechnung, Auftragsbestätigung: letzte Zwischensumme vor der Umsatzsteuer = Netto
    ust = lab["umsatzsteuer"]
    if ust:
        iu = ust[0][0]
        mwst = ust[0][1][-1]
        vorher = sorted([(i, w[-1]) for k in ("nettosumme", "zwischensumme", "gesamtsumme") for i, w in lab[k] if i < iu])
        nachher = [w[-1] for i, w in lab["gesamtsumme"] if i > iu]
        netto = vorher[-1][1] if vorher else None
        brutto = nachher[0] if nachher else None
        if netto is None and brutto is not None:
            netto = round(brutto - mwst, 2)
        if brutto is None and netto is not None:
            brutto = round(netto + mwst, 2)
        erg.update(netto=netto, mwst=mwst, brutto=brutto)
        satz_ok = netto is not None and (_ok(netto * 0.19, mwst, 0.05) or _ok(netto * 0.07, mwst, 0.05))
        if netto is not None and _ok(netto + mwst, brutto) and satz_ok:
            erg.update(status="sicher", hinweis=None)
        else:
            erg["hinweis"] = "Summen passen nicht zusammen – bitte prüfen"
    elif mm["p13b"]:
        kand = sorted([(i, w[-1]) for k in ("nettosumme", "zwischensumme", "gesamtsumme") for i, w in lab[k]])
        if kand:
            netto = kand[-1][1]
            erg.update(netto=netto, mwst=0.0, brutto=netto, status="sicher", hinweis="§13b (ohne MwSt)")
        else:
            erg["hinweis"] = "§13b, aber kein Betrag gefunden – bitte prüfen"
    else:
        erg["hinweis"] = "kein Betrag gefunden – bitte prüfen"
    if art in RECHNUNGSARTEN:
        erg.update(rest_netto=erg["netto"], rest_brutto=erg["brutto"])
    return erg


def beleg_auswerten(row, text, text_quelle):
    """Ein Beleg: Ordnername + PDF-Text → Felder für die Tabelle ausgangsbelege (ohne Verrechnung)."""
    oi = ordner_info(row["ordner"], row["quelle"])
    mm = text_merkmale(text)
    art_t = art_aus_text(mm, oi["nummer"])
    art = oi["art"] or art_t
    hinweise = []
    if art_t and oi["art"] and art_t != oi["art"] and art_t != "rechnung":   # „Rechnung 0NNNN/JJ“ steht auch im Kopf von AZ/SR
        hinweise.append(f"Ordner sagt {oi['art']}, PDF sagt {art_t}")
        art = art_t
    # Nummer: aus dem Ordner; Jahr wie im PDF gedruckt, falls dort dieselbe Nummer steht
    nummer = oi["nummer"]
    for _, n in mm["titel"]:
        if nummer and nr4(n) == nr4(nummer):
            nummer = n
            break
    b = betraege(mm, art)
    if b["hinweis"]:
        hinweise.append(b["hinweis"])
    status = b["status"]
    if text_quelle == "ocr" and status == "sicher":
        status = "pruefen"
        hinweise.append("per Texterkennung gelesen – bitte prüfen")
    if text_quelle == "keiner":
        status = "pruefen"
        hinweise = ["kein Text im PDF lesbar – Betrag bitte eintragen"]
    if hinweise and any("Ordner sagt" in h for h in hinweise):
        status = "pruefen"
    bezug = None
    if art == "schluss":
        bezug = mm["sr_bezug"]
    elif art == "storno":
        bezug = mm["storno_bezug"] or oi["bezug"]
    return {"gruppe": oi["gruppe"], "belegart": art, "nummer": nummer, "bezug_nummer": bezug,
            "datum": mm["datum"], "faellig": mm["faellig"],
            "netto": b["netto"], "mwst": b["mwst"], "brutto": b["brutto"],
            "rest_netto": b["rest_netto"], "rest_brutto": b["rest_brutto"],
            "abzuege": mm["abzuege"] or None, "steuer_13b": mm["p13b"] and not b["mwst"],
            "betrag_status": status, "betrag_hinweis": " · ".join(dict.fromkeys(hinweise)) or None,
            "text_quelle": text_quelle}


# ════════════════════════════════════
# Verrechnung über alle Belege (reine Rechenfunktion)
# ════════════════════════════════════

def eur(x):
    return "?" if x is None else f"{x:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".") + " €"


def verrechne(rows):
    """Bekommt alle Belege (ohne gelöschte), liefert {id: änderungen}. Läuft bei jedem Lauf komplett neu."""
    vorher = {r["id"]: dict(r) for r in rows}
    rows = [dict(r) for r in rows]
    for r in rows:
        r.update(status="neu", storniert_durch=None, verrechnet_in=None)

    def setze(r, **f):
        if r.get("betrag_status") in GEPRUEFT:
            for k in ("netto", "mwst", "brutto", "rest_netto", "rest_brutto", "betrag_status", "betrag_hinweis"):
                f.pop(k, None)
        r.update(f)

    def hinweis_dazu(r, text):
        if r.get("betrag_status") in GEPRUEFT:
            return
        alt = r.get("betrag_hinweis") or ""
        if text not in alt:
            r["betrag_hinweis"] = (alt + " · " if alt else "") + text

    # 1) mehrere Fassungen derselben Nummer: lesbarer Text vor Texterkennung, dann die neueste Datei
    fassungen = {}
    for r in rows:
        if r.get("nummer"):
            fassungen.setdefault((r["quelle"], nr4(r["nummer"])), []).append(r)
    haupt = {}
    for k, liste in fassungen.items():
        liste.sort(key=lambda x: (x.get("betrag_status") in GEPRUEFT, x.get("text_quelle") == "pdf",
                                  x.get("datei_geaendert_am") or "", x["id"]))
        h = liste[-1]
        haupt[k] = h
        for alt in liste[:-1]:
            setze(alt, status="ersetzt")
        andere = {round(a["netto"], 2) for a in liste[:-1] if a.get("netto") is not None}
        if h.get("netto") is not None and andere - {round(h["netto"], 2)} and h.get("betrag_status") == "sicher":
            setze(h, betrag_status="pruefen")
            hinweis_dazu(h, f"{len(liste)} Fassungen mit unterschiedlichem Betrag im Ordner – neueste genommen, bitte prüfen")

    def finde(nummer):
        return haupt.get(("rechnung", nr4(nummer))) if nummer else None

    aktiv = [r for r in rows if r["status"] == "neu" and r["quelle"] == "rechnung"]

    # 2) Stornorechnungen
    for s in sorted([r for r in aktiv if r.get("belegart") == "storno"], key=lambda x: x["id"]):
        ziel = finde(s.get("bezug_nummer")) if s.get("bezug_nummer") else None
        if ziel is not None and ziel is not s:
            vorschlag = s.get("betrag_status") != "sicher" and "gleicher Betrag" in (s.get("betrag_hinweis") or "")
            if not vorschlag or s.get("betrag_status") in GEPRUEFT:
                ziel["storniert_durch"] = s.get("nummer")
            continue
        if s.get("bezug_nummer"):
            hinweis_dazu(s, f"stornierte Rechnung {s['bezug_nummer']} ist nicht eingelesen")
            continue
        # Bezug nicht genannt: gleiche Gruppe, frühere Nummer, gleicher Betrag – nur als Vorschlag
        kand = [r for r in aktiv if r is not s and r.get("gruppe") == s.get("gruppe") and r.get("belegart") in RECHNUNGSARTEN
                and (nr4(r.get("nummer")) or "") < (nr4(s.get("nummer")) or "") and r.get("netto") is not None
                and s.get("netto") is not None and abs(r["netto"] - s["netto"]) <= 0.01]
        if len(kand) == 1:
            setze(s, bezug_nummer=kand[0].get("nummer"), betrag_status="pruefen")
            hinweis_dazu(s, f"storniert vermutlich Rechnung {kand[0].get('nummer')} (gleicher Betrag) – bitte bestätigen")
            if s.get("betrag_status") in GEPRUEFT:
                kand[0]["storniert_durch"] = s.get("nummer")
        else:
            setze(s, betrag_status="pruefen")
            hinweis_dazu(s, "nicht erkennbar, welche Rechnung storniert wird – bitte prüfen")

    # 3) Schlussrechnungen: Abschläge aus der Abzugsliste ablösen und deren Beträge bestätigen
    for sr in sorted([r for r in aktiv if r.get("belegart") == "schluss" and not r.get("storniert_durch")], key=lambda x: x["id"]):
        fehlend = 0
        for a in sr.get("abzuege") or []:
            az = finde(a["nummer"])
            if az is None or az is sr or az.get("storniert_durch"):
                fehlend += 1
                continue
            az["verrechnet_in"] = sr.get("nummer")
            if sr.get("text_quelle") != "pdf":
                continue   # Abzugsliste nur per Texterkennung gelesen → bestätigt nichts
            if not az.get("datum") and a.get("datum"):
                az["datum"] = a["datum"]
            if az.get("netto") is None or az.get("betrag_status") == "pruefen":
                setze(az, netto=a["netto"], mwst=a["mwst"], brutto=a["brutto"], rest_netto=a["netto"], rest_brutto=a["brutto"],
                      betrag_status="sicher", betrag_hinweis=f"Betrag aus der Abzugsliste der Schlussrechnung {sr.get('nummer')}")
            elif not _ok(az["netto"], a["netto"], 0.05) and az.get("betrag_status") not in GEPRUEFT:
                setze(az, betrag_status="pruefen")
                hinweis_dazu(az, f"Schlussrechnung {sr.get('nummer')} nennt {eur(a['netto'])} netto – bitte prüfen")
        if fehlend:
            hinweis_dazu(sr, f"{fehlend} abgezogene Abschläge nicht eingelesen (vor 2025)")

    upd = {}
    for r in rows:
        diff = {k: v for k, v in r.items() if vorher[r["id"]].get(k) != v}
        if diff:
            upd[r["id"]] = diff
    return upd


# ════════════════════════════════════
# Einträge auf der Seite „Rechnungen“
# ════════════════════════════════════

VERWALTET = ("nr", "datum", "projId", "betrag", "netto", "mwstSatz", "faellig", "beschr")


def eintrag_soll(b, projekt_id):
    """So soll der Eintrag in „rechnungen“ aussehen (nur die Felder, die das Skript verwaltet)."""
    art = b.get("belegart")
    if art == "schluss":
        netto, brutto = b.get("rest_netto"), b.get("rest_brutto")
        anz = len(b.get("abzuege") or [])
        beschr = f"Schlussrechnung · Gesamt {eur(b.get('netto'))} netto"
        if anz:
            beschr += ", abzgl. 1 Abschlag" if anz == 1 else f", abzgl. {anz} Abschläge"
    else:
        netto, brutto = b.get("netto"), b.get("brutto")
        if art == "abschlag":
            k = ordner_info(b.get("ordner"), "rechnung").get("az_nr")
            beschr = f"{k}. Abschlagsrechnung" if k else "Abschlagsrechnung"
            if b.get("verrechnet_in"):
                beschr += f" · in Schlussrechnung {b['verrechnet_in']} verrechnet"
        else:
            beschr = "Rechnung"
    if b.get("steuer_13b"):
        beschr += " · §13b"
    if b.get("storniert_durch"):
        beschr += f" · storniert durch {b['storniert_durch']}"
    datum = b.get("datum") or (b.get("datei_geaendert_am") or "")[:10] or jetzt()[:10]
    return {"nr": b.get("nummer") or "", "datum": datum, "projId": int(projekt_id),
            "betrag": round(float(brutto), 2) if brutto is not None else 0.0,
            "netto": round(float(netto), 2) if netto is not None else 0.0,
            "mwstSatz": "0%" if b.get("steuer_13b") else "19%", "faellig": b.get("faellig") or "", "beschr": beschr}


def nr_norm(s):
    m = re.search(r"0?(\d{4})\s*[-/]\s*(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}" if m else (s or "").strip()


def plane_rechnungen(belege, zuordnung, eintraege, geloescht, bezahlt_bis=None):
    """Reine Rechenfunktion. belege: alle aktiven Belege · zuordnung: gruppe → projekt_id (nur bestätigte) ·
    eintraege: id(str) → data der Tabelle rechnungen · geloescht: Menge gelöschter ids (deleted_ids).
    Liefert (schreiben: {id: data}, verknuepfen: {beleg_id: rechnung_id})."""
    schreiben, verknuepfen = {}, {}
    # von Andy gelöschte eigene Einträge zählen nicht als „schon erfasst“ (sonst bliebe die Rechnung unsichtbar)
    manuell = {nr_norm(d.get("nr")): i for i, d in eintraege.items()
               if i not in geloescht and d.get("quelle") != "beleg" and d.get("nr")}
    for b in belege:
        if b.get("quelle") != "rechnung" or b.get("status") != "neu" or b.get("belegart") not in RECHNUNGSARTEN:
            continue
        pid = zuordnung.get(b.get("gruppe"))
        if pid is None:
            continue
        soll = eintrag_soll(b, pid)
        rid = b.get("rechnung_id")
        if rid:
            d = eintraege.get(str(rid))
            if d is None or str(rid) in geloescht or d.get("quelle") != "beleg":
                continue                       # von Andy gelöscht bzw. sein eigener Eintrag → nicht anfassen
            auto = dict(d.get("_auto") or {})
            neu = dict(d)
            for k in VERWALTET:
                if d.get(k) == auto.get(k) and soll[k] != d.get(k):   # nur, was Andy nicht selbst geändert hat
                    neu[k] = soll[k]
                    auto[k] = soll[k]
            if b.get("storniert_durch") and not auto.get("storniert") and d.get("status") in ("offen", "mahnung", "teilbezahlt"):
                neu["status"] = "storniert"
                auto["storniert"] = True
            neu["_auto"] = auto
            if neu != d:
                schreiben[str(rid)] = neu
            continue
        if b.get("betrag_status") not in ("sicher", "bestaetigt") or b.get("storniert_durch") or soll["betrag"] <= 0:
            continue                           # erst nach Prüfung bzw. stornierte gar nicht erst anlegen
        vorhanden = manuell.get(nr_norm(b.get("nummer")))
        if vorhanden is not None:
            verknuepfen[b["id"]] = vorhanden   # Andy hat sie schon selbst erfasst → nur verknüpfen
            continue
        rid = str(ID_BASIS + int(b["id"]))
        if rid in geloescht or rid in eintraege:
            verknuepfen[b["id"]] = rid
            continue
        status = "bezahlt" if bezahlt_bis and soll["datum"] <= bezahlt_bis else "offen"
        schreiben[rid] = {"id": int(rid), **soll, "status": status, "bezam": "", "notiz": "", "pdfData": None, "pdfName": None,
                          "quelle": "beleg", "belegId": b["id"], "belegPfad": b.get("storage_pfad"), "_auto": dict(soll)}
        verknuepfen[b["id"]] = rid
    return schreiben, verknuepfen


# ════════════════════════════════════
# Datenbank-Abläufe
# ════════════════════════════════════

def sb_download(path):
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{BUCKET}/{urllib.parse.quote(path)}",
                                 headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    with urllib.request.urlopen(req, timeout=120) as res:
        return res.read()


def ocr_text(daten):
    """Texterkennung für reine Bild-PDFs (pdftoppm + tesseract, im Ablauf installiert)."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "b.pdf")
        with open(pdf, "wb") as f:
            f.write(daten)
        subprocess.run(["pdftoppm", "-r", "300", "-gray", "-png", "-l", "30", pdf, os.path.join(d, "s")],
                       check=True, timeout=600, capture_output=True)
        teile = []
        for name in sorted(n for n in os.listdir(d) if n.endswith(".png")):
            r = subprocess.run(["tesseract", os.path.join(d, name), "-", "-l", "deu", "--psm", "6"],
                               capture_output=True, text=True, timeout=600)
            teile.append(r.stdout)
        return "\n".join(teile)


def ocr_verfuegbar():
    try:
        subprocess.run(["tesseract", "--version"], capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def pdf_text_alle(daten, max_seiten=40):
    """Wie pdf_text aus Teil D, aber alle Seiten – die Summen stehen bei langen Schlussrechnungen auf Seite 11–18."""
    import io
    import pdfplumber  # im Ablauf installiert
    with pdfplumber.open(io.BytesIO(daten)) as pdf:
        return "\n".join((s.extract_text() or "") for s in pdf.pages[:max_seiten])


def text_holen(daten, ocr):
    try:
        text = pdf_text_alle(daten)
    except Exception:  # noqa: BLE001 – defektes PDF
        text = ""
    if len(re.sub(r"\s", "", text)) >= 80 and re.search(r"\d", text):
        return text, "pdf"
    if ocr:
        try:
            t = ocr_text(daten)
            if len(re.sub(r"\s", "", t)) >= 80:
                return t, "ocr"
        except Exception:  # noqa: BLE001
            pass
    return "", "keiner"


def auslesen(stat):
    offen = sb_select_all("ausgangsbelege", "id,quelle,ordner,dateiname,storage_pfad,betrag_status",
                          "&ausgelesen_am=is.null&status=neq.geloescht&order=id")
    ocr = ocr_verfuegbar()
    for r in offen:
        try:
            text, quelle = text_holen(sb_download(r["storage_pfad"]), ocr)
            felder = beleg_auswerten(r, text, quelle)
        except Exception as e:  # noqa: BLE001
            felder = {"betrag_status": "pruefen", "betrag_hinweis": f"PDF nicht lesbar ({type(e).__name__})", "text_quelle": "keiner",
                      **{k: v for k, v in ordner_info(r["ordner"], r["quelle"]).items() if k in ("gruppe",)}}
        if r.get("betrag_status") in GEPRUEFT:      # von Andy bestätigte Beträge bleiben
            for k in ("netto", "mwst", "brutto", "rest_netto", "rest_brutto", "betrag_status", "betrag_hinweis"):
                felder.pop(k, None)
        sb_request("PATCH", f"/rest/v1/ausgangsbelege?id=eq.{int(r['id'])}",
                   body={**felder, "ausgelesen_am": jetzt(), "aktualisiert_am": jetzt()})
        stat["gelesen_" + felder.get("text_quelle", "keiner")] = stat.get("gelesen_" + felder.get("text_quelle", "keiner"), 0) + 1


BELEG_FELDER = ("id,quelle,ordner,dateiname,storage_pfad,datei_geaendert_am,gruppe,belegart,nummer,bezug_nummer,datum,faellig,"
                "netto,mwst,brutto,rest_netto,rest_brutto,abzuege,steuer_13b,betrag_status,betrag_hinweis,text_quelle,status,"
                "storniert_durch,verrechnet_in,rechnung_id")


def _zahlen(r):
    for k in ("netto", "mwst", "brutto", "rest_netto", "rest_brutto"):
        if r.get(k) is not None:
            r[k] = float(r[k])
    for a in r.get("abzuege") or []:
        for k in ("netto", "mwst", "brutto"):
            a[k] = float(a[k])
    return r


def verrechnen(stat):
    rows = [_zahlen(r) for r in sb_select_all("ausgangsbelege", BELEG_FELDER, "&status=neq.geloescht&ausgelesen_am=not.is.null&order=id")]
    for rid, aenderung in verrechne(rows).items():
        sb_request("PATCH", f"/rest/v1/ausgangsbelege?id=eq.{int(rid)}", body={**aenderung, "aktualisiert_am": jetzt()})
        stat["geaendert"] = stat.get("geaendert", 0) + 1


def gruppen_vorschlaege(belege, projekte, bestaetigt):
    """Reine Rechenfunktion: je Gruppe (Beispiel-Ordner, Vorschlag). bestaetigt: gruppe → projekt_id (von Andy)."""
    # Auftragsbestätigung, auf die sich eine Schlussrechnung einer bereits zugeordneten Gruppe bezieht → gleiches Projekt
    ab_ziel = {nr4(b["bezug_nummer"]): bestaetigt[b["gruppe"]] for b in belege
               if b.get("belegart") == "schluss" and b.get("bezug_nummer") and b.get("gruppe") in bestaetigt}
    erg = {}
    for b in belege:
        if b.get("gruppe") and b["gruppe"] not in erg:
            erg[b["gruppe"]] = [b["ordner"], vorschlag(b["gruppe"], projekte)]
    for b in belege:
        if b.get("belegart") == "auftrag" and nr4(b.get("nummer")) in ab_ziel and erg[b["gruppe"]][1] is None:
            erg[b["gruppe"]][1] = ab_ziel[nr4(b["nummer"])]
    return {g: tuple(v) for g, v in erg.items()}


def gruppen_pflegen(stat):
    belege = sb_select_all("ausgangsbelege", "id,ordner,gruppe,quelle,belegart,nummer,bezug_nummer,status",
                           "&status=eq.neu&gruppe=not.is.null&order=id")
    vorhanden = {g["gruppe"]: g for g in sb_select_all("ausgangs_gruppen", "gruppe,projekt_id,vorschlag_projekt_id,quelle")}
    projekte = []
    for p in sb_select_all("projects", "id,data"):
        d = p.get("data") or {}
        if d.get("typ") in ("laufend", "abgeschlossen"):
            projekte.append((p["id"], f"{d.get('name') or ''} {d.get('kunde') or ''}"))
    bestaetigt = {g: v["projekt_id"] for g, v in vorhanden.items() if v["quelle"] == "manuell" and v["projekt_id"]}
    for gruppe, (ordner, vid) in gruppen_vorschlaege(belege, projekte, bestaetigt).items():
        v = vorhanden.get(gruppe)
        if v is None:
            sb_request("POST", "/rest/v1/ausgangs_gruppen", headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                       body={"gruppe": gruppe, "beispiel": ordner[:300], "vorschlag_projekt_id": vid, "quelle": "auto"})
            stat["gruppen_neu"] = stat.get("gruppen_neu", 0) + 1
        elif v["quelle"] == "auto" and v.get("vorschlag_projekt_id") != vid:
            sb_request("PATCH", f"/rest/v1/ausgangs_gruppen?gruppe=eq.{urllib.parse.quote(gruppe)}&quelle=eq.auto",
                       body={"vorschlag_projekt_id": vid, "aktualisiert_am": jetzt()})


def rechnungen_pflegen(stat, bezahlt_bis=None, schreiben_erlaubt=True):
    belege = [_zahlen(r) for r in sb_select_all("ausgangsbelege", BELEG_FELDER, "&status=eq.neu&order=id")]
    zuordnung = {g["gruppe"]: g["projekt_id"] for g in sb_select_all("ausgangs_gruppen", "gruppe,projekt_id,quelle")
                 if g["quelle"] == "manuell" and g["projekt_id"]}
    eintraege = {str(r["id"]): (r.get("data") or {}) for r in sb_select_all("rechnungen", "id,data")}
    geloescht = {r["id"].split(":", 1)[1] for r in sb_select_all("deleted_ids", "id", "&id=like.rechnungen:*")}
    schreiben, verknuepfen = plane_rechnungen(belege, zuordnung, eintraege, geloescht, bezahlt_bis)
    stat["rechnungen_neu"] = sum(1 for k in schreiben if k not in eintraege)
    stat["rechnungen_aktualisiert"] = sum(1 for k in schreiben if k in eintraege)
    if not schreiben_erlaubt:
        return
    if schreiben:
        sb_request("POST", "/rest/v1/rechnungen", headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                   body=[{"id": k, "data": v, "updated_at": jetzt()} for k, v in schreiben.items()])
    for bid, rid in verknuepfen.items():
        sb_request("PATCH", f"/rest/v1/ausgangsbelege?id=eq.{int(bid)}", body={"rechnung_id": str(rid), "aktualisiert_am": jetzt()})


def anstehend():
    return len(sb_select_all("ausgangsbelege", "id", "&ausgelesen_am=is.null&status=neq.geloescht"))


def main(bezahlt_bis=None, schreiben_erlaubt=True):
    stat = {}
    auslesen(stat)
    verrechnen(stat)
    gruppen_pflegen(stat)
    rechnungen_pflegen(stat, bezahlt_bis, schreiben_erlaubt)
    print("Eigene Belege | gelesen: Text {0}, Texterkennung {1}, ohne Text {2} | Verrechnung geändert: {3} | "
          "neue Gruppen: {4} | Rechnungen neu: {5}, aktualisiert: {6}{7}".format(
              stat.get("gelesen_pdf", 0), stat.get("gelesen_ocr", 0), stat.get("gelesen_keiner", 0), stat.get("geaendert", 0),
              stat.get("gruppen_neu", 0), stat.get("rechnungen_neu", 0), stat.get("rechnungen_aktualisiert", 0),
              "" if schreiben_erlaubt else " (Probe – nicht geschrieben)"))
    return 0


# ════════════════════════════════════
# Selbsttest – nur ausgedachte Namen und Beträge (öffentliches Repository!)
# ════════════════════════════════════

def selbsttest():
    # Ordnernamen
    o = ordner_info("Rechnung 4999-26_Musterkunde_Badsanierung Musterstadt_2.AZ", "rechnung")
    assert (o["nummer"], o["art"], o["gruppe"], o["az_nr"]) == ("4999-26", "abschlag", "musterkunde badsanierung musterstadt", 2), o
    o = ordner_info("Rechnung 4998-26_Musterkunde_Badsanierung Musterstadt_SR", "rechnung")
    assert (o["art"], o["gruppe"]) == ("schluss", "musterkunde badsanierung musterstadt"), o
    o = ordner_info("Rechnung 4997-26_Musterkunde_Badsanierung Musterstadt_Storno 4998", "rechnung")
    assert (o["art"], o["bezug"], o["gruppe"]) == ("storno", "4998-26", "musterkunde badsanierung musterstadt"), o
    o = ordner_info("Rechnung 4996-26_Estrich_Stornu zu Rechnung 4990-26_Estrich", "rechnung")
    assert (o["art"], o["bezug"], o["gruppe"]) == ("storno", "4990-26", "estrich"), o
    o = ordner_info("Rechnung 4995-25_Beispiel GmbH_Hofweg 3 STORNO", "rechnung")
    assert (o["art"], o["bezug"], o["gruppe"]) == ("storno", None, "beispiel gmbh hofweg 3"), o
    o = ordner_info("Rechnung 4994-26__Muster_Malerarbeiten Am Bach 2. Rechnung", "rechnung")
    assert (o["art"], o["gruppe"]) == ("rechnung", "muster malerarbeiten am bach"), o
    o = ordner_info("Rechnung 4993-26_Muster_Renovierung Reihenhaus_4.AZ zus. Leistungen", "rechnung")
    assert (o["art"], o["az_nr"], o["gruppe"]) == ("abschlag", 4, "muster renovierung reihenhaus"), o
    o = ordner_info("Rechnung 4992-25_Muster GbR_Balkone B1,B2 SR", "rechnung")
    assert (o["art"], o["gruppe"]) == ("schluss", "muster gbr balkone b1 b2"), o
    o = ordner_info("AuftragsNr. 3999-26_Musterkunde_Badsanierung Musterstadt", "auftrag")
    assert (o["nummer"], o["art"], o["gruppe"]) == ("3999-26", "auftrag", "musterkunde badsanierung musterstadt"), o
    assert ordner_info("AuftragsNr_3998-26_Muster_Bad", "auftrag")["nummer"] == "3998-26"
    assert nr4("04404/26") == nr4("4404-26") == "4404" and nr4(None) is None

    # Zuordnungs-Vorschläge: nur unterscheidende Wörter zählen (Kunde mit mehreren Projekten / Hausnummer allein reicht nicht)
    pj = [(1, "Musterbau Renovierung Lindenweg 14 München Musterbau"), (2, "Musterbau Badsanierung Ahornstr. 3 Musterbau"),
          (3, "Huber Sanierung RH Birkenallee 64 Trudering"), (4, "Wohnungssanierung Kastanienplatz 5 Maier")]
    assert vorschlag("musterbau abbrucharbeiten eschenweg 14", pj) is None
    assert vorschlag("musterbau lindenweg renovierungsarbeiten", pj) == 1
    assert vorschlag("huber renovierung reihenhaus", pj) == 3
    assert vorschlag("maier wohnungsrenovierung kastanienplatz 5", pj) == 4
    assert vorschlag("estricharbeiten", pj) is None
    pj2 = pj + [(5, "Badsanierung Rosenau Seeblick - Musterbau"), (6, "Teilbadsanierung Koch Fliederstr. 33 Musterstadt")]
    assert vorschlag("musterbau bv seeblick", pj2) == 5               # Kunde mit mehreren Projekten → zweites Wort zählt
    assert vorschlag("musterbau renovierungsarbeiten musterbaustr musterstadt", pj2) is None   # nur der Ort passt
    gv = gruppen_vorschlaege([{"gruppe": "schmidt bad", "ordner": "Rechnung 4900-26_Schmidt_Bad_SR", "belegart": "schluss",
                               "bezug_nummer": "3900-26", "nummer": "4900-26"},
                              {"gruppe": "schmitt bad neu", "ordner": "AuftragsNr. 3900-26_Schmitt_Bad neu", "belegart": "auftrag",
                               "nummer": "3900-26"}], pj, {"schmidt bad": 4})
    assert gv["schmitt bad neu"][1] == 4, gv   # Auftragsbestätigung folgt der Schlussrechnung, die sie nennt

    # Abschlagsrechnung (Aufbau wie Andys Vorlage, Beträge ausgedacht)
    t = ("Sehr geehrte Familie Muster,\nPosition Menge ME Bezeichnung\n1 1,00 pauschaler Abschlag 35% bei Auftragsbestätigung\n"
         "Nettosumme 1.000,00\nUmsatzsteuer 19 % 190,00\nGesamtsumme 1.190,00\n"
         "Bitte überweisen Sie den Betrag von 1.190,00 € bis zum 11.02.2026 auf folgendes Konto.\n"
         "Datum: 04.02.2026\n1. Abschlagsrechnung 04999/26\n")
    e = beleg_auswerten({"ordner": "Rechnung 4999-26_Musterkunde_Bad_1.AZ", "quelle": "rechnung"}, t, "pdf")
    assert (e["belegart"], e["nummer"], e["netto"], e["mwst"], e["brutto"], e["betrag_status"]) == \
        ("abschlag", "4999-26", 1000.0, 190.0, 1190.0, "sicher"), e
    assert (e["datum"], e["faellig"]) == ("2026-02-04", "2026-02-11"), e
    # Nachlass → Zwischensumme ist das Netto
    t2 = "Nettosumme 2.000,00\nNachlass Pauschal - 3 % - 60,00\nZwischensumme 1.940,00\nUmsatzsteuer 19 % 368,60\nGesamtsumme 2.308,60\n"
    e = beleg_auswerten({"ordner": "AuftragsNr. 3999-26_Muster_Bad", "quelle": "auftrag"}, "Auftragsbestätigung 03999/26\n" + t2, "pdf")
    assert (e["belegart"], e["netto"], e["betrag_status"]) == ("auftrag", 1940.0, "sicher"), e
    # Rabatt ohne „Zwischensumme“: erste „Gesamtsumme“ vor der Umsatzsteuer ist netto
    t3 = "Nettosumme 1.100,00\nRabatt - 100,00\nGesamtsumme 1.000,00\nUmsatzsteuer 19 % 190,00\n"
    e = beleg_auswerten({"ordner": "Rechnung 4990-26_Muster_Fassade", "quelle": "rechnung"}, "Rechnung 04990/26\n" + t3, "pdf")
    assert (e["netto"], e["brutto"], e["betrag_status"]) == (1000.0, 1190.0, "sicher"), e
    # §13b
    e = beleg_auswerten({"ordner": "Rechnung 4989-26_Muster Bau_Decke", "quelle": "rechnung"},
                        "Rechnung 04989/26\nNettosumme 800,00\nDiese Rechnung unterliegt § 13b UStG.", "pdf")
    assert (e["netto"], e["mwst"], e["brutto"], e["steuer_13b"], e["betrag_status"]) == (800.0, 0.0, 800.0, True, "sicher"), e
    # Schlussrechnung mit Abzugsliste (Gesamt − Abschläge = Restsumme)
    sr = ("Rechnung 04998/26 (Schlussrechnung zu Auftragsbestätigung 03999/26)\nNettosumme 3.100,00\nNachlass - 100,00\n"
          "Zwischensumme 3.000,00\nUmsatzsteuer 19 % 570,00\nGesamtsumme 3.570,00\n"
          "abzüglich Gesamtbetrag Rechnung vom Netto MwSt.\n"
          "04999/26 (1. Abschlagsrechnung) 04.02.2026 1.000,00 190,00 - 1.190,00\n"
          "04997/26 (2. Abschlagsrechnung) 18.02.2026 1.500,00 285,00 - 1.785,00\n"
          "Restsumme 500,00 95,00 595,00\nGesamtsumme Rechnungen 3.000,00 570,00 3.570,00\n"
          "abzüglich gezahlten Betrag Rechnung vom Netto MwSt.\n"
          "04999/26 (1. Abschlagsrechnung) 04.02.2026 0,00 0,00 0,00\n"
          "Insgesamt zu zahlender Betrag 3.000,00 570,00 3.570,00\n")
    e = beleg_auswerten({"ordner": "Rechnung 4998-26_Musterkunde_Bad_SR", "quelle": "rechnung"}, sr, "pdf")
    assert (e["belegart"], e["netto"], e["rest_netto"], e["rest_brutto"], e["bezug_nummer"], e["betrag_status"]) == \
        ("schluss", 3000.0, 500.0, 595.0, "3999-26", "sicher"), e
    assert [a["nummer"] for a in e["abzuege"]] == ["4999-26", "4997-26"] and e["abzuege"][1]["netto"] == 1500.0 and e["abzuege"][1]["datum"] == "2026-02-18", e
    # Schlussrechnung §13b (Rest als „Nettosumme Schlussrechnung“)
    sr13 = ("Rechnung 04988/26 (Schlussrechnung zu Angebot 09999/26)\nNettosumme 5.000,00\n"
            "04987/26 (1. Abschlagsrechnung) 15.07.2025 4.000,00 0,00 - 4.000,00\nNettosumme Schlussrechnung 1.000,00\n"
            "Gesamtsumme Rechnungen 5.000,00\nInsgesamt zu zahlender Betrag 5.000,00 0,00 5.000,00\n§ 13b UStG")
    e = beleg_auswerten({"ordner": "Rechnung 4988-26_Muster_Bau_SR", "quelle": "rechnung"}, sr13, "pdf")
    assert (e["netto"], e["mwst"], e["rest_netto"], e["betrag_status"]) == (5000.0, 0.0, 1000.0, "sicher"), e
    # Gegenprobe geht nicht auf → prüfen
    e = beleg_auswerten({"ordner": "Rechnung 4998-26_Musterkunde_Bad_SR", "quelle": "rechnung"},
                        sr.replace("Restsumme 500,00", "Restsumme 600,00"), "pdf")
    assert e["betrag_status"] == "pruefen" and "Restsumme" in e["betrag_hinweis"], e
    # Texterkennung → immer prüfen; kein Text → prüfen
    e = beleg_auswerten({"ordner": "Rechnung 4999-26_Musterkunde_Bad_1.AZ", "quelle": "rechnung"}, t, "ocr")
    assert e["betrag_status"] == "pruefen" and e["netto"] == 1000.0, e
    e = beleg_auswerten({"ordner": "Rechnung 4999-26_Musterkunde_Bad_1.AZ", "quelle": "rechnung"}, "", "keiner")
    assert (e["betrag_status"], e["netto"], e["belegart"]) == ("pruefen", None, "abschlag"), e
    # Stornorechnung mit „(zu Rechnung …)“
    e = beleg_auswerten({"ordner": "Rechnung 4986-26_Muster_Bad_Storno", "quelle": "rechnung"},
                        "Stornorechnung 04986/26 (zu Rechnung 04998/26)\n" + t3, "pdf")
    assert (e["belegart"], e["bezug_nummer"]) == ("storno", "4998-26"), e

    # Verrechnung
    def b(i, art, nummer, netto, **kw):
        r = {"id": i, "quelle": "rechnung", "gruppe": "g", "belegart": art, "nummer": nummer, "netto": netto, "mwst": None,
             "brutto": None, "rest_netto": netto, "rest_brutto": None, "abzuege": None, "betrag_status": "sicher",
             "betrag_hinweis": None, "text_quelle": "pdf", "status": "neu", "storniert_durch": None, "verrechnet_in": None,
             "datei_geaendert_am": f"2026-01-{i:02d}", "bezug_nummer": None}
        r.update(kw)
        return r

    def anwenden(rows):
        u = verrechne(rows)
        return {r["id"]: {**r, **u.get(r["id"], {})} for r in rows}

    # A) zwei Abschläge (einer nur als Bild) + Schlussrechnung → abgelöst, Bild-Betrag aus der Abzugsliste bestätigt
    e = anwenden([b(1, "abschlag", "4999-26", 1000.0),
                  b(2, "abschlag", "4997-26", None, betrag_status="pruefen", text_quelle="ocr"),
                  b(3, "schluss", "4998-26", 3000.0, rest_netto=500.0,
                    abzuege=[{"nummer": "4999-26", "netto": 1000.0, "mwst": 190.0, "brutto": 1190.0},
                             {"nummer": "4997-26", "netto": 1500.0, "mwst": 285.0, "brutto": 1785.0, "datum": "2026-02-18"}])])
    assert e[1]["verrechnet_in"] == e[2]["verrechnet_in"] == "4998-26", e
    assert (e[2]["netto"], e[2]["betrag_status"], e[2].get("datum")) == (1500.0, "sicher", "2026-02-18"), e
    # B) Schlussrechnung storniert und neu gestellt → nur die neue zählt, Abschläge hängen an der neuen
    e = anwenden([b(4, "abschlag", "4980-26", 1000.0),
                  b(5, "schluss", "4981-26", 2000.0, abzuege=[{"nummer": "4980-26", "netto": 1000.0, "mwst": 0, "brutto": 0}]),
                  b(6, "storno", "4982-26", 2000.0, bezug_nummer="4981-26"),
                  b(7, "schluss", "4983-26", 1900.0, abzuege=[{"nummer": "4980-26", "netto": 1000.0, "mwst": 0, "brutto": 0}])])
    assert e[5]["storniert_durch"] == "4982-26" and e[7]["storniert_durch"] is None and e[4]["verrechnet_in"] == "4983-26", e
    # C) Storno ohne Bezug, ein Kandidat mit gleichem Betrag → nur Vorschlag (Rechnung zählt weiter), nach Bestätigung storniert
    rows = [b(8, "rechnung", "4970-26", 500.0), b(9, "storno", "4971-26", 500.0)]
    e = anwenden(rows)
    assert e[8]["storniert_durch"] is None and e[9]["betrag_status"] == "pruefen" and e[9]["bezug_nummer"] == "4970-26", e
    e = anwenden([b(8, "rechnung", "4970-26", 500.0),
                  b(9, "storno", "4971-26", 500.0, betrag_status="bestaetigt", bezug_nummer="4970-26",
                    betrag_hinweis="storniert vermutlich Rechnung 4970-26 (gleicher Betrag) – bitte bestätigen")])
    assert e[8]["storniert_durch"] == "4971-26", e
    # D) drei Fassungen derselben Nummer, unterschiedliche Beträge → neueste zählt, „bitte prüfen“
    e = anwenden([b(10, "rechnung", "4960-26", 900.0), b(11, "rechnung", "4960-26", 950.0), b(12, "rechnung", "4960-26", 980.0)])
    assert e[10]["status"] == e[11]["status"] == "ersetzt" and e[12]["status"] == "neu" and e[12]["betrag_status"] == "pruefen", e
    # E) von Andy bestätigter Betrag bleibt, auch wenn die Schlussrechnung etwas anderes nennt
    e = anwenden([b(13, "abschlag", "4950-26", 999.0, betrag_status="bestaetigt"),
                  b(14, "schluss", "4951-26", 2000.0, abzuege=[{"nummer": "4950-26", "netto": 1000.0, "mwst": 0, "brutto": 0}])])
    assert (e[13]["netto"], e[13]["betrag_status"], e[13]["verrechnet_in"]) == (999.0, "bestaetigt", "4951-26"), e

    # Einträge auf der Seite „Rechnungen“
    belege = [dict(e[13], gruppe="g", rest_netto=999.0, rest_brutto=1188.81, brutto=1188.81, datum="2026-03-01", ordner="Rechnung 4950-26_M_1.AZ"),
              dict(e[14], gruppe="g", rest_netto=1001.0, rest_brutto=1191.19, brutto=2380.0, datum="2026-04-01", ordner="Rechnung 4951-26_M_SR"),
              b(15, "rechnung", "4940-26", 300.0, brutto=357.0, rest_brutto=357.0, datum="2026-02-01", gruppe="h"),
              b(16, "rechnung", "4941-26", 100.0, brutto=119.0, rest_brutto=119.0, betrag_status="pruefen", datum="2026-02-02")]
    schreiben, verkn = plane_rechnungen(belege, {"g": 77}, {}, set())
    assert set(schreiben) == {str(ID_BASIS + 13), str(ID_BASIS + 14)} and verkn == {13: str(ID_BASIS + 13), 14: str(ID_BASIS + 14)}, schreiben
    sr_e = schreiben[str(ID_BASIS + 14)]
    assert (sr_e["netto"], sr_e["betrag"], sr_e["projId"], sr_e["status"], sr_e["quelle"]) == (1001.0, 1191.19, 77, "offen", "beleg"), sr_e
    assert "in Schlussrechnung 4951-26 verrechnet" in schreiben[str(ID_BASIS + 13)]["beschr"], schreiben
    # Andy hat sie schon selbst erfasst (andere Schreibweise der Nummer) → nur verknüpfen
    schreiben, verkn = plane_rechnungen(belege, {"g": 77}, {"123": {"nr": "04951/26", "betrag": 5}}, set())
    assert verkn[14] == "123" and str(ID_BASIS + 14) not in schreiben, (schreiben, verkn)
    # … aber Andys eigenen Eintrag gelöscht → zählt nicht, Rechnung wird aus dem Beleg angelegt
    schreiben, verkn = plane_rechnungen(belege, {"g": 77}, {"123": {"nr": "04951/26", "betrag": 5}}, {"123"})
    assert verkn[14] == str(ID_BASIS + 14) and str(ID_BASIS + 14) in schreiben, (schreiben, verkn)
    # schon verknüpfter Eintrag später gelöscht → nicht mehr anfassen
    s, _ = plane_rechnungen([dict(belege[0], rechnung_id="123")], {"g": 77}, {"123": {"nr": "x", "quelle": "beleg"}}, {"123"})
    assert s == {}, s
    # von Andy gelöscht → nicht wieder anlegen
    schreiben, verkn = plane_rechnungen(belege, {"g": 77}, {}, {str(ID_BASIS + 13)})
    assert str(ID_BASIS + 13) not in schreiben, schreiben
    # bereits angelegt: Andy hat Betrag geändert und bezahlt markiert → bleibt; Storno setzt nur offene auf „storniert“
    rid = str(ID_BASIS + 15)
    alt = plane_rechnungen([belege[2]], {"h": 5}, {}, set(), bezahlt_bis="2026-02-15")[0][rid]
    assert alt["status"] == "bezahlt", alt
    alt = dict(alt, betrag=400.0, status="offen")
    neu_b = dict(belege[2], rechnung_id=rid, storniert_durch="4942-26", netto=310.0)
    s, _ = plane_rechnungen([neu_b], {"h": 5}, {rid: alt}, set())
    assert s[rid]["betrag"] == 400.0 and s[rid]["netto"] == 310.0 and s[rid]["status"] == "storniert", s
    s2, _ = plane_rechnungen([neu_b], {"h": 5}, {rid: dict(s[rid], status="offen")}, set())
    assert rid not in s2 or s2[rid]["status"] == "offen", s2     # Andy hat „storniert“ zurückgenommen → bleibt so

    print("Selbsttest Belege OK")
    return 0


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        sys.exit(selbsttest())
    if not SB_KEY:
        print("FEHLER: SUPABASE_SERVICE_KEY fehlt.")
        sys.exit(2)
    if "--anstehend" in sys.argv:
        print(f"anstehend={anstehend()}")
        sys.exit(0)
    bis = BEZAHLT_BIS
    if "--bezahlt-bis" in sys.argv:
        bis = sys.argv[sys.argv.index("--bezahlt-bis") + 1]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", bis), "Datum als JJJJ-MM-TT"
    sys.exit(main(bezahlt_bis=bis, schreiben_erlaubt="--probe" not in sys.argv))

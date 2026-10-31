"""
Rechnungs-Sync: holt Eingangsrechnungen (PDF-Anhänge) aus dem Postfach rechnungen@papist-bau.de
und legt sie in Supabase ab (Tabelle eingangsrechnungen + privater Speicher "eingangsrechnungen").

Postfach-Aufbau:  Posteingang/03 Rechnungen/<Nr> <Bauvorhaben>/...   z. B. ".../76 Lindenweg 13 Musterbau Feldkirchen"
Zuordnung:        rechnungsordner_mapping. Das Skript ordnet NIE selbst zu (Vorgabe Andy): Es trägt jeden Ordner
                  mit einem Vorschlag ein (quelle='auto', projekt_id=NULL). Übernommen werden nur Ordner, die Andy
                  bestätigt hat (quelle='manuell', projekt_id gesetzt). quelle='manuell' ohne Projekt = ignoriert.
Modus --nur-ordner: nur Ordner erfassen und Vorschläge eintragen, keine Rechnungen übernehmen.

Läuft täglich als GitHub-Ablauf (.github/workflows/sync_rechnungen.yml).
Zugangsdaten kommen ausschließlich aus Umgebungsvariablen (GitHub Secrets):
  IMAP_USER, IMAP_PASSWORD, SUPABASE_SERVICE_KEY  (+ optional IMAP_HOST, SUPABASE_URL, ROOT_FOLDER)

WICHTIG: Das Repository ist öffentlich, die Protokolle der GitHub-Abläufe auch.
Dieses Skript gibt deshalb NUR Zahlen aus – keine Absender, Betreffs, Ordner- oder Projektnamen.
Details (inkl. Fehlertexte) landen nur in der geschützten Tabelle rechnungs_sync_laeufe.
"""

import base64
import datetime
import difflib
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.strato.de")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
SB_URL = os.environ.get("SUPABASE_URL", "https://bthbicznechsnnowkxjz.supabase.co").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ROOT_FOLDER = os.environ.get("ROOT_FOLDER", "03 Rechnungen")
BUCKET = "eingangsrechnungen"
OHNE_ORDNER = "(ohne Unterordner)"
MAX_PDF_BYTES = 25 * 1024 * 1024


# ════════════════════════════════════
# Supabase (REST + Storage) mit Bordmitteln
# ════════════════════════════════════

def sb_request(method, path, body=None, headers=None, raw=None):
    h = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}
    if headers:
        h.update(headers)
    data = None
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(SB_URL + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            txt = res.read().decode("utf-8")
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {method} {path.split('?')[0]}: {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


def sb_select_all(table, select, extra=""):
    rows, off = [], 0
    while True:
        page = sb_request("GET", f"/rest/v1/{table}?select={select}{extra}&offset={off}&limit=1000")
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def jetzt():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pg_eq(wert):
    """Filter 'gleich' für PostgREST, sicher auch bei Kommas/Klammern/Anführungszeichen im Wert."""
    return "eq." + urllib.parse.quote('"' + wert.replace("\\", "\\\\").replace('"', '\\"') + '"', safe="")


def sb_download_pdf(path):
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{BUCKET}/{urllib.parse.quote(path)}",
                                 headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def sb_upload_pdf(path, data):
    sb_request("POST", f"/storage/v1/object/{BUCKET}/{urllib.parse.quote(path)}",
               raw=data, headers={"Content-Type": "application/pdf", "x-upsert": "true"})


# ════════════════════════════════════
# IMAP-Hilfen
# ════════════════════════════════════

def imap_utf7_decode(s):
    """Ordnernamen kommen im 'modified UTF-7' (RFC 3501), z. B. Umlaute als &APw-."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')


def list_folders(imap):
    """Liefert [(roher_name_für_SELECT, pfadteile_dekodiert, flags)]"""
    typ, data = imap.list()
    if typ != "OK":
        raise RuntimeError("IMAP LIST fehlgeschlagen")
    result = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):  # Name als Literal
            line = item[0].decode("utf-8", "replace")
            raw_name = item[1].decode("utf-8", "replace")
            m = LIST_RE.match(line.rsplit(" {", 1)[0] + " x")
        else:
            line = item.decode("utf-8", "replace")
            m = LIST_RE.match(line)
            raw_name = m.group("name") if m else None
        if not m or raw_name is None:
            continue
        delim = m.group("delim").strip('"') if m.group("delim") != "NIL" else "/"
        if raw_name.startswith('"') and raw_name.endswith('"'):
            raw_name = raw_name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        parts = imap_utf7_decode(raw_name).split(delim) if delim else [imap_utf7_decode(raw_name)]
        result.append((raw_name, parts, m.group("flags")))
    return result


def rechnungs_ordner(folders):
    """Alle Ordner unter ROOT_FOLDER (auch unter INBOX). Zuordnung über die erste Ebene darunter."""
    out = []
    for raw, parts, flags in folders:
        if "\\Noselect" in flags or "\\NonExistent" in flags:
            continue
        if parts and parts[0].upper() == "INBOX" and len(parts) > 1 and parts[1] == ROOT_FOLDER:
            rel = parts[2:]
        elif parts and parts[0] == ROOT_FOLDER:
            rel = parts[1:]
        else:
            continue
        out.append((raw, rel[0] if rel else OHNE_ORDNER))
    return out


def quote_mailbox(raw):
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ════════════════════════════════════
# Mail auswerten
# ════════════════════════════════════

def pdf_anhaenge(msg):
    """Alle PDF-Anhänge (auch in weitergeleiteten Mails). Liefert [(dateiname, bytes)]"""
    found, namen = [], set()
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = part.get_content_type()
        if ctype != "application/pdf" and not (fname and fname.lower().endswith(".pdf")):
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        fname = (fname or f"anhang-{len(found) + 1}.pdf").strip().replace("/", "_").replace("\\", "_")
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
        basis, n = fname, 2
        while fname in namen:
            fname = basis[:-4] + f" ({n}).pdf"
            n += 1
        namen.add(fname)
        found.append((fname, data))
    return found


def mail_message_id(msg, header_bytes):
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return mid
    # Fallback ohne Message-ID: Fingerabdruck aus Kopfzeilen
    return "ohne-id-" + hashlib.sha256(header_bytes).hexdigest()[:32]


def storage_pfad(message_id, idx, dateiname):
    sicher = unicodedata.normalize("NFKD", dateiname).encode("ascii", "ignore").decode("ascii")
    sicher = re.sub(r"[^A-Za-z0-9._-]+", "_", sicher).strip("_") or "rechnung.pdf"
    return f"{hashlib.sha256(message_id.encode('utf-8')).hexdigest()[:20]}/{idx}-{sicher[-80:]}"


# ════════════════════════════════════
# Ordner → Projekt (automatischer Namensabgleich)
# ════════════════════════════════════

STOPP = {"bv", "bauvorhaben", "und", "der", "die", "das", "in", "im", "am", "an", "bei", "fa", "firma", "gmbh"}


def norm_tokens(s):
    s = s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t not in STOPP and len(t) > 1]


def ordner_kern(ordner):
    return re.sub(r"^\s*\d+[\s._-]*", "", ordner)  # führende Nummer weg


def _passt(a, b):
    return a == b or (len(a) > 3 and len(b) > 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)


def vorschlag_projekt(ordner, projekte):
    """Nur ein VORSCHLAG zur Bestätigung durch Andy (wird nie automatisch übernommen).
    Bewertet, wie viele Wörter von Ordner und Projektname zueinander passen. Kein klarer Favorit → None."""
    ft = norm_tokens(ordner_kern(ordner))
    if not ft:
        return None
    scores = []
    for pid, pname in projekte:
        pt = norm_tokens(pname)
        if not pt:
            continue
        f_treffer = sum(1 for t in ft if any(_passt(t, p) for p in pt))
        p_treffer = sum(1 for p in pt if any(_passt(p, t) for t in ft))
        scores.append(((f_treffer / len(ft) + p_treffer / len(pt)) / 2, pid))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 0.35:
        return None
    if len(scores) > 1 and scores[1][0] > scores[0][0] - 0.1:
        return None  # kein klarer Favorit
    return scores[0][1]


# ════════════════════════════════════
# Beträge aus dem PDF-Text (Teil D)
# ════════════════════════════════════
# Grundsatz (Vorgabe Andy: im Zweifel fragen): 'sicher' nur, wenn die Zahlen rechnerisch zusammenpassen
# (Netto + MwSt = Brutto bei 19 % / 7 %) oder bei §13b (keine MwSt) ein eindeutiger Endbetrag da ist.
# Alles andere → 'pruefen' (mit Vorschlag, falls vorhanden); zählt erst nach Bestätigung in der App.

BETRAG_RE = re.compile(r"(?<![\d.,])-?\d{1,3}(?:\.\d{3})*,\d{2}(?!\d)(?!\s*%)|(?<![\d.,])-?\d+,\d{2}(?!\d)(?!\s*%)")
LABELS = {
    "netto": r"netto(?:betrag|summe|wert)?|summe\s+netto|gesamt\s*netto|warenwert|zwischensumme|"
             r"(?:summe|betrag|gesamt)\s+ohne\s+(?:mwst|ust|umsatzsteuer|mehrwertsteuer)",
    "mwst": r"mwst|mw\.?\s*-?st|mehrwertsteuer|umsatzsteuer|\bust\b|\bsteuer\b",
    "brutto": r"brutto(?:betrag|summe)?|gesamtbetrag|rechnungsbetrag|rechnungssumme|endbetrag|zu\s+zahlen|zahlbetrag|"
              r"gesamtsumme|summe\s+brutto|\btotal\b|zahlbar|summe\s+inkl|betrag\s+inkl|rechnungswert|forderungsbetrag|"
              r"\bsumme\b(?!\s+(?:netto|ohne|der\s+positionen))",
}
ZU_ZAHLEN = (r"zu\s+zahlen|zahlbetrag|zahlbar|auszahlungsbetrag|restbetrag|restforderung|forderungsbetrag|\bforderung\b|"
             r"offener\s+betrag|noch\s+offen")
ABSCHLAG = (r"abschlag|abzügl|abzgl|bereits\s+(?:gezahlt|bezahlt|berechnet)|bisher\s+(?:gezahlt|berechnet|abgerechnet)|"
            r"teilzahlung|anzahlung")
NR_RE = re.compile(r"(?:rechnungs?\s*-?\s*(?:nr|nummer)|rg\.?\s*-?\s*nr|beleg\s*-?\s*nr)\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/._]{2,30})",
                   re.IGNORECASE)


def zahl(s):
    return round(float(s.replace(".", "").replace(",", ".")), 2)


def alle_betraege(text):
    return {zahl(b.group(0)) for b in BETRAG_RE.finditer(text)}


def kandidaten(text):
    """Findet je Art (netto/mwst/brutto) die Beträge, die hinter einer passenden Beschriftung stehen."""
    gefunden = {k: [] for k in LABELS}
    gefunden["zu_zahlen"] = []
    zeilen = [z.strip() for z in text.splitlines()]
    for i, zeile in enumerate(zeilen):
        klein = zeile.lower()
        # Tabellen-Summen: Beschriftungen nebeneinander ohne Zahlen, Zahlen in der nächsten Zeile
        if not BETRAG_RE.search(zeile) and i + 1 < len(zeilen):
            treffer = sorted((m.start(), art) for art, muster in LABELS.items() for m in re.finditer(muster, klein))
            arten = []
            for _, art in treffer:
                if not arten or arten[-1] != art:
                    arten.append(art)
            werte = [zahl(b.group(0)) for b in BETRAG_RE.finditer(zeilen[i + 1])]
            if len(arten) >= 2 and len(werte) == len(arten):
                for art, wert in zip(arten, werte):
                    gefunden[art].append(wert)
        for m in re.finditer(ZU_ZAHLEN, klein):
            b = BETRAG_RE.search(zeile[m.end():])
            if b:
                gefunden["zu_zahlen"].append(zahl(b.group(0)))
        for art, muster in LABELS.items():
            for m in re.finditer(muster, klein):
                rest = zeile[m.end():]
                b = BETRAG_RE.search(rest)
                if not b and i + 1 < len(zeilen) and not BETRAG_RE.search(zeile[:m.start()]):
                    b = BETRAG_RE.search(zeilen[i + 1])  # Betrag steht in der nächsten Zeile
                if b:
                    gefunden[art].append(zahl(b.group(0)))
    return gefunden


# Andys eigene Kundenrechnungen (Nummernkreis z. B. „Rechnung 4436-26_…“) sind keine Kosten (Vorgabe Andy)
EIGENE_RE = re.compile(r"^(?:rechnung|gutschrift|schlussrechnung|stornorechnung)\s*\d{4}-\d{2}(?:[_\s.]|$)", re.IGNORECASE)

# ── Belegart, Belegnummer, Lieferant (für die Verrechnung von Abschlägen/Schlussrechnungen) ──
# Belegart nach der Überschrift: das Stichwort, das im Kopf des PDFs ZUERST vorkommt, gewinnt
# (eine Auftragsbestätigung kann weiter unten „Anzahlung 50 %“ erwähnen, eine Schlussrechnung „abzgl. Abschlag“).
ART_MUSTER = {
    "schluss": r"schlussrechnung|schlußrechnung|schluss-rechnung|schlussabrechnung|endabrechnung",
    "korrektur": r"rechnungskorrektur|stornorechnung|\bstorno\b",
    "gutschrift": r"gutschrift",
    "abschlag": r"abschlagsrechnung|anzahlungsrechnung|\banzahlung\b|teilrechnung|vorausrechnung|\b\d\.\s*az\b|\babschlag\b",
    "kein_beleg": r"auftragsbest(?:ä|ae)tigung|\bfreigabe\b|\bangebot\b|lieferschein|kostenvoranschlag",
}
AB_DATEINAME = r"(?:^|[\s_\-])ab(?:[\s_\-.]|$)"   # „7301359 AB Papist.pdf“
USTID_RE = re.compile(r"\bDE\s?\d{3}\s?\d{3}\s?\d{3}\b")
STNR_RE = re.compile(r"\b\d{2,3}/\d{3}/\d{4,5}\b")
NUMMER_RE = re.compile(r"[A-Za-z]{0,4}\s?\d[\d./\-]{3,22}\d")
DATUM_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")
BELEGNR_RE = re.compile(r"(?:rechnung|schlussrechnung|anzahlungsrechnung|abschlagsrechnung|gutschrift|korrektur|beleg|storno)\w*"
                        r"\s*[-.]?\s*(?:nr\.?|nummer|no\.?)?\s*[:#]?\s*((?:[A-Z]{1,4}\s?)?\d[\d/.\-]{3,20}\d)", re.IGNORECASE)


def ziffern(s):
    return re.sub(r"\D", "", s or "")


def beleg_art(text, betreff="", dateiname=""):
    kopf = "\n".join([z for z in (text or "").splitlines() if z.strip()][:15]).lower()
    for quelle in (kopf, (dateiname or "").lower(), (betreff or "").lower()):
        treffer = [(m.start(), art) for art, muster in ART_MUSTER.items() for m in [re.search(muster, quelle)] if m]
        if quelle is not kopf and re.search(AB_DATEINAME, quelle):
            treffer.append((-1, "kein_beleg"))
        if treffer:
            return min(treffer)[1]
    return "rechnung"


def beleg_nummer(text, betreff="", dateiname=""):
    """Eigene Belegnummer, nur Ziffern (mind. 5). Zuerst aus der Überschrift, sonst aus Betreff/Dateiname."""
    kopf = "\n".join([z for z in (text or "").splitlines() if z.strip()][:40])
    for m in BELEGNR_RE.finditer(kopf):
        roh = m.group(1).strip()
        if not DATUM_RE.match(roh.split()[-1]) and len(ziffern(roh)) >= 5:
            return ziffern(roh)
    for quelle in (betreff or "", dateiname or ""):
        for m in NUMMER_RE.finditer(quelle):
            roh = m.group(0).strip()
            if not DATUM_RE.match(re.sub(r"^[A-Za-z]+\s?", "", roh)) and len(ziffern(roh)) >= 6:
                return ziffern(roh)
    return None


def genannte_nummern(text):
    """Alle im PDF genannten Nummern (nur Ziffern, mind. 5), z. B. die Abzugsliste einer Schlussrechnung."""
    return sorted({ziffern(m.group(0)) for m in NUMMER_RE.finditer(text or "") if len(ziffern(m.group(0))) >= 5})


def steuer_ids(text):
    ids = [re.sub(r"\s", "", m.group(0)) for m in USTID_RE.finditer(text or "")]
    ids += [m.group(0) for m in STNR_RE.finditer(text or "")]
    return list(dict.fromkeys(ids))


def betrag_aus_text(text, betreff="", dateiname=""):
    """Liefert dict mit netto (Betrag dieses Belegs), gesamt (Gesamtleistung), zahl (Zahlbetrag netto),
    mwst, brutto, nr, status ('sicher'/'pruefen') und hinweis. Die Verrechnung von Abschlägen mit
    Schlussrechnungen passiert danach je Projekt in verrechne_projekt()."""
    erg = _betrag_aus_pdf(text)
    # Betrag im Dateinamen (Andy schreibt ihn oft hinein, z. B. „OBI 28.07.26 78,94€ Husel.pdf“) als Abgleich
    namens_betraege = [zahl(b.group(0)) for b in BETRAG_RE.finditer(dateiname or "")]
    if namens_betraege:
        nb = namens_betraege[-1]
        if erg["brutto"] is not None or erg["netto"] is not None:
            if nb in (erg["brutto"], erg["netto"]):
                if erg["status"] == "sicher":
                    erg["hinweis"] = (erg["hinweis"] + " · " if erg["hinweis"] else "") + "Dateiname bestätigt"
            elif erg["status"] == "sicher":
                erg["status"] = "pruefen"
                erg["hinweis"] = f"Dateiname nennt {nb:.2f} €, PDF {erg['brutto'] or erg['netto']:.2f} € – bitte prüfen".replace(".", ",")
        else:
            erg.update(brutto=nb, netto=round(nb / 1.19, 2),
                       hinweis="Betrag aus Dateiname (Brutto), Netto mit 19 % geschätzt")
    return erg


def _betrag_aus_pdf(text):
    erg = {"netto": None, "mwst": None, "brutto": None, "nr": None, "status": "pruefen", "hinweis": None,
           "gesamt": None, "zahl": None}
    if not text or len(text.strip()) < 20:
        erg["hinweis"] = "kein lesbarer Text (evtl. Scan)"
        return erg
    nr = NR_RE.search(text)
    if nr:
        erg["nr"] = nr.group(1).strip(".-/")
    klein = text.lower()
    k = kandidaten(text)
    netto_k, mwst_k, brutto_k = set(k["netto"]), set(k["mwst"]), set(k["brutto"])
    zu_zahlen = {z for z in k["zu_zahlen"] if z >= 0}
    abschlag = re.search(ABSCHLAG, klein) is not None
    p13b = "13b" in klein or "steuerschuldnerschaft" in klein or "reverse charge" in klein
    gleich = {x for x in netto_k & brutto_k if x > 0}   # Netto = Brutto beschriftet → ohne MwSt

    def stimmt(n, b):
        return any(abs(n * (1 + s) - b) <= max(0.02, b * 0.0002) for s in (0.19, 0.07))

    # 1) Beschriftete Summen, die rechnerisch zusammenpassen
    passend = []
    for n in netto_k:
        for b in brutto_k:
            if n > 0 and b > 0 and (stimmt(n, b) or any(m > 0 and abs(n + m - b) <= 0.02 for m in mwst_k)):
                passend.append((b, n))
    # 2) Brutto + ausgewiesene MwSt (z. B. „darin enthalten 19 % MwSt“)
    for b in brutto_k:
        for m in mwst_k:
            if 0 < m < b and stimmt(round(b - m, 2), b):
                passend.append((b, round(b - m, 2)))
    # 3) Dreier-Probe: Netto, MwSt und Brutto stehen als Zahlen irgendwo und passen genau zusammen
    #    (nicht bei Belegen ohne MwSt – dort würden Positionszeilen fälschlich als Summe erkannt)
    quelle = "beschriftet"
    if not passend and not gleich and not p13b:
        alle = sorted(x for x in alle_betraege(text) if x > 0)
        alle_set = set(alle)
        for n in alle:
            for s in (0.19, 0.07):
                b = round(n * (1 + s), 2)
                for kand in (b, round(b - 0.01, 2), round(b + 0.01, 2)):
                    if kand in alle_set and round(kand - n, 2) in alle_set:
                        passend.append((kand, n))
        quelle = "Dreier-Probe"

    if passend:
        gb, gn = max(passend)
        erg["gesamt"] = gn
        # Zahlbetrag netto (bei Abschlags-/Schlussrechnungen: was mit DIESEM Beleg zu zahlen ist)
        zp = [(b, n) for b, n in passend if b in zu_zahlen]
        if zp:
            erg["zahl"] = min(zp)[1]
        elif len(zu_zahlen) == 1:
            erg["zahl"] = round(next(iter(zu_zahlen)) * gn / gb, 2)   # Zahlbetrag steht brutto da
        if abschlag and erg["zahl"] is not None and erg["zahl"] > 0:
            n = erg["zahl"]
            b = round(n * gb / gn, 2)
            erg.update(netto=n, brutto=b, mwst=round(b - n, 2), status="sicher", hinweis="Betrag dieser Rechnung („zu zahlen“)")
        elif abschlag:
            b, n = min(passend)
            erg.update(netto=n, brutto=b, mwst=round(b - n, 2),
                       hinweis="Abschlags-/Teilrechnung – welcher Betrag gilt? bitte prüfen")
        else:
            erg.update(netto=gn, brutto=gb, mwst=round(gb - gn, 2), status="sicher",
                       hinweis=None if quelle == "beschriftet" else "Summen per Dreier-Probe erkannt")
    else:
        endbetraege = sorted(x for x in netto_k | brutto_k if x > 0)
        if gleich or (p13b and endbetraege):
            # ohne MwSt (§13b oder Summenzeile mit 0 % MwSt): Netto = Brutto.
            # Gesamtleistung = größter beschrifteter Betrag (bei Schlussrechnungen steht der Restbetrag kleiner darunter)
            gesamt = max(endbetraege)
            erg["gesamt"] = gesamt
            if len(zu_zahlen) == 1:
                erg["zahl"] = next(iter(zu_zahlen))
            art = "§13b (ohne MwSt)" if p13b else "ohne MwSt (Netto = Brutto)"
            if abschlag and erg["zahl"] is not None and erg["zahl"] > 0:
                erg.update(netto=erg["zahl"], status="sicher", hinweis=f"{art}, Betrag dieser Rechnung")
            elif abschlag:
                erg.update(netto=min(endbetraege), hinweis=f"{art}, mit Abschlägen – welcher Betrag gilt? bitte prüfen")
            elif gleich or len(endbetraege) == 1:
                erg.update(netto=gesamt, status="sicher", hinweis=art)
            else:
                erg.update(netto=max(endbetraege), hinweis=f"{art}, mehrere mögliche Beträge")
            erg.update(brutto=erg["netto"], mwst=0.0)
        elif brutto_k:
            b = max(brutto_k)
            erg.update(brutto=b, netto=round(b / 1.19, 2), hinweis="nur Bruttobetrag gefunden, Netto mit 19 % geschätzt")
        elif netto_k:
            erg.update(netto=max(netto_k), hinweis="nur Nettobetrag gefunden")
        else:
            erg["hinweis"] = "kein Betrag gefunden – evtl. keine Rechnung (Lieferschein, Datenblatt …)"

    if "gutschrift" in klein:
        erg["status"] = "pruefen"
        erg["hinweis"] = "Gutschrift – bitte prüfen" + (f" ({erg['hinweis']})" if erg["hinweis"] else "")
    return erg


def pdf_text(data):
    import io
    import pdfplumber  # wird im GitHub-Ablauf installiert (siehe Workflow)
    teile = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for seite in pdf.pages[:10]:
            teile.append(seite.extract_text() or "")
    return "\n".join(teile)


DIAG_RE = re.compile(r"netto|brutto|mwst|ust\b|steuer|summe|gesamt|betrag|zahlen|total|abschlag|abzgl|abzügl|13b",
                     re.IGNORECASE)


def diagnose(anzahl=30):
    """Schreibt für einige „bitte prüfen“-Rechnungen die Betrags-Zeilen in die GESCHÜTZTE Tabelle betrag_diagnose
    (kein Zugriff von außen). Dient nur zum Abstimmen der Erkennung; Tabelle wird danach gelöscht. Nichts ins Protokoll."""
    zeilen_gesamt = 0
    rows = sb_select_all("eingangsrechnungen", "id,storage_pfad,betrag_hinweis",
                         "&betrag_status=eq.pruefen&storage_pfad=not.is.null&order=id")
    # gleichmäßig über die Liste verteilt auswählen
    schritt = max(1, len(rows) // anzahl)
    for r in rows[::schritt][:anzahl]:
        try:
            text = pdf_text(sb_download_pdf(r["storage_pfad"]))
        except Exception as e:  # noqa: BLE001
            text = f"(PDF nicht lesbar: {type(e).__name__})"
        relevant = [z.strip() for z in text.splitlines() if BETRAG_RE.search(z) or DIAG_RE.search(z)]
        auszug = "\n".join(z[:160] for z in relevant[-45:])[:4000]
        sb_request("POST", "/rest/v1/betrag_diagnose",
                   body={"rechnung_id": r["id"], "hinweis": r["betrag_hinweis"], "zeilen": auszug})
        zeilen_gesamt += 1
    print(f"Diagnose: {zeilen_gesamt} Beispiele in geschützte Tabelle geschrieben")


def eigene_markieren(stat):
    """Andys eigene Kundenrechnungen in den Projektordnern → status 'eigene' (zählen nicht als Kosten)."""
    muster = urllib.parse.quote(r"^(rechnung|gutschrift|schlussrechnung|stornorechnung)[[:space:]]*[0-9]{4}-[0-9]{2}([_[:space:].]|$)",
                                safe="")
    res = sb_request("PATCH", f"/rest/v1/eingangsrechnungen?status=eq.neu&dateiname=imatch.{muster}",
                     body={"status": "eigene", "aktualisiert_am": jetzt()}, headers={"Prefer": "return=representation"})
    stat["eigene"] = stat.get("eigene", 0) + len(res or [])


# ════════════════════════════════════
# Verrechnung je Projekt (Abschläge ↔ Schlussrechnung, Duplikate, Korrekturen)
# ════════════════════════════════════
# Abgestimmt mit Andy am 31.10.2026 und an echten Fällen unabhängig nachgerechnet:
#  1. Lieferant über USt-IdNr./Steuernummer im PDF (Andys eigene Nummer wird ignoriert), sonst Absender-Domain
#  2. Auftragsbestätigung/Freigabe/Angebot/Lieferschein → keine Kosten (status 'kein_beleg')
#  3. Schlussrechnung → es zählt die Gesamtleistung netto; jeder Beleg, den sie nennt (Abzugsliste), wird 'abgeloest'
#  4. Gegenprobe: Gesamtleistung = verrechnete Belege + Zahlbetrag (±5 Cent), sonst „bitte prüfen“
#  5. Abschläge ohne Schlussrechnung zählen einzeln („vorläufig“)
#  6. Duplikate NIE über den Betrag – nur gleiche Datei oder gleicher Lieferant + gleiche Belegnummer
#  7. Korrektur/Storno zählt nur, wenn der Ursprungsbeleg vorliegt; sonst ohne Wirkung (oder fragen)
GEPRUEFT = ("bestaetigt", "manuell")   # von Andy gesetzt → nie automatisch ändern


def nummer_genannt(nr, refs):
    return bool(nr) and any(nr == r or (len(nr) >= 6 and len(r) >= 6 and (nr in r or r in nr)) for r in refs)


def eigene_steuer_ids(rows):
    """Andys eigene USt-IdNr./Steuernummer steht als „Ihre USt-IdNr.“ auf sehr vielen Belegen verschiedener Lieferanten."""
    zaehler = {}
    for r in rows:
        for i in set((r.get("ustids") or "").split()):
            zaehler[i] = zaehler.get(i, 0) + 1
    mit_ids = sum(1 for r in rows if r.get("ustids"))
    return {i for i, n in zaehler.items() if mit_ids >= 10 and n >= 0.2 * mit_ids}


def lieferant_von(r, eigene_ids):
    for i in (r.get("ustids") or "").split():
        if i not in eigene_ids:
            return i
    m = re.search(r"@([\w.\-]+)", r.get("absender") or "")
    domain = m.group(1).lower() if m else ""
    if domain and "papist" not in domain and domain not in ("gmx.de", "gmx.net", "web.de", "gmail.com", "t-online.de"):
        return domain
    return None


def verrechne_projekt(rows, eigene_ids):
    """Reine Rechenfunktion (ohne Datenbank): bekommt alle Belege EINES Projekts, liefert {id: änderungen}."""
    vorher = {r["id"]: dict(r) for r in rows}
    rows = [dict(r) for r in rows]

    def setze(r, **felder):
        if r["betrag_status"] in GEPRUEFT:   # von Andy bestätigte Beträge bleiben, wie sie sind
            for k in ("betrag_netto", "betrag_status", "betrag_hinweis"):
                felder.pop(k, None)
        r.update(felder)

    for r in rows:
        setze(r, lieferant=lieferant_von(r, eigene_ids), status="neu")
    # 2) keine Rechnungen
    for r in rows:
        if r.get("rechnungsart") == "kein_beleg":
            setze(r, status="kein_beleg", betrag_hinweis="Auftragsbestätigung/Freigabe/Angebot – keine Kosten")
    # 6) Duplikate: gleiche Datei oder gleicher Lieferant + gleiche Belegnummer (nie über den Betrag)
    gesehen = {}
    for r in sorted(rows, key=lambda x: x["id"]):
        if r["status"] != "neu":
            continue
        schluessel = []
        if r.get("datei_hash"):
            schluessel.append(("datei", r["datei_hash"]))
        if r.get("lieferant") and r.get("belegnr"):
            schluessel.append(("nr", r["lieferant"], r["belegnr"]))
        frueher = next((gesehen[s] for s in schluessel if s in gesehen), None)
        if frueher is not None:
            setze(r, status="doppelt", betrag_hinweis=f"doppelt (gleicher Beleg wie Nr. {frueher})")
        for s in schluessel:
            gesehen.setdefault(s, r["id"])

    aktiv = [r for r in rows if r["status"] == "neu"]
    nummern = {r["belegnr"] for r in aktiv if r.get("belegnr")}

    def eigen(r):  # Betrag dieses Belegs; Korrekturen/Stornos mindern
        n = r.get("betrag_netto")
        if n is None:
            return None
        return -abs(n) if r.get("rechnungsart") == "korrektur" else n

    def ursprung_da(k):  # Korrektur: liegt der korrigierte Beleg vor?
        refs = set((k.get("referenzen") or "").split()) - {k.get("belegnr")}
        return any(nummer_genannt(n, refs) for n in nummern if n != k.get("belegnr"))

    verrechnet = set()
    # 3)+4) Schlussrechnungen
    for sr in sorted([r for r in aktiv if r.get("rechnungsart") == "schluss"], key=lambda x: x["id"]):
        refs = set((sr.get("referenzen") or "").split()) - {sr.get("belegnr")}
        vor = [o for o in aktiv if o is not sr and o.get("rechnungsart") in ("abschlag", "rechnung", "korrektur", "gutschrift")
               and nummer_genannt(o.get("belegnr"), refs)]
        if not vor and sr.get("lieferant"):   # Ersatz: Abschläge desselben Lieferanten (nur wenn die Gegenprobe aufgeht)
            vor = [o for o in aktiv if o is not sr and o.get("rechnungsart") == "abschlag" and o.get("lieferant") == sr["lieferant"]]
        gl, z = sr.get("gesamt_netto"), sr.get("zahl_netto")
        zaehlt = [o for o in vor if not (o.get("rechnungsart") == "korrektur" and not ursprung_da(o))]
        werte = [eigen(o) for o in zaehlt]
        s = round(sum(w for w in werte if w is not None), 2)
        stimmig = (gl is not None and None not in werte
                   and ((z is not None and abs(gl - s - z) <= 0.05) or (z is None and vor and s <= gl + 0.02)))
        if stimmig:
            setze(sr, betrag_netto=gl, betrag_status="sicher",
                  betrag_hinweis=f"Schlussrechnung: Gesamtleistung, {len(vor)} Vorbeleg(e) verrechnet")
            for o in vor:
                setze(o, status="abgeloest", betrag_hinweis=f"in Schlussrechnung {sr.get('belegnr') or sr['id']} verrechnet")
                verrechnet.add(o["id"])
        else:
            def eur(x):
                return "?" if x is None else f"{x:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
            setze(sr, betrag_netto=gl if gl is not None else sr.get("betrag_netto"), betrag_status="pruefen",
                  betrag_hinweis=f"Gegenprobe passt nicht: Gesamtleistung {eur(gl)} €, Vorbelege {eur(s)} €, "
                                 f"Rest laut Rechnung {eur(z)} € – bitte prüfen")
            for o in vor:
                setze(o, betrag_status="pruefen", betrag_hinweis="gehört zu einer Schlussrechnung, deren Gegenprobe nicht aufgeht")
                verrechnet.add(o["id"])

    # 5) Abschläge ohne Schlussrechnung, 7) Korrekturen außerhalb einer Schlussrechnung
    sr_lieferanten = {r.get("lieferant") for r in aktiv if r.get("rechnungsart") == "schluss" and r.get("lieferant")}
    for r in aktiv:
        if r["id"] in verrechnet or r["status"] != "neu":
            continue
        art = r.get("rechnungsart")
        if art == "abschlag":
            if r.get("lieferant") in sr_lieferanten:
                setze(r, betrag_status="pruefen",
                      betrag_hinweis="Schlussrechnung dieses Lieferanten vorhanden, Abschlag dort nicht gefunden – bitte prüfen")
            elif r.get("betrag_netto") is not None and r["betrag_status"] == "sicher":
                setze(r, betrag_hinweis="vorläufig – noch keine Schlussrechnung")
        elif art == "korrektur":
            if ursprung_da(r) and r.get("betrag_netto") is not None:
                setze(r, betrag_netto=-abs(r["betrag_netto"]), betrag_status="sicher", betrag_hinweis="Korrektur/Storno zu vorhandenem Beleg")
            else:
                setze(r, betrag_status="pruefen", betrag_hinweis="Korrektur/Storno ohne vorhandenen Ursprungsbeleg – bitte prüfen")

    # nur tatsächliche Änderungen zurückgeben
    upd = {}
    for r in rows:
        diff = {k: v for k, v in r.items() if vorher[r["id"]].get(k) != v}
        if diff:
            upd[r["id"]] = diff
    return upd


def verrechnen(stat):
    felder = ("id,projekt_id,status,betrag_status,betrag_netto,betrag_hinweis,rechnungsart,lieferant,ustids,belegnr,"
              "referenzen,gesamt_netto,zahl_netto,datei_hash,absender")
    rows = sb_select_all("eingangsrechnungen", felder, "&status=in.(neu,abgeloest,doppelt,kein_beleg)&order=id")
    eigene_ids = eigene_steuer_ids(rows)
    projekte = {}
    for r in rows:
        projekte.setdefault(r["projekt_id"], []).append(r)
    for pid, liste in projekte.items():
        if pid is None:
            continue
        for rid, aenderung in verrechne_projekt(liste, eigene_ids).items():
            sb_request("PATCH", f"/rest/v1/eingangsrechnungen?id=eq.{int(rid)}", body={**aenderung, "aktualisiert_am": jetzt()})
            for k in ("status",):
                if k in aenderung and aenderung[k] != "neu":
                    stat[aenderung[k]] = stat.get(aenderung[k], 0) + 1


def betraege_auslesen(stat, maximal=1500):
    """Liest für alle Belege ohne Betrag-Status Betrag, Belegart, Nummern und Lieferanten-IDs aus dem PDF."""
    offen = sb_select_all("eingangsrechnungen", "id,storage_pfad,betreff,dateiname",
                          "&betrag_status=is.null&status=eq.neu&storage_pfad=not.is.null&order=id")
    for r in offen[:maximal]:
        betreff, dateiname = r["betreff"] or "", r["dateiname"] or ""
        try:
            daten = sb_download_pdf(r["storage_pfad"])
            text = pdf_text(daten)
            erg = betrag_aus_text(text, betreff, dateiname)
            merkmale = {"rechnungsart": beleg_art(text, betreff, dateiname), "belegnr": beleg_nummer(text, betreff, dateiname),
                        "referenzen": " ".join(genannte_nummern(text))[:4000] or None, "ustids": " ".join(steuer_ids(text)) or None,
                        "datei_hash": hashlib.sha256(daten).hexdigest()}
        except Exception as e:  # noqa: BLE001 – defektes PDF o. ä.
            erg = {"netto": None, "mwst": None, "brutto": None, "nr": None, "status": "pruefen", "gesamt": None, "zahl": None,
                   "hinweis": f"PDF nicht lesbar ({type(e).__name__})"}
            merkmale = {"rechnungsart": beleg_art("", betreff, dateiname), "belegnr": beleg_nummer("", betreff, dateiname)}
        body = {"betrag_netto": erg["netto"], "betrag_mwst": erg["mwst"], "betrag_brutto": erg["brutto"],
                "betrag_status": erg["status"], "betrag_hinweis": erg["hinweis"], "betrag_ausgelesen_am": jetzt(),
                "gesamt_netto": erg["gesamt"], "zahl_netto": erg["zahl"], **merkmale}
        if erg["nr"]:
            body["rechnungsnummer"] = erg["nr"][:100]
        sb_request("PATCH", f"/rest/v1/eingangsrechnungen?id=eq.{int(r['id'])}", body=body)
        stat["betrag_" + erg["status"]] = stat.get("betrag_" + erg["status"], 0) + 1


# ════════════════════════════════════
# Hauptablauf
# ════════════════════════════════════

def main(nur_ordner=False):
    if not (IMAP_USER and IMAP_PASSWORD and SB_KEY):
        print("FEHLER: Zugangsdaten fehlen (IMAP_USER, IMAP_PASSWORD, SUPABASE_SERVICE_KEY).")
        return 2

    lauf = sb_request("POST", "/rest/v1/rechnungs_sync_laeufe", body={},
                      headers={"Prefer": "return=representation"})[0]
    stat = {"ordner": 0, "bestaetigt": 0, "offen": 0, "ignoriert": 0, "neue_ordner": 0,
            "mails": 0, "ohne_pdf": 0, "neue_rechnungen": 0, "verschoben": 0}
    fehler = None

    try:
        # Bestand aus Supabase. Vorgabe Andy: Rechnungen NUR für Projekte, die laufend oder abgeschlossen sind
        # UND auf die Stunden gebucht wurden (keine Angebote/Anfragen, keine Projekte ohne Stunden).
        projekte_roh = sb_select_all("projects", "id,name:data->>name,typ:data->>typ")
        geloescht = {r["id"] for r in sb_select_all("deleted_ids", "id", "&id=like.projects:*")}
        mit_stunden = {z["projId"] for z in sb_select_all("zeiten", "projId:data->>projId,std:data->>std")
                       if z["projId"] and float(z["std"] or 0) > 0}
        projekte = [(p["id"], p["name"] or "") for p in projekte_roh
                    if f"projects:{p['id']}" not in geloescht
                    and p["typ"] in ("laufend", "abgeschlossen")
                    and str(p["id"]) in mit_stunden]
        erlaubt = {pid for pid, _ in projekte}
        mapping = {m["ordner_name"]: m for m in
                   sb_select_all("rechnungsordner_mapping", "ordner_name,projekt_id,quelle,vorschlag_projekt_id")}
        bekannt = {}
        for r in sb_select_all("eingangsrechnungen", "message_id,ordner_name"):
            bekannt[r["message_id"]] = r["ordner_name"]

        def bestaetigtes_projekt(ordner):
            m = mapping.get(ordner)
            if m and m["quelle"] == "manuell" and m["projekt_id"] in erlaubt:
                return m["projekt_id"]
            return None

        imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
        imap.login(IMAP_USER, IMAP_PASSWORD)
        try:
            ordnerliste = rechnungs_ordner(list_folders(imap))
            alle_ordner = sorted({o for _, o in ordnerliste})
            stat["ordner"] = len(alle_ordner)

            # Jeden Ordner mit Vorschlag eintragen (nur unbestätigte; Andys Entscheidungen bleiben unberührt)
            for ordner in alle_ordner:
                m = mapping.get(ordner)
                if m and m["quelle"] == "manuell":
                    continue
                vorschlag = None if ordner == OHNE_ORDNER else vorschlag_projekt(ordner, projekte)
                if m is None or m["vorschlag_projekt_id"] != vorschlag:
                    sb_request("POST", "/rest/v1/rechnungsordner_mapping?on_conflict=ordner_name",
                               body={"ordner_name": ordner, "projekt_id": None, "quelle": "auto",
                                     "vorschlag_projekt_id": vorschlag, "aktualisiert_am": jetzt()},
                               headers={"Prefer": "resolution=merge-duplicates"})
                    if m is None:
                        stat["neue_ordner"] += 1
                    mapping[ordner] = {"ordner_name": ordner, "projekt_id": None, "quelle": "auto",
                                       "vorschlag_projekt_id": vorschlag}

            for ordner in alle_ordner:
                m = mapping[ordner]
                if bestaetigtes_projekt(ordner):
                    stat["bestaetigt"] += 1
                elif m["quelle"] == "manuell":
                    stat["ignoriert"] += 1
                else:
                    stat["offen"] += 1

            for raw, ordner in ([] if nur_ordner else ordnerliste):
                pid = bestaetigtes_projekt(ordner)
                if pid is None:
                    continue  # nicht bestätigt oder ignoriert → Rechnungen werden nicht übernommen
                typ, _ = imap.select(quote_mailbox(raw), readonly=True)
                if typ != "OK":
                    continue
                typ, data = imap.uid("search", None, "ALL")
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                for uid in uids:
                    typ, hdr = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])")
                    if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                        continue
                    hbytes = hdr[0][1]
                    hmsg = email.message_from_bytes(hbytes, policy=email.policy.default)
                    mid = mail_message_id(hmsg, hbytes)
                    stat["mails"] += 1

                    if mid in bekannt:
                        if bekannt[mid] != ordner:  # Mail wurde in einen anderen Projektordner verschoben
                            sb_request("PATCH", "/rest/v1/eingangsrechnungen?message_id=" + pg_eq(mid),
                                       body={"ordner_name": ordner, "projekt_id": pid, "aktualisiert_am": jetzt()})
                            bekannt[mid] = ordner
                            stat["verschoben"] += 1
                        continue

                    typ, full = imap.uid("fetch", uid, "(BODY.PEEK[])")
                    if typ != "OK" or not full or not isinstance(full[0], tuple):
                        continue
                    msg = email.message_from_bytes(full[0][1], policy=email.policy.default)
                    anhaenge = pdf_anhaenge(msg)
                    if not anhaenge:
                        stat["ohne_pdf"] += 1
                        continue

                    try:
                        datum = email.utils.parsedate_to_datetime(msg.get("Date")).isoformat() if msg.get("Date") else None
                    except Exception:
                        datum = None
                    for idx, (dateiname, pdf) in enumerate(anhaenge, start=1):
                        pfad = None
                        if len(pdf) <= MAX_PDF_BYTES:
                            pfad = storage_pfad(mid, idx, dateiname)
                            sb_upload_pdf(pfad, pdf)
                        sb_request("POST", "/rest/v1/eingangsrechnungen?on_conflict=message_id,dateiname",
                                   body={"projekt_id": pid, "ordner_name": ordner, "message_id": mid,
                                         "absender": str(msg.get("From") or "")[:300],
                                         "betreff": str(msg.get("Subject") or "")[:500],
                                         "mail_datum": datum, "dateiname": dateiname[:300],
                                         "storage_pfad": pfad, "groesse_bytes": len(pdf)},
                                   headers={"Prefer": "resolution=ignore-duplicates"})
                        stat["neue_rechnungen"] += 1
                    bekannt[mid] = ordner
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        # Teil D: eigene Rechnungen aussortieren, Beträge aus neuen PDFs lesen, Doppelte markieren
        if not nur_ordner:
            eigene_markieren(stat)
            betraege_auslesen(stat)
            verrechnen(stat)

        # Bestätigte Zuordnungen auf alle Rechnungen des Ordners anwenden (z. B. nach Änderung in der App)
        for ordner in mapping:
            pid = bestaetigtes_projekt(ordner)
            if pid is not None:
                sb_request("PATCH", "/rest/v1/eingangsrechnungen?ordner_name=" + pg_eq(ordner)
                           + f"&or=(projekt_id.is.null,projekt_id.neq.{int(pid)})",
                           body={"projekt_id": pid, "aktualisiert_am": jetzt()})

    except Exception as e:  # noqa: BLE001 – Fehler protokollieren, aber ohne Details im öffentlichen Log
        fehler = f"{type(e).__name__}: {e}"[:2000]
        print(f"FEHLER ({type(e).__name__}) – Details in Supabase-Tabelle rechnungs_sync_laeufe, Lauf {lauf['id']}")

    sb_request("PATCH", f"/rest/v1/rechnungs_sync_laeufe?id=eq.{lauf['id']}",
               body={"beendet_am": jetzt(), "ordner": stat["ordner"], "neue_rechnungen": stat["neue_rechnungen"],
                     "nicht_zugeordnet": stat["offen"], "fehler": fehler})

    print(f"Modus: {'nur Ordner erfassen' if nur_ordner else 'Rechnungen abholen'} | Ordner: {stat['ordner']} "
          f"(bestätigt: {stat['bestaetigt']}, offen: {stat['offen']}, ignoriert: {stat['ignoriert']}, neu erfasst: {stat['neue_ordner']}) | "
          f"Mails geprüft: {stat['mails']} | neue Rechnungen (PDFs): {stat['neue_rechnungen']} | "
          f"Mails ohne PDF: {stat['ohne_pdf']} | verschoben: {stat['verschoben']} | "
          f"Beträge sicher: {stat.get('betrag_sicher', 0)}, bitte prüfen: {stat.get('betrag_pruefen', 0)} | "
          f"eigene: {stat.get('eigene', 0)}, doppelt: {stat.get('doppelt', 0)}, abgelöst: {stat.get('abgeloest', 0)}, "
          f"keine Rechnung: {stat.get('kein_beleg', 0)}")
    return 1 if fehler else 0


def selbsttest():
    """Prüft die Bausteine mit Beispieldaten – ohne Postfach und ohne Datenbank."""
    from email.message import EmailMessage

    assert imap_utf7_decode("03 BV M&APw-nchen") == "03 BV München"
    assert imap_utf7_decode("A&-B") == "A&B"

    # Vorschläge – ausgedachte Namen (öffentliches Repository!), aber mit denselben Mustern wie im echten Postfach
    projekte = [(10, "Renovierungsarbeiten Lindenweg 13 Feldkirchen Musterbau"),
                (11, "Renovierungsarbeiten Kastanienstr. 14 München Musterbau"),
                (12, "Villa Adler Seeshaupt Bergstr. 61"),
                (13, "Baubegleitung Brandl Apartmenthaus Poing"),
                (14, "Brandl Gartenstr 2 Poing"),
                (15, "Badsanierung Ahornstr. 6 München Musterbau")]
    assert vorschlag_projekt("76 Lindenweg 13 Musterbau Feldkirchen", projekte) == 10
    assert vorschlag_projekt("74 Sendling Musterbau Kastanienstr", projekte) == 11
    assert vorschlag_projekt("03 BV Adler Seeshaupt", projekte) == 12
    assert vorschlag_projekt("59 Poing Brandl Apartments", projekte) == 13      # Apartments ≈ Apartmenthaus
    assert vorschlag_projekt("05 BV Brandl Poing", projekte) is None            # zwei ähnliche → kein Vorschlag
    assert vorschlag_projekt("12 BV Schmidt", projekte) is None                 # kein Treffer
    assert vorschlag_projekt("0000 Archiv", projekte) is None

    m = EmailMessage()
    m["Message-ID"] = "<test@beispiel.de>"
    m["Subject"] = "Rechnung"
    m.set_content("Text")
    m.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="Rechnung 2026-001.pdf")
    m.add_attachment(b"%PDF-1.4 test2", maintype="application", subtype="pdf", filename="Rechnung 2026-001.pdf")
    m.add_attachment(b"kein pdf", maintype="image", subtype="png", filename="logo.png")
    m = email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
    anh = pdf_anhaenge(m)
    assert [a[0] for a in anh] == ["Rechnung 2026-001.pdf", "Rechnung 2026-001 (2).pdf"], anh
    assert mail_message_id(m, b"") == "<test@beispiel.de>"

    pfad = storage_pfad("<test@beispiel.de>", 1, "Rechnung Müller & Söhne (Kopie).pdf")
    assert re.fullmatch(r"[0-9a-f]{20}/1-[A-Za-z0-9._-]+", pfad), pfad
    assert pg_eq('a,b "c"') == "eq." + urllib.parse.quote('"a,b \\"c\\""', safe="")

    ordner = rechnungs_ordner([
        ("INBOX", ["INBOX"], "\\HasChildren"),
        ("INBOX/03 Rechnungen", ["INBOX", "03 Rechnungen"], "\\HasChildren"),
        ("INBOX/03 Rechnungen/76 Lindenweg 13 Musterbau Feldkirchen",
         ["INBOX", "03 Rechnungen", "76 Lindenweg 13 Musterbau Feldkirchen"], "\\HasNoChildren"),
        ("INBOX/03 Rechnungen/47 Kaufmann/Nachtrag", ["INBOX", "03 Rechnungen", "47 Kaufmann", "Nachtrag"], ""),
        ("03 Rechnungen/50 Weber Badsanierung", ["03 Rechnungen", "50 Weber Badsanierung"], ""),
        ("Gesendet", ["Gesendet"], ""),
    ])
    assert [o for _, o in ordner] == [OHNE_ORDNER, "76 Lindenweg 13 Musterbau Feldkirchen", "47 Kaufmann",
                                      "50 Weber Badsanierung"], ordner
    # Beträge – ausgedachte Beispielrechnungen
    e = betrag_aus_text("Musterfirma GmbH\nRechnung Nr. RE-2026-0815\nPosition 1 Fliesen 1.000,00\n"
                        "Nettobetrag 1.000,00 €\nMwSt 19 % 190,00 €\nGesamtbetrag 1.190,00 €")
    assert (e["status"], e["netto"], e["mwst"], e["brutto"], e["nr"]) == ("sicher", 1000.0, 190.0, 1190.0, "RE-2026-0815"), e
    e = betrag_aus_text("Rechnungsnummer: 4711\nSumme netto 2.500,00\n"
                        "Steuerschuldnerschaft des Leistungsempfängers gem. §13b UStG\nRechnungsbetrag 2.500,00")
    assert (e["status"], e["netto"], e["mwst"]) == ("sicher", 2500.0, 0.0), e
    e = betrag_aus_text("Beispiel Baustoffe\nVielen Dank für Ihren Einkauf\nGesamtbetrag 119,00 EUR")
    assert (e["status"], e["netto"], e["brutto"]) == ("pruefen", 100.0, 119.0), e
    e = betrag_aus_text("")
    assert (e["status"], e["netto"]) == ("pruefen", None), e
    e = betrag_aus_text("Leistungszeitraum Oktober\nMwSt 19,00 % 38,00\nNetto 200,00\nBrutto 238,00")
    assert (e["status"], e["netto"]) == ("sicher", 200.0), e
    e = betrag_aus_text("Kleinrechnung Material Musterlager\nNetto 50,00 MwSt 9,50 Brutto 59,50")
    assert (e["status"], e["netto"], e["brutto"]) == ("sicher", 50.0, 59.5), e
    e = betrag_aus_text("Lieferung Beispielware\nNettobetrag 100,00\n7% MwSt 7,00\nEndbetrag 107,00")
    assert (e["status"], e["netto"]) == ("sicher", 100.0), e
    e = betrag_aus_text("Gutschrift Nr. G-12 für Rücksendung\nNetto 100,00\nMwSt 19,00\nBrutto 119,00")
    assert e["status"] == "pruefen" and "Gutschrift" in e["hinweis"], e
    e = betrag_aus_text("Rechnung über Beispielarbeiten\nNetto 1.000,00\nBrutto 1.500,00")  # passt nicht zusammen
    assert e["status"] == "pruefen", e
    e = betrag_aus_text("Beispiel Handwerk GmbH\nNetto MwSt Brutto\n1.000,00 190,00 1.190,00")  # Tabellen-Summen
    assert (e["status"], e["netto"]) == ("sicher", 1000.0), e
    e = betrag_aus_text("BEISPIEL BAUMARKT\nSchrauben 9,99\nSUMME EUR 59,50\nenthaltene MwSt 19% 9,50")  # Kassenbeleg
    assert (e["status"], e["netto"], e["brutto"]) == ("sicher", 50.0, 59.5), e
    e = betrag_aus_text("Beispiel Bau GmbH\nPos 1 Arbeiten 400,00\nPos 2 Material 600,00\n1.000,00\n190,00\n1.190,00")
    assert (e["status"], e["netto"]) == ("sicher", 1000.0) and "Dreier" in e["hinweis"], e  # Dreier-Probe
    # Abschlags-/Schlussrechnungen: Betrag DIESES Belegs, Gesamtleistung und Zahlbetrag getrennt erkennen
    e = betrag_aus_text("3. Abschlagsrechnung Beispielbau\nGesamtleistung netto 50.000,00\nMwSt 19% 9.500,00\n"
                        "Gesamt brutto 59.500,00\nabzüglich bisherige Abschläge 47.600,00\nzu zahlen 11.900,00")
    assert (e["status"], e["netto"], e["gesamt"], e["zahl"]) == ("sicher", 10000.0, 50000.0, 10000.0), e
    e = betrag_aus_text("Abschlagsrechnung Nr. 3 Beispielbau\nNetto 10.000,00\nMwSt 1.900,00\nzu zahlen 11.900,00")
    assert (e["status"], e["netto"]) == ("sicher", 10000.0), e
    e = betrag_aus_text("Beispiel Fenster GmbH\nZwischensumme 1.985,00\nÜbertrag 1.985,00\nGesamtbetrag* 2.359,40\n"
                        "*Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge)\n"
                        "Abzüglich erhaltener Abschlagszahlungen Brutto €\n1. Anzahlungsrechnung vom 01.09.2026 -1.179,70\n"
                        "Forderungsbetrag* 1.179,70")
    assert (e["netto"], e["mwst"], e["gesamt"], e["zahl"]) == (1179.7, 0.0, 2359.4, 1179.7), e  # §13b-Schluss
    e = betrag_aus_text("Beispiel Fensterbau\nSchlussrechnung 9200999\nNetto 1.542,00\nMwSt 292,98\nBrutto 1.834,98\n"
                        "abzüglich Anzahlung 771,00 netto 917,49 brutto\nForderung 917,49")
    assert (e["gesamt"], e["zahl"]) == (1542.0, 771.0), e   # Zahlbetrag steht brutto da → netto umgerechnet

    # Belegart, Belegnummer, genannte Nummern, Steuer-IDs (ausgedachte Belege)
    assert beleg_art("Musterfirma\nAuftragsbestätigung AB2025000999\nAnzahlung 50 % bei Auftrag") == "kein_beleg"
    assert beleg_art("Musterfirma\nSchlussrechnung RE2025000111\nabzüglich Anzahlung RE2025000110") == "schluss"
    assert beleg_art("Musterfirma\n1. Abschlagsrechnung R26/000101 vom 12.06.2026") == "abschlag"
    assert beleg_art("Musterfirma\nRechnungskorrektur 6800001 zu Re 6000001") == "korrektur"
    assert beleg_art("Musterfirma\nRechnung 9200001") == "rechnung"
    assert beleg_art("", "Bestätigung und Anzahlungsrechnung", "7301234 AB Muster.pdf") == "kein_beleg"
    assert beleg_art("Rechnung Beispiel GmbH\nNetto 100,00", "Schlussrechnung BV Muster", "SR.pdf") == "schluss"
    assert beleg_nummer("Musterfirma\n1. Abschlagsrechnung R26/000101 vom 12.06.2026") == "26000101"
    assert beleg_nummer("Musterfirma\nRechnung Nr. RE2025000584\nDatum 01.01.2026") == "2025000584"
    assert beleg_nummer("Musterfirma\nRechnung vom 12.06.2026", "Schlussrechnung 9200549 Muster") == "9200549"
    assert {"6000393", "26000101"} <= set(genannte_nummern("abzgl. RE 600.0393 und R26/000101 vom 1.2.2026"))
    assert steuer_ids("USt-IdNr. DE 123 456 789\nSteuernummer 143/152/83044") == ["DE123456789", "143/152/83044"]

    # Verrechnung je Projekt – ausgedachte Fälle nach den Mustern, die am 31.10.2026 an echten Belegen geprüft wurden
    def mk(i, art, netto, **kw):
        r = {"id": i, "status": "neu", "betrag_status": "sicher", "betrag_netto": netto, "betrag_hinweis": None,
             "rechnungsart": art, "lieferant": None, "ustids": "DE111111111 DE999999999", "belegnr": None,
             "referenzen": None, "gesamt_netto": None, "zahl_netto": None, "datei_hash": f"h{i}", "absender": "a@beispiel.de"}
        r.update(kw)
        return r

    def ergebnis(rows):
        u = verrechne_projekt(rows, {"DE999999999"})
        return {r["id"]: {**r, **u.get(r["id"], {})} for r in rows}

    def kosten(e):
        return round(sum(r["betrag_netto"] for r in e.values()
                         if r["status"] == "neu" and r["betrag_status"] == "sicher"), 2)

    # A) zwei Abschläge + Schlussrechnung (Gesamtleistung zählt, Abschläge abgelöst; Probe 20.000+10.000+15.000)
    e = ergebnis([mk(1, "abschlag", 20000.0, belegnr="26000101"), mk(2, "abschlag", 10000.0, belegnr="26000202"),
                  mk(3, "schluss", 15000.0, belegnr="26000303", gesamt_netto=45000.0, zahl_netto=15000.0,
                     referenzen="26000101 26000202 26000303")])
    assert kosten(e) == 45000.0 and e[1]["status"] == e[2]["status"] == "abgeloest", e
    # B) Auftragsbestätigung + Anzahlung + Schluss; Anzahlung == Restbetrag (darf NICHT als Duplikat gelten)
    e = ergebnis([mk(4, "kein_beleg", 2000.0), mk(5, "abschlag", 1000.0, belegnr="2025000001"),
                  mk(6, "schluss", 1000.0, belegnr="2025000002", gesamt_netto=2000.0, zahl_netto=1000.0, referenzen="2025000001")])
    assert kosten(e) == 2000.0 and e[4]["status"] == "kein_beleg" and e[5]["status"] == "abgeloest", e
    # C) Schlussrechnung doppelt (identische Datei) → nur einmal
    e = ergebnis([mk(7, "abschlag", 700.0, belegnr="2025000441"),
                  mk(8, "schluss", 700.0, belegnr="2025000467", gesamt_netto=1400.0, zahl_netto=700.01, referenzen="2025000441",
                     datei_hash="gleich"),
                  mk(9, "schluss", 700.0, belegnr="2025000467", gesamt_netto=1400.0, zahl_netto=700.01, referenzen="2025000441",
                     datei_hash="gleich")])
    assert kosten(e) == 1400.0 and e[9]["status"] == "doppelt", e
    # D) Rechnung + Storno eines fehlenden Belegs + Schlussrechnung, die alles verrechnet (Rest 0)
    e = ergebnis([mk(10, "rechnung", 500.0, belegnr="9200001"),
                  mk(11, "korrektur", 500.0, belegnr="6800001", referenzen="6800001 6000001"),
                  mk(12, "schluss", 0.0, belegnr="9200002", gesamt_netto=500.0, zahl_netto=0.0,
                     referenzen="6000001 6800001 9200001 9200002")])
    assert kosten(e) == 500.0 and e[10]["status"] == e[11]["status"] == "abgeloest", e
    # E) Abschlag ohne Schlussrechnung → zählt vorläufig
    e = ergebnis([mk(13, "abschlag", 3000.0, belegnr="5550001")])
    assert kosten(e) == 3000.0 and "vorläufig" in e[13]["betrag_hinweis"], e
    # F) Gegenprobe geht nicht auf → nichts zählt automatisch, Andy wird gefragt
    e = ergebnis([mk(14, "abschlag", 30000.0, belegnr="7770001"),
                  mk(15, "schluss", 5000.0, belegnr="7770002", gesamt_netto=25000.0, zahl_netto=5000.0, referenzen="7770001")])
    assert kosten(e) == 0 and e[15]["betrag_status"] == e[14]["betrag_status"] == "pruefen", e
    # G) gleiche Belegnummer desselben Lieferanten, andere Datei (weitergeleitet) → doppelt; gleicher Betrag allein → NICHT
    e = ergebnis([mk(16, "rechnung", 99.0, belegnr="1234567"), mk(17, "rechnung", 99.0, belegnr="1234567"),
                  mk(18, "rechnung", 99.0, belegnr="7654321")])
    assert e[17]["status"] == "doppelt" and e[18]["status"] == "neu" and kosten(e) == 198.0, e
    # Andys eigene Steuernummer steht auf vielen Belegen → wird nicht als Lieferant genommen
    viele = [{"ustids": f"DE{i:09d} DE999999999"} for i in range(1, 13)]
    assert eigene_steuer_ids(viele) == {"DE999999999"}
    assert lieferant_von({"ustids": "DE999999999 DE111111111", "absender": "x@papist-bau.de"}, {"DE999999999"}) == "DE111111111"
    assert lieferant_von({"ustids": "", "absender": "Info <info@beispiel-glas.de>"}, set()) == "beispiel-glas.de"
    assert lieferant_von({"ustids": "", "absender": "info@papist-bau.de"}, set()) is None
    # Betrag im Dateinamen als Abgleich
    e = betrag_aus_text("BEISPIELMARKT\nSUMME EUR 78,94\nenthaltene MwSt 19% 12,60", "", "Beispielmarkt 28.07.26 78,94€ Muster.pdf")
    assert e["status"] == "sicher" and "Dateiname bestätigt" in e["hinweis"], e
    e = betrag_aus_text("BEISPIELMARKT\nSUMME EUR 78,94\nenthaltene MwSt 19% 12,60", "", "Beispielmarkt 13,38€.pdf")
    assert e["status"] == "pruefen" and "Dateiname nennt" in e["hinweis"], e  # Widerspruch → prüfen
    e = betrag_aus_text("", "", "Beispielmarkt 10.09.26 39,98€ Muster.pdf")  # Scan ohne Text
    assert (e["status"], e["brutto"], e["netto"]) == ("pruefen", 39.98, 33.6), e
    # Andys eigene Rechnungen am Dateinamen erkennen (ausgedachte Beispiele)
    assert EIGENE_RE.match("Rechnung 4999-26_Musterkunde_Musterstraße 1.pdf")
    assert EIGENE_RE.match("Schlussrechnung 5001-27_Beispiel SR.pdf")
    assert not EIGENE_RE.match("Rechnung_00108_26.pdf") and not EIGENE_RE.match("Rechnung Beispiel 2260993 2008,13 €.pdf")
    e = betrag_aus_text("Beispielhersteller\nWarenwert : 216,60 EUR\n19,00 % MwSt. : 41,15 EUR\nRechnungswert : 257,75 EUR")
    assert (e["status"], e["netto"]) == ("sicher", 216.6), e

    print("Selbsttest OK")
    return 0


def neu_auslesen():
    """Liest alle automatisch ermittelten Beträge neu aus (von Andy Bestätigtes oder von Hand Erfasstes bleibt unberührt)."""
    sb_request("PATCH", "/rest/v1/eingangsrechnungen?betrag_status=in.(sicher,pruefen)&status=in.(neu,doppelt,abgeloest,kein_beleg)",
               body={"betrag_status": None, "status": "neu", "aktualisiert_am": jetzt()})
    stat = {}
    eigene_markieren(stat)
    betraege_auslesen(stat)
    verrechnen(stat)
    print(f"Neu ausgelesen | Beträge sicher: {stat.get('betrag_sicher', 0)}, bitte prüfen: {stat.get('betrag_pruefen', 0)} | "
          f"eigene: {stat.get('eigene', 0)}, doppelt: {stat.get('doppelt', 0)}, abgelöst: {stat.get('abgeloest', 0)}, "
          f"keine Rechnung: {stat.get('kein_beleg', 0)}")
    return 0


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        sys.exit(selbsttest())
    if ("--diagnose" in sys.argv or "--neu-auslesen" in sys.argv) and not SB_KEY:
        print("FEHLER: SUPABASE_SERVICE_KEY fehlt.")
        sys.exit(2)
    if "--diagnose" in sys.argv:
        diagnose()
        sys.exit(0)
    if "--neu-auslesen" in sys.argv:
        sys.exit(neu_auslesen())
    sys.exit(main(nur_ordner="--nur-ordner" in sys.argv))

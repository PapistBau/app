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
ZU_ZAHLEN = r"zu\s+zahlen|zahlbetrag|zahlbar|auszahlungsbetrag|restbetrag|forderungsbetrag"
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


# Abschlags-/Schlussrechnungen: immer prüfen (Vorgabe Andy), damit Abschläge nicht doppelt zählen
ABSCHLUSS = r"abschlag|schlussrechnung|schlußrechnung|teilrechnung|anzahlung"
# Andys eigene Kundenrechnungen (Nummernkreis z. B. „Rechnung 4436-26_…“) sind keine Kosten (Vorgabe Andy)
EIGENE_RE = re.compile(r"^(?:rechnung|gutschrift|schlussrechnung|stornorechnung)\s*\d{4}-\d{2}(?:[_\s.]|$)", re.IGNORECASE)


def betrag_aus_text(text, betreff="", dateiname=""):
    """Liefert dict mit netto, mwst, brutto, nr, status ('sicher'/'pruefen') und hinweis."""
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
    # Abschlags-/Schlussrechnungen nie automatisch zählen
    if re.search(ABSCHLUSS, f"{betreff} {dateiname} {text or ''}".lower()) and erg["status"] == "sicher":
        erg["status"] = "pruefen"
        erg["hinweis"] = "Abschlags-/Schlussrechnung – bitte prüfen, ob Abschläge doppelt zählen" + \
                         (f" ({erg['hinweis']})" if erg["hinweis"] else "")
    return erg


def _betrag_aus_pdf(text):
    erg = {"netto": None, "mwst": None, "brutto": None, "nr": None, "status": "pruefen", "hinweis": None}
    if not text or len(text.strip()) < 20:
        erg["hinweis"] = "kein lesbarer Text (evtl. Scan)"
        return erg
    nr = NR_RE.search(text)
    if nr:
        erg["nr"] = nr.group(1).strip(".-/")
    klein = text.lower()
    k = kandidaten(text)
    netto_k, mwst_k, brutto_k = set(k["netto"]), set(k["mwst"]), set(k["brutto"])
    zu_zahlen = set(k["zu_zahlen"])
    abschlag = re.search(ABSCHLAG, klein) is not None

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
    quelle = "beschriftet"
    if not passend:
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
        # Bei Abschlags-/Teilrechnungen steht oft die Gesamtleistung als größte Summe darauf.
        # Dann nur „sicher“, wenn die Kombination genau dem Betrag unter „zu zahlen“ entspricht.
        if abschlag:
            treffer = [(b, n) for b, n in passend if b in zu_zahlen]
            if len({b for b, _ in treffer}) == 1:
                b, n = treffer[0]
                erg.update(netto=n, brutto=b, mwst=round(b - n, 2), status="sicher",
                           hinweis="Abschlagsrechnung: Betrag „zu zahlen“")
            else:
                b, n = min(passend)
                erg.update(netto=n, brutto=b, mwst=round(b - n, 2),
                           hinweis="Abschlags-/Teilrechnung – welcher Betrag gilt? bitte prüfen")
        else:
            b, n = max(passend)
            erg.update(netto=n, brutto=b, mwst=round(b - n, 2), status="sicher",
                       hinweis=None if quelle == "beschriftet" else "Summen per Dreier-Probe erkannt")
    else:
        # 2) §13b: keine MwSt, Netto = Rechnungsbetrag
        p13b = "13b" in klein or "steuerschuldnerschaft" in klein or "reverse charge" in klein
        endbetraege = sorted(netto_k | brutto_k)
        if p13b and endbetraege and abschlag:
            # §13b-Schlussrechnung mit abgezogenen Anzahlungen: es zählt nur der noch zu zahlende Betrag,
            # die Anzahlungsrechnungen sind selbst schon erfasst (sonst doppelt)
            if len(zu_zahlen) == 1:
                wert = next(iter(zu_zahlen))
                erg.update(netto=wert, brutto=wert, mwst=0.0, status="sicher",
                           hinweis="§13b, abzüglich Anzahlungen: Forderungsbetrag")
            else:
                erg.update(netto=min(endbetraege), hinweis="§13b mit Anzahlungen – welcher Betrag gilt? bitte prüfen")
        elif p13b and endbetraege:
            if len(endbetraege) == 1 or (netto_k & brutto_k):
                wert = max(netto_k & brutto_k) if netto_k & brutto_k else endbetraege[0]
                erg.update(netto=wert, brutto=wert, mwst=0.0, status="sicher", hinweis="§13b (ohne MwSt)")
            else:
                erg.update(netto=max(endbetraege), hinweis="§13b, mehrere mögliche Beträge")
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


def doppelte_markieren(stat):
    """Eindeutig doppelte Rechnungen (gleiches Projekt + gleicher Betrag + gleiche Rechnungsnummer oder gleiche Datei)
    zählen nur einmal: die ältere bleibt, die weiteren bekommen status 'doppelt' (bleiben sichtbar)."""
    rows = sb_select_all("eingangsrechnungen", "id,projekt_id,betrag_netto,rechnungsnummer,groesse_bytes",
                         "&status=eq.neu&betrag_netto=not.is.null&order=id")
    gesehen, doppelt = set(), []
    for r in rows:
        schluessel = []
        if r["rechnungsnummer"]:
            schluessel.append(("nr", r["projekt_id"], r["betrag_netto"], r["rechnungsnummer"].lower()))
        if r["groesse_bytes"]:
            schluessel.append(("datei", r["projekt_id"], r["betrag_netto"], r["groesse_bytes"]))
        if any(s in gesehen for s in schluessel):
            doppelt.append(r["id"])
        gesehen.update(schluessel)
    for rid in doppelt:
        sb_request("PATCH", f"/rest/v1/eingangsrechnungen?id=eq.{int(rid)}",
                   body={"status": "doppelt", "aktualisiert_am": jetzt()})
    stat["doppelt"] = stat.get("doppelt", 0) + len(doppelt)


def betraege_auslesen(stat, maximal=1500):
    """Liest für alle Rechnungen ohne Betrag-Status den Betrag aus dem PDF."""
    offen = sb_select_all("eingangsrechnungen", "id,storage_pfad,betreff,dateiname",
                          "&betrag_status=is.null&status=eq.neu&storage_pfad=not.is.null&order=id")
    for r in offen[:maximal]:
        try:
            erg = betrag_aus_text(pdf_text(sb_download_pdf(r["storage_pfad"])), r["betreff"] or "", r["dateiname"] or "")
        except Exception as e:  # noqa: BLE001 – defektes PDF o. ä.
            erg = {"netto": None, "mwst": None, "brutto": None, "nr": None, "status": "pruefen",
                   "hinweis": f"PDF nicht lesbar ({type(e).__name__})"}
        body = {"betrag_netto": erg["netto"], "betrag_mwst": erg["mwst"], "betrag_brutto": erg["brutto"],
                "betrag_status": erg["status"], "betrag_hinweis": erg["hinweis"], "betrag_ausgelesen_am": jetzt()}
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
            doppelte_markieren(stat)

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
          f"eigene Rechnungen: {stat.get('eigene', 0)}, doppelte: {stat.get('doppelt', 0)}")
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
    e = betrag_aus_text("3. Abschlagsrechnung Beispielbau\nGesamtleistung netto 50.000,00\nMwSt 19% 9.500,00\n"
                        "Gesamt brutto 59.500,00\nabzüglich bisherige Abschläge 47.600,00\nzu zahlen 11.900,00")
    assert e["status"] == "pruefen" and "Abschlag" in e["hinweis"], e  # Gesamtleistung darf NICHT sicher zählen
    # Abschlags-/Schlussrechnungen: Betrag wird erkannt, aber immer „bitte prüfen“ (Vorgabe Andy)
    e = betrag_aus_text("Abschlagsrechnung Nr. 3 Beispielbau\nNetto 10.000,00\nMwSt 1.900,00\nzu zahlen 11.900,00")
    assert (e["status"], e["netto"]) == ("pruefen", 10000.0), e
    e = betrag_aus_text("Beispiel Fenster GmbH\nZwischensumme 1.985,00\nÜbertrag 1.985,00\nGesamtbetrag* 2.359,40\n"
                        "*Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge)\n"
                        "Abzüglich erhaltener Abschlagszahlungen Brutto €\n1. Anzahlungsrechnung vom 01.09.2026 -1.179,70\n"
                        "Forderungsbetrag* 1.179,70")
    assert (e["status"], e["netto"], e["mwst"]) == ("pruefen", 1179.7, 0.0), e  # §13b: Vorschlag Forderungsbetrag
    e = betrag_aus_text("Rechnung Beispiel GmbH\nNetto 100,00\nMwSt 19,00\nBrutto 119,00", "Schlussrechnung BV Muster", "SR.pdf")
    assert e["status"] == "pruefen", e  # Schlussrechnung nur im Betreff erkannt
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
    sb_request("PATCH", "/rest/v1/eingangsrechnungen?betrag_status=in.(sicher,pruefen)&status=in.(neu,doppelt)",
               body={"betrag_status": None, "status": "neu", "aktualisiert_am": jetzt()})
    stat = {}
    eigene_markieren(stat)
    betraege_auslesen(stat)
    doppelte_markieren(stat)
    print(f"Neu ausgelesen | Beträge sicher: {stat.get('betrag_sicher', 0)}, bitte prüfen: {stat.get('betrag_pruefen', 0)} | "
          f"eigene Rechnungen: {stat.get('eigene', 0)}, doppelte: {stat.get('doppelt', 0)}")
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

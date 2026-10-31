"""
Rechnungs-Sync: holt Eingangsrechnungen (PDF-Anhänge) aus dem Postfach rechnungen@papist-bau.de
und legt sie in Supabase ab (Tabelle eingangsrechnungen + privater Speicher "eingangsrechnungen").

Postfach-Aufbau:  Posteingang/03 Rechnungen/<Nr> <Bauvorhaben>/...   z. B. ".../76 Amperweg 13 Tassilobau Kirchheim"
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
        # Bestand aus Supabase
        projekte_roh = sb_select_all("projects", "id,name:data->>name")
        geloescht = {r["id"] for r in sb_select_all("deleted_ids", "id", "&id=like.projects:*")}
        projekte = [(p["id"], p["name"] or "") for p in projekte_roh if f"projects:{p['id']}" not in geloescht]
        mapping = {m["ordner_name"]: m for m in
                   sb_select_all("rechnungsordner_mapping", "ordner_name,projekt_id,quelle,vorschlag_projekt_id")}
        bekannt = {}
        for r in sb_select_all("eingangsrechnungen", "message_id,ordner_name"):
            bekannt[r["message_id"]] = r["ordner_name"]

        def bestaetigtes_projekt(ordner):
            m = mapping.get(ordner)
            return m["projekt_id"] if m and m["quelle"] == "manuell" and m["projekt_id"] else None

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
          f"Mails ohne PDF: {stat['ohne_pdf']} | verschoben: {stat['verschoben']}")
    return 1 if fehler else 0


def selbsttest():
    """Prüft die Bausteine mit Beispieldaten – ohne Postfach und ohne Datenbank."""
    from email.message import EmailMessage

    assert imap_utf7_decode("03 BV M&APw-nchen") == "03 BV München"
    assert imap_utf7_decode("A&-B") == "A&B"

    # Vorschläge mit echten Namensmustern aus Postfach und App
    projekte = [(10, "Renovierungsarbeiten Amperweg 13 Kirchheim Tassilobau"),
                (11, "Renovierungsarbeiten Meichelbeckstr. 14 München Tassilobau"),
                (12, "Villa Hauber Murnau Kohlgruberstr. 61"),
                (13, "Baubegleitung Bauer Apartmenthaus Aschheim"),
                (14, "Bauer Ismaningerstr 2 Aschheim"),
                (15, "Badsanierung Stollbergstr. 6 München Tassilobau")]
    assert vorschlag_projekt("76 Amperweg 13 Tassilobau Kirchheim", projekte) == 10
    assert vorschlag_projekt("74 Harlaching Tassilobau Meichelbeckstr", projekte) == 11
    assert vorschlag_projekt("03 BV Hauber Murnau", projekte) == 12
    assert vorschlag_projekt("59 Aschheim Bauer Apartments", projekte) is None  # zwei ähnliche → kein Vorschlag
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
        ("INBOX/03 Rechnungen/76 Amperweg 13 Tassilobau Kirchheim",
         ["INBOX", "03 Rechnungen", "76 Amperweg 13 Tassilobau Kirchheim"], "\\HasNoChildren"),
        ("INBOX/03 Rechnungen/47 Trittner/Nachtrag", ["INBOX", "03 Rechnungen", "47 Trittner", "Nachtrag"], ""),
        ("03 Rechnungen/50 Wehner Badsanierung", ["03 Rechnungen", "50 Wehner Badsanierung"], ""),
        ("Gesendet", ["Gesendet"], ""),
    ])
    assert [o for _, o in ordner] == [OHNE_ORDNER, "76 Amperweg 13 Tassilobau Kirchheim", "47 Trittner",
                                      "50 Wehner Badsanierung"], ordner
    print("Selbsttest OK")
    return 0


if __name__ == "__main__":
    sys.exit(selbsttest() if "--selbsttest" in sys.argv else main(nur_ordner="--nur-ordner" in sys.argv))

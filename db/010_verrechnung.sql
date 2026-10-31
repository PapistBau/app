-- Rechnungs-Sync Teil D (31.10.2026): Abschläge / Schlussrechnungen automatisch verrechnen.
-- Je Beleg werden Belegart, Lieferant und Nummern gespeichert; die Verrechnung je Projekt übernimmt das Skript.
-- Neue Werte für status: 'abgeloest' (durch Schlussrechnung ersetzt, sichtbar, zählt nicht),
--                         'kein_beleg' (Auftragsbestätigung, Freigabe, Angebot, Lieferschein – keine Kosten).
-- Rückweg: alter table public.eingangsrechnungen drop column rechnungsart, drop column lieferant, drop column ustids,
--          drop column belegnr, drop column referenzen, drop column gesamt_netto, drop column zahl_netto, drop column datei_hash;

alter table public.eingangsrechnungen
  add column if not exists rechnungsart text,      -- rechnung | abschlag | schluss | korrektur | gutschrift | kein_beleg
  add column if not exists lieferant    text,      -- USt-IdNr./Steuernummer des Ausstellers, sonst Absender-Domain
  add column if not exists ustids       text,      -- alle im PDF gefundenen USt-IdNr./Steuernummern (Leerzeichen-getrennt)
  add column if not exists belegnr      text,      -- eigene Belegnummer, nur Ziffern (z. B. „RE 600.0393“ → 6000393)
  add column if not exists referenzen   text,      -- im PDF genannte Nummern (nur Ziffern), z. B. Abzugsliste der Schlussrechnung
  add column if not exists gesamt_netto numeric(12,2),  -- Schlussrechnung: Gesamtleistung vor Abzügen
  add column if not exists zahl_netto   numeric(12,2),  -- Zahl-/Rest-/Forderungsbetrag netto
  add column if not exists datei_hash   text;      -- Prüfwert der PDF-Datei (erkennt identische Kopien)

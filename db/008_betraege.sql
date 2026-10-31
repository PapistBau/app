-- Rechnungs-Sync Teil D (31.10.2026): Beträge aus den PDFs.
-- betrag_status:
--   NULL         = noch nicht ausgelesen
--   'sicher'     = automatisch gelesen und rechnerisch stimmig (Netto + MwSt = Brutto, oder §13b ohne MwSt) → zählt
--   'pruefen'    = nicht eindeutig / kein lesbarer Text → zählt erst nach Andys Bestätigung
--   'bestaetigt' = von Andy in der App bestätigt oder korrigiert → zählt
--   'manuell'    = von Hand erfasste Rechnung → zählt
-- Rückweg: alter table public.eingangsrechnungen drop column betrag_mwst, drop column betrag_status,
--          drop column betrag_hinweis, drop column betrag_ausgelesen_am;

alter table public.eingangsrechnungen
  add column if not exists betrag_mwst          numeric(12,2),
  add column if not exists betrag_status        text check (betrag_status in ('sicher','pruefen','bestaetigt','manuell')),
  add column if not exists betrag_hinweis       text,
  add column if not exists betrag_ausgelesen_am timestamptz;

-- Bereits von Hand erfasste Rechnungen (Teil C) zählen als 'manuell'
update public.eingangsrechnungen set betrag_status = 'manuell'
where status = 'manuell' and betrag_netto is not null and betrag_status is null;

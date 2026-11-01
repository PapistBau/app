-- Teil E (31.10.2026): Andys eigene Belege aus OneDrive (Ausgangsrechnungen + Auftragsbestätigungen)
-- Hochgeladen von einer Aufgabe auf Andys PC (scripts/belege_hochladen.ps1), ausgelesen und verrechnet
-- im GitHub-Ablauf (scripts/ausgangsbelege.py). Daraus entstehen:
--   · Einträge auf der Seite „Rechnungen“ (bestehende Tabelle rechnungen, Kennzeichen data.quelle = 'beleg')
--   · die automatische Auftragssumme je Projekt (berechnet in der App aus diesen Belegen)
-- Neue Tabellen, bestehende bleiben unverändert. Lesen/Ändern nur für den Admin (is_admin(), siehe 001).
-- Rückweg: db/011_ausgangsbelege_rollback.sql

begin;

-- ── Eine Zeile pro PDF ──
create table if not exists public.ausgangsbelege (
  id                 bigserial primary key,
  quelle             text not null check (quelle in ('rechnung', 'auftrag')),  -- Ordner 003 Rechnungen / 002 Aufträge
  ordner             text not null,
  dateiname          text not null,
  datei_hash         text not null unique,
  groesse_bytes      bigint,
  datei_geaendert_am timestamptz,
  storage_pfad       text not null,
  gruppe             text,            -- Kunde + Projekt aus dem Ordnernamen (Schlüssel für die Projektzuordnung)
  belegart           text,            -- abschlag | schluss | rechnung | storno | auftrag
  nummer             text,            -- z. B. 4447-26 (Rechnung) bzw. 3174-26 (Auftragsbestätigung)
  bezug_nummer       text,            -- Storno: stornierte Rechnung · Schlussrechnung: Auftragsbestätigung/Angebot
  datum              date,
  faellig            date,
  netto              numeric(12,2),   -- Schlussrechnung: Gesamtleistung netto
  mwst               numeric(12,2),
  brutto             numeric(12,2),
  rest_netto         numeric(12,2),   -- Schlussrechnung: noch zu zahlen (Gesamt − Abschläge)
  rest_brutto        numeric(12,2),
  abzuege            jsonb,           -- Schlussrechnung: [{"nummer":"4404-26","netto":8439.78}, …]
  steuer_13b         boolean,
  betrag_status      text,            -- sicher | pruefen | bestaetigt
  betrag_hinweis     text,
  text_quelle        text,            -- pdf | ocr | keiner
  status             text not null default 'neu',   -- neu | ersetzt (ältere Fassung) | geloescht
  storniert_durch    text,            -- Nummer der Stornorechnung
  verrechnet_in      text,            -- Abschlag: Nummer der Schlussrechnung
  rechnung_id        text,            -- angelegter Eintrag in Tabelle rechnungen
  ausgelesen_am      timestamptz,
  erstellt_am        timestamptz not null default now(),
  aktualisiert_am    timestamptz not null default now()
);
create index if not exists ausgangsbelege_gruppe on public.ausgangsbelege (gruppe);
create index if not exists ausgangsbelege_nummer on public.ausgangsbelege (nummer);

-- ── Zuordnung Kunde/Projekt (aus dem Ordnernamen) → Projekt in der App ──
-- Gleiche Regel wie rechnungsordner_mapping: quelle 'auto' = nur Vorschlag; 'manuell' + projekt_id = von Andy bestätigt;
-- 'manuell' ohne projekt_id = ignoriert
create table if not exists public.ausgangs_gruppen (
  gruppe               text primary key,
  beispiel             text,
  projekt_id           bigint references public.projects(id) on delete set null,
  vorschlag_projekt_id bigint,
  quelle               text not null default 'auto' check (quelle in ('auto', 'manuell')),
  aktualisiert_am      timestamptz not null default now()
);

do $$
declare t text;
begin
  foreach t in array array['ausgangsbelege','ausgangs_gruppen'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon', t);
    execute format('drop policy if exists admin_all on public.%I', t);
    execute format('create policy admin_all on public.%I for all to authenticated
                    using (public.is_admin()) with check (public.is_admin())', t);
  end loop;
end $$;

-- ── Privater PDF-Speicher ──
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('ausgangsbelege', 'ausgangsbelege', false, 52428800, array['application/pdf'])
on conflict (id) do nothing;

drop policy if exists ausgangsbelege_admin_lesen on storage.objects;
create policy ausgangsbelege_admin_lesen on storage.objects for select to authenticated
  using (bucket_id = 'ausgangsbelege' and public.is_admin());

-- Nebenbefund Teil D: „Kundennr“ o. ä. (Spaltenüberschrift) war als Rechnungsnummer gespeichert
update public.eingangsrechnungen set rechnungsnummer = null where rechnungsnummer is not null and rechnungsnummer !~ '\d';

commit;

-- Rechnungs-Sync Teil A (31.10.2026): Eingangsrechnungen aus dem Postfach rechnungen@papist-bau.de
-- Neu und unabhängig von der bestehenden Tabelle "rechnungen" (= gestellte Ausgangsrechnungen).
-- Lesen/Ändern nur für den Admin (is_admin(), siehe 001). Das Sync-Skript schreibt mit eigenem
-- Supabase-Geheimschlüssel (umgeht RLS). PDFs liegen im privaten Speicher "eingangsrechnungen".
-- Rückweg: db/004_eingangsrechnungen_rollback.sql

begin;

-- ── Eine Zeile pro PDF-Anhang ──
create table if not exists public.eingangsrechnungen (
  id              bigserial primary key,
  projekt_id      bigint references public.projects(id) on delete set null,
  ordner_name     text not null,
  message_id      text not null,
  absender        text,
  betreff         text,
  mail_datum      timestamptz,
  dateiname       text not null,
  storage_pfad    text,
  groesse_bytes   integer,
  betrag_netto    numeric(12,2),
  betrag_brutto   numeric(12,2),
  rechnungsnummer text,
  status          text not null default 'neu',
  erstellt_am     timestamptz not null default now(),
  aktualisiert_am timestamptz not null default now(),
  unique (message_id, dateiname)
);
create index if not exists eingangsrechnungen_projekt on public.eingangsrechnungen (projekt_id);
create index if not exists eingangsrechnungen_ordner on public.eingangsrechnungen (ordner_name);

-- ── Zuordnung Postfach-Ordner → Projekt ──
create table if not exists public.rechnungsordner_mapping (
  ordner_name     text primary key,
  projekt_id      bigint references public.projects(id) on delete set null,
  quelle          text not null check (quelle in ('auto', 'manuell')),
  aktualisiert_am timestamptz not null default now()
);

-- ── Protokoll der Sync-Läufe (für "letzter Abgleich" in der App) ──
create table if not exists public.rechnungs_sync_laeufe (
  id                bigserial primary key,
  gestartet_am      timestamptz not null default now(),
  beendet_am        timestamptz,
  ordner            integer,
  neue_rechnungen   integer,
  nicht_zugeordnet  integer,
  fehler            text
);

-- ── Schutz: nur Admin ──
do $$
declare t text;
begin
  foreach t in array array['eingangsrechnungen','rechnungsordner_mapping','rechnungs_sync_laeufe'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon', t);
    execute format('drop policy if exists admin_all on public.%I', t);
    execute format('create policy admin_all on public.%I for all to authenticated
                    using (public.is_admin()) with check (public.is_admin())', t);
  end loop;
end $$;

-- ── Privater PDF-Speicher ──
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('eingangsrechnungen', 'eingangsrechnungen', false, 26214400, array['application/pdf'])
on conflict (id) do nothing;

drop policy if exists eingangsrechnungen_admin_lesen on storage.objects;
create policy eingangsrechnungen_admin_lesen on storage.objects for select to authenticated
  using (bucket_id = 'eingangsrechnungen' and public.is_admin());

commit;

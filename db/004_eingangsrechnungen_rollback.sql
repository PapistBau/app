-- Rückweg zu 004_eingangsrechnungen.sql. ACHTUNG: löscht alle importierten Eingangsrechnungen
-- (die Mails im Postfach bleiben unberührt, ein neuer Sync holt sie wieder).
-- Die PDFs im Speicher vorher im Supabase-Dashboard unter Storage → eingangsrechnungen leeren,
-- dann diesen Rückweg ausführen.
begin;
drop policy if exists eingangsrechnungen_admin_lesen on storage.objects;
delete from storage.buckets where id = 'eingangsrechnungen';
drop table if exists public.rechnungs_sync_laeufe;
drop table if exists public.rechnungsordner_mapping;
drop table if exists public.eingangsrechnungen;
commit;

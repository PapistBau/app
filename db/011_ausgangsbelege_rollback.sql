-- Rückweg zu db/011_ausgangsbelege.sql: entfernt die Tabellen für Andys eigene Belege.
-- Die von den Belegen angelegten Einträge auf der Seite „Rechnungen“ bleiben, bis sie hier mit gelöscht werden:
--   delete from public.rechnungen where data->>'quelle' = 'beleg';
-- PDFs im Speicher „ausgangsbelege“ vorher im Supabase-Dashboard löschen (Storage → ausgangsbelege → alle markieren).

begin;
drop policy if exists ausgangsbelege_admin_lesen on storage.objects;
drop table if exists public.ausgangs_gruppen;
drop table if exists public.ausgangsbelege;
commit;

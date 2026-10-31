-- Schritt 6 Sicherheitsumbau (31.10.2026): Datenbank dicht machen
-- Entfernt die Regeln "anon_all" (jeder ohne Anmeldung darf alles) und nimmt dem
-- öffentlichen Schlüssel zusätzlich die Tabellenrechte.
-- Danach gilt: Tabellen nur noch für den Admin (Regel admin_all, siehe 001),
-- Mitarbeiter ausschließlich über die ma_*-Funktionen (siehe 002).
-- Rückweg: db/003_anon_sperren_rollback.sql

begin;

do $$
declare t text;
begin
  foreach t in array array['projects','workers','zeiten','rechnungen','todos','allgtodos',
                           'kategorien','material','fotos','deleted_ids','settings']
  loop
    execute format('drop policy if exists anon_all on public.%I', t);
    execute format('revoke all on public.%I from anon', t);
  end loop;
end $$;

commit;

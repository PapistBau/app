-- Rückweg zu 002_mitarbeiter_pin.sql: entfernt die PIN-Anmeldung auf dem Server wieder.
-- Achtung: Die App-Version mit Server-PIN funktioniert danach nicht mehr → vorher App zurücksetzen.
begin;
drop function if exists public.ma_logout(uuid);
drop function if exists public.ma_zeit_loeschen(uuid, bigint);
drop function if exists public.ma_zeit_speichern(uuid, jsonb);
drop function if exists public.ma_daten(uuid);
drop function if exists public.ma_login(text);
drop function if exists public._buchbare_projekte();
drop function if exists public._ma_worker(uuid);
drop function if exists public._client_ip();
drop table if exists public.pin_fehlversuche;
drop table if exists public.ma_sessions;
commit;

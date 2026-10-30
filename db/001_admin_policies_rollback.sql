-- Rückweg zu 001_admin_policies.sql: entfernt die Admin-Regeln wieder.
begin;

do $$
declare t text;
begin
  foreach t in array array['projects','workers','zeiten','rechnungen','todos','allgtodos',
                           'kategorien','material','fotos','deleted_ids','settings']
  loop
    execute format('drop policy if exists admin_all on public.%I', t);
  end loop;
end $$;

drop function if exists public.is_admin();

commit;

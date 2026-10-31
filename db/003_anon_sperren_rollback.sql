-- Rückweg zu 003_anon_sperren.sql: stellt den alten offenen Zustand wieder her
-- (jeder mit dem öffentlichen Schlüssel darf wieder alles). Nur im Notfall!
begin;

do $$
declare t text;
begin
  foreach t in array array['projects','workers','zeiten','rechnungen','todos','allgtodos',
                           'kategorien','material','fotos','deleted_ids','settings']
  loop
    execute format('grant select, insert, update, delete on public.%I to anon', t);
    execute format('drop policy if exists anon_all on public.%I', t);
    execute format('create policy anon_all on public.%I for all to anon using (true) with check (true)', t);
  end loop;
end $$;

commit;

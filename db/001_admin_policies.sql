-- Schritt 3 Sicherheitsumbau (30.10.2026)
-- Der eine Admin (info@papist-bau.de) darf mit seiner Supabase-Anmeldung alles.
-- Rein ergänzend: die bestehenden Regeln "anon_all" bleiben bis Schritt 6 unverändert.
-- Rückweg: db/001_admin_policies_rollback.sql

begin;

create or replace function public.is_admin() returns boolean
language sql stable
as $$ select auth.uid() = '785806c1-9fc9-4fb7-b520-05c6910a6ef9'::uuid $$;

do $$
declare t text;
begin
  foreach t in array array['projects','workers','zeiten','rechnungen','todos','allgtodos',
                           'kategorien','material','fotos','deleted_ids','settings']
  loop
    execute format('drop policy if exists admin_all on public.%I', t);
    execute format('create policy admin_all on public.%I for all to authenticated
                    using (public.is_admin()) with check (public.is_admin())', t);
  end loop;
end $$;

commit;

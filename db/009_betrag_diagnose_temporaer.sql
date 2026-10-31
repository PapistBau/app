-- VORÜBERGEHEND (31.10.2026): Betrags-Zeilen von ~30 Beispielrechnungen zum Abstimmen der Erkennung (Teil D).
-- Kein Zugriff von außen (RLS an, keine Regeln, Rechte entzogen). Wird nach der Auswertung wieder gelöscht:
--   drop table if exists public.betrag_diagnose;
create table if not exists public.betrag_diagnose (
  id          bigserial primary key,
  rechnung_id bigint,
  hinweis     text,
  zeilen      text,
  erstellt_am timestamptz not null default now()
);
alter table public.betrag_diagnose enable row level security;
revoke all on public.betrag_diagnose from anon, authenticated;

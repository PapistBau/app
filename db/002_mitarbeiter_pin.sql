-- Schritt 4 Sicherheitsumbau (30.10.2026): PIN-Anmeldung der Mitarbeiter auf dem Server
-- Mitarbeiter bekommen nur noch: eigenen Namen, eigene Stunden, buchbare Baustellen (Name/Ort).
-- Rein ergänzend: bestehende Tabellen und Regeln bleiben unverändert.
-- Rückweg: db/002_mitarbeiter_pin_rollback.sql

begin;

-- ── Interne Tabellen (RLS an, keine Regeln → von außen weder lesbar noch schreibbar) ──
create table if not exists public.ma_sessions (
  token      uuid primary key default gen_random_uuid(),
  worker_id  bigint not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '12 hours'
);
alter table public.ma_sessions enable row level security;
revoke all on public.ma_sessions from anon, authenticated;

create table if not exists public.pin_fehlversuche (
  id     bigserial primary key,
  quelle text not null,
  zeit   timestamptz not null default now()
);
create index if not exists pin_fehlversuche_quelle_zeit on public.pin_fehlversuche (quelle, zeit);
alter table public.pin_fehlversuche enable row level security;
revoke all on public.pin_fehlversuche from anon, authenticated;

-- ── Hilfsfunktionen (nicht von außen aufrufbar) ──

-- Anschluss des Aufrufers. cf-connecting-ip setzt Cloudflare selbst, der Aufrufer kann es nicht fälschen.
create or replace function public._client_ip() returns text
language sql stable set search_path = public as $$
  select coalesce(
    nullif(current_setting('request.headers', true), '')::json->>'cf-connecting-ip',
    'unbekannt')
$$;

-- Mitarbeiter-ID zu einem gültigen Zugangsschein (null, wenn abgelaufen/ungültig/archiviert)
create or replace function public._ma_worker(p_token uuid) returns bigint
language sql stable security definer set search_path = public as $$
  select s.worker_id
  from ma_sessions s join workers w on w.id = s.worker_id
  where s.token = p_token and s.expires_at > now()
    and (w.data->>'archiviert') is distinct from 'true'
$$;

-- Baustellen, auf die gebucht werden darf (wie bisher in der App: laufend/abgeschlossen, nicht gesperrt)
create or replace function public._buchbare_projekte() returns setof bigint
language sql stable security definer set search_path = public as $$
  select p.id from projects p
  where p.data->>'typ' in ('laufend', 'abgeschlossen')
    and (p.data->>'zeitGesperrt') is distinct from 'true'
    and not exists (select 1 from deleted_ids d where d.id = 'projects:' || p.id)
    and p.id not between 1 and 7
$$;

-- ── Anmeldung per PIN ──
create or replace function public.ma_login(p_pin text) returns json
language plpgsql security definer set search_path = public as $$
declare
  v_ip     text := _client_ip();
  v_fehl   int;
  v_fuenft timestamptz;
  v_anzahl int;
  v_w      record;
  v_token  uuid;
begin
  -- Aufräumen
  delete from pin_fehlversuche where zeit < now() - interval '1 day';
  delete from ma_sessions where expires_at < now();

  -- Gesperrt? (5 Fehlversuche in 15 Minuten)
  select count(*) into v_fehl from pin_fehlversuche
  where quelle = v_ip and zeit > now() - interval '15 minutes';
  if v_fehl >= 5 then
    select zeit into v_fuenft from pin_fehlversuche
    where quelle = v_ip order by zeit desc offset 4 limit 1;
    return json_build_object('ok', false, 'gesperrt', true,
      'sekunden', greatest(1, ceil(extract(epoch from (v_fuenft + interval '15 minutes' - now())))));
  end if;

  select count(*) into v_anzahl from workers
  where coalesce(p_pin, '') ~ '^[0-9]{4}$'
    and data->>'pin' = p_pin and (data->>'archiviert') is distinct from 'true';

  if v_anzahl = 0 then
    insert into pin_fehlversuche (quelle) values (v_ip);
    v_fehl := v_fehl + 1;
    if v_fehl >= 5 then
      return json_build_object('ok', false, 'gesperrt', true, 'sekunden', 900);
    end if;
    return json_build_object('ok', false, 'rest', 5 - v_fehl);
  end if;

  if v_anzahl > 1 then
    return json_build_object('ok', false, 'doppelt', true);
  end if;

  select id, data->>'vorname' as vorname, data->>'nachname' as nachname into v_w
  from workers where data->>'pin' = p_pin and (data->>'archiviert') is distinct from 'true';

  delete from pin_fehlversuche where quelle = v_ip;
  insert into ma_sessions (worker_id) values (v_w.id) returning token into v_token;

  return json_build_object('ok', true, 'token', v_token,
    'mitarbeiter', json_build_object('id', v_w.id, 'vorname', v_w.vorname, 'nachname', v_w.nachname));
end $$;

-- ── Daten für die Mitarbeiter-Ansicht ──
create or replace function public.ma_daten(p_token uuid) returns json
language plpgsql security definer set search_path = public as $$
declare v_id bigint := _ma_worker(p_token);
begin
  if v_id is null then return json_build_object('ok', false, 'abgelaufen', true); end if;
  return json_build_object(
    'ok', true,
    'mitarbeiter', (select json_build_object('id', w.id, 'vorname', w.data->>'vorname', 'nachname', w.data->>'nachname')
                    from workers w where w.id = v_id),
    'zeiten', coalesce((
      select json_agg(z.data order by z.data->>'datum' desc)
      from zeiten z
      where z.data->>'maId' = v_id::text
        and not exists (select 1 from deleted_ids d where d.id = 'zeiten:' || z.id)
        and z.id not between 1 and 7), '[]'::json),
    'projekte', coalesce((
      select json_agg(json_build_object('id', p.data->'id', 'name', p.data->>'name', 'ort', p.data->>'ort',
                                        'typ', p.data->>'typ', 'zeitGesperrt', p.data->'zeitGesperrt'))
      from projects p
      where p.id in (select _buchbare_projekte())
         or p.id::text in (select z.data->>'projId' from zeiten z where z.data->>'maId' = v_id::text)), '[]'::json)
  );
end $$;

-- ── Stunden speichern (nur für sich selbst) ──
create or replace function public.ma_zeit_speichern(p_token uuid, p_eintrag jsonb) returns json
language plpgsql security definer set search_path = public as $$
declare
  v_id    bigint := _ma_worker(p_token);
  v_art   text   := p_eintrag->>'art';
  v_datum text   := p_eintrag->>'datum';
  v_std   numeric;
  v_pause int;
  v_proj  bigint;
  v_zid   bigint;
  v_data  jsonb;
begin
  if v_id is null then return json_build_object('ok', false, 'abgelaufen', true); end if;

  if v_datum is null or v_datum !~ '^\d{4}-\d{2}-\d{2}$' then
    return json_build_object('ok', false, 'fehler', 'Ungültiges Datum');
  end if;
  if v_art is null or v_art not in ('arbeit', 'urlaub', 'feiertag', 'krank', 'urlaub-unbezahlt') then
    return json_build_object('ok', false, 'fehler', 'Ungültige Art');
  end if;
  begin
    v_std   := (p_eintrag->>'std')::numeric;
    v_pause := coalesce((p_eintrag->>'pause')::int, 0);
    v_proj  := nullif(p_eintrag->>'projId', '')::bigint;
  exception when others then
    return json_build_object('ok', false, 'fehler', 'Ungültige Zahl');
  end;
  if v_std is null or v_std < 0 or v_std > 24 or v_pause < 0 or v_pause > 1440 then
    return json_build_object('ok', false, 'fehler', 'Ungültige Stunden');
  end if;
  if v_art = 'arbeit' then
    if v_std <= 0 then return json_build_object('ok', false, 'fehler', 'Arbeitszeit muss größer als 0 sein'); end if;
    if v_proj is null or v_proj not in (select _buchbare_projekte()) then
      return json_build_object('ok', false, 'fehler', 'Baustelle nicht buchbar');
    end if;
  else
    v_proj := null;
  end if;

  -- Eindeutige ID (Millisekunden wie bisher in der App)
  v_zid := floor(extract(epoch from clock_timestamp()) * 1000)::bigint;
  while exists (select 1 from zeiten where id = v_zid) loop v_zid := v_zid + 1; end loop;

  v_data := jsonb_build_object(
    'id', v_zid, 'datum', v_datum, 'maId', v_id, 'projId', v_proj, 'std', v_std,
    'zeit', left(coalesce(p_eintrag->>'zeit', ''), 20), 'pause', v_pause, 'art', v_art,
    'notiz', left(coalesce(p_eintrag->>'notiz', ''), 500));

  insert into zeiten (id, data, updated_at) values (v_zid, v_data, now());
  return json_build_object('ok', true, 'eintrag', v_data);
end $$;

-- ── Eigenen Eintrag löschen ──
create or replace function public.ma_zeit_loeschen(p_token uuid, p_id bigint) returns json
language plpgsql security definer set search_path = public as $$
declare v_id bigint := _ma_worker(p_token);
begin
  if v_id is null then return json_build_object('ok', false, 'abgelaufen', true); end if;
  if not exists (select 1 from zeiten where id = p_id and data->>'maId' = v_id::text) then
    return json_build_object('ok', false, 'fehler', 'Eintrag nicht gefunden');
  end if;
  delete from zeiten where id = p_id;
  -- wie bisher: gelöschte ID merken, damit andere Geräte den Eintrag nicht wieder hochladen
  insert into deleted_ids (id, data, updated_at)
  values ('zeiten:' || p_id, jsonb_build_object('table', 'zeiten', 'id', p_id::text), now())
  on conflict (id) do nothing;
  return json_build_object('ok', true);
end $$;

-- ── Abmelden ──
create or replace function public.ma_logout(p_token uuid) returns json
language sql security definer set search_path = public as $$
  delete from ma_sessions where token = p_token;
  select json_build_object('ok', true);
$$;

-- ── Rechte: nur die fünf ma_-Funktionen sind von außen aufrufbar ──
revoke all on function public._client_ip() from public, anon, authenticated;
revoke all on function public._ma_worker(uuid) from public, anon, authenticated;
revoke all on function public._buchbare_projekte() from public, anon, authenticated;
revoke all on function public.ma_login(text) from public;
revoke all on function public.ma_daten(uuid) from public;
revoke all on function public.ma_zeit_speichern(uuid, jsonb) from public;
revoke all on function public.ma_zeit_loeschen(uuid, bigint) from public;
revoke all on function public.ma_logout(uuid) from public;
grant execute on function public.ma_login(text) to anon, authenticated;
grant execute on function public.ma_daten(uuid) to anon, authenticated;
grant execute on function public.ma_zeit_speichern(uuid, jsonb) to anon, authenticated;
grant execute on function public.ma_zeit_loeschen(uuid, bigint) to anon, authenticated;
grant execute on function public.ma_logout(uuid) to anon, authenticated;

commit;

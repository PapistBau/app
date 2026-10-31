-- Rechnungs-Sync Teil C (31.10.2026): Admin darf für manuell erfasste Sonderfälle eigene PDFs hochladen.
-- Nur in den Unterordner "manuell/" des privaten Speichers "eingangsrechnungen".
-- Status-Werte in eingangsrechnungen: 'neu' (aus Mail), 'manuell' (von Hand erfasst), 'geloescht' (ausgeblendet –
-- bleibt als Zeile erhalten, damit der tägliche Abgleich die Rechnung nicht erneut aus dem Postfach holt).
-- Rückweg: drop policy if exists eingangsrechnungen_admin_hochladen on storage.objects;

drop policy if exists eingangsrechnungen_admin_hochladen on storage.objects;
create policy eingangsrechnungen_admin_hochladen on storage.objects for insert to authenticated
  with check (bucket_id = 'eingangsrechnungen' and (storage.foldername(name))[1] = 'manuell' and public.is_admin());

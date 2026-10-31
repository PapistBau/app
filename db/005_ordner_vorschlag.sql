-- Rechnungs-Sync (31.10.2026): Ordner werden nie automatisch zugeordnet (Andys Vorgabe: nur bei 100 % Sicherheit → immer fragen).
-- Das Sync-Skript trägt jeden Ordner mit einem Vorschlag ein (quelle = 'auto', projekt_id = NULL).
-- Erst wenn Andy bestätigt (quelle = 'manuell', projekt_id gesetzt), werden Rechnungen des Ordners übernommen.
-- quelle = 'manuell' und projekt_id = NULL bedeutet: bewusst ignoriert.
-- Rückweg: alter table public.rechnungsordner_mapping drop column vorschlag_projekt_id;

alter table public.rechnungsordner_mapping
  add column if not exists vorschlag_projekt_id bigint references public.projects(id) on delete set null;

-- Eventuell bereits automatisch gesetzte Zuordnungen in Vorschläge umwandeln
update public.rechnungsordner_mapping
set vorschlag_projekt_id = projekt_id, projekt_id = null
where quelle = 'auto' and projekt_id is not null;

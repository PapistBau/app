<#
  Teil E: lädt Andys eigene Belege aus OneDrive nach Supabase hoch (Tabelle ausgangsbelege + privater Speicher).
  Die Ordner werden NUR GELESEN, nie verändert. Läuft als geplante Aufgabe auf Andys PC (eingerichtet mit belege_einrichten.ps1).

  Hochgeladen wird ab dem Jahr 2025 (Nummer …-25 und neuer):
    003 Rechnungen\Rechnung NNNN-JJ_…\Rechnung*.pdf              (ohne Entwürfe; Mahnungen, Lieferscheine usw. bleiben weg)
    002 Aufträge\01 Aufträge\AuftragsNr. NNNN-JJ_…\AuftragsNr*.pdf
  Ausgelesen und verrechnet wird danach im GitHub-Ablauf (scripts/ausgangsbelege.py).

  Schlüssel: eigener Supabase-Geheimschlüssel „belege-pc“, verschlüsselt nur für diesen Windows-Benutzer
  in %LOCALAPPDATA%\PapistBau\belege-schluessel.txt. Er steht nie im Klartext auf der Platte oder im Protokoll.

  Aufruf:  belege_hochladen.ps1           hochladen
           belege_hochladen.ps1 -Probe    nur auflisten, was hochgeladen würde (ohne Schlüssel, ohne Netz)
#>
param([switch]$Probe, [string]$Basis = (Join-Path $env:USERPROFILE 'OneDrive\PB'))

$ErrorActionPreference = 'Stop'
$SbUrl     = 'https://bthbicznechsnnowkxjz.supabase.co'
# Supabase lehnt Geheimschlüssel ab, wenn die Anfrage wie ein Browser aussieht – PowerShell 5.1 meldet sich sonst als „Mozilla/5.0 …“
$UA        = 'PapistBau-Belege/1.0'
$Bucket    = 'ausgangsbelege'
$Ablage    = Join-Path $env:LOCALAPPDATA 'PapistBau'
$Schluessel = Join-Path $Ablage 'belege-schluessel.txt'
$Protokoll = Join-Path $Ablage 'belege_hochladen.log'
$AbJahr    = 25

function Schreibe([string]$text) {
  $zeile = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $text
  if ($Probe) { Write-Output $zeile; return }
  if (-not (Test-Path $Ablage)) { New-Item -ItemType Directory -Path $Ablage | Out-Null }
  if ((Test-Path $Protokoll) -and (Get-Item $Protokoll).Length -gt 1MB) { Move-Item $Protokoll "$Protokoll.alt" -Force }
  Add-Content -Path $Protokoll -Value $zeile -Encoding UTF8
}

# ── Welche Dateien gehören dazu? ──
function Belege-Finden {
  $liste = @()
  $re = Join-Path $Basis '003 Rechnungen'
  foreach ($ordner in Get-ChildItem -LiteralPath $re -Directory -ErrorAction SilentlyContinue) {
    if ($ordner.Name -notmatch '^Rechnung\s+\d{4}-(\d{2})') { continue }
    if ([int]$Matches[1] -lt $AbJahr) { continue }
    foreach ($f in Get-ChildItem -LiteralPath $ordner.FullName -File -Filter '*.pdf') {
      if ($f.Name -notmatch '^Rechnung' -or $f.Name -match 'entwurf') { continue }
      $liste += [pscustomobject]@{ Quelle = 'rechnung'; Ordner = $ordner.Name; Datei = $f }
    }
  }
  $ab = Join-Path $Basis ("002 Auftr{0}ge\01 Auftr{0}ge" -f [char]0xE4)   # „Aufträge“ ohne Umlaut im Quelltext
  foreach ($ordner in Get-ChildItem -LiteralPath $ab -Directory -ErrorAction SilentlyContinue) {
    if ($ordner.Name -notmatch '^AuftragsNr[._\s]*\d{4}-(\d{2})') { continue }
    if ([int]$Matches[1] -lt $AbJahr) { continue }
    foreach ($f in Get-ChildItem -LiteralPath $ordner.FullName -File -Filter '*.pdf') {
      if ($f.Name -notmatch '^AuftragsNr' -or $f.Name -match 'entwurf') { continue }
      $liste += [pscustomobject]@{ Quelle = 'auftrag'; Ordner = $ordner.Name; Datei = $f }
    }
  }
  return $liste
}

# ── Supabase ──
function Lies-Schluessel {
  if (-not (Test-Path $Schluessel)) { throw 'Kein Schlüssel hinterlegt – bitte belege_einrichten.ps1 ausführen.' }
  $sicher = (Get-Content -LiteralPath $Schluessel -Raw).Trim() | ConvertTo-SecureString
  $p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sicher)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }
}

function Sb([string]$methode, [string]$pfad, $body = $null, [hashtable]$extra = @{}) {
  $h = @{ apikey = $script:Key; Authorization = "Bearer $script:Key" }
  foreach ($k in $extra.Keys) { $h[$k] = $extra[$k] }
  $arg = @{ Method = $methode; Uri = "$SbUrl$pfad"; Headers = $h; UserAgent = $UA; UseBasicParsing = $true; TimeoutSec = 120 }
  if ($null -ne $body) {
    # Windows PowerShell 5.1 schickt Texte sonst nicht als UTF-8 (Umlaute in Ordnernamen)
    $arg.Body = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 5 -Compress))
    $arg.ContentType = 'application/json; charset=utf-8'
  }
  $antwort = Invoke-WebRequest @arg
  if ($antwort.Content) {
    $text = if ($antwort.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($antwort.Content) } else { $antwort.Content }
    return ($text | ConvertFrom-Json)
  }
}

function Vorhandene-Belege {
  $alle = @(); $off = 0
  while ($true) {
    $seite = @(Sb 'GET' "/rest/v1/ausgangsbelege?select=id,datei_hash,ordner,dateiname&order=id&offset=$off&limit=1000")
    $alle += $seite
    if ($seite.Count -lt 1000) { return $alle }
    $off += 1000
  }
}

# ── Ablauf ──
$mutex = New-Object Threading.Mutex($false, 'Global\PapistBauBelegeHochladen')
if (-not $mutex.WaitOne(0)) { exit 0 }   # läuft schon
try {
  $belege = @(Belege-Finden)
  if (-not $belege.Count) { Schreibe "Keine Belege gefunden unter $Basis – OneDrive verbunden?"; exit 1 }

  if ($Probe) {
    $belege | Group-Object Quelle | ForEach-Object { Write-Output ('{0}: {1} PDFs in {2} Ordnern' -f $_.Name, $_.Count, ($_.Group.Ordner | Sort-Object -Unique).Count) }
    $belege | ForEach-Object { Write-Output ('  {0} | {1}' -f $_.Ordner, $_.Datei.Name) }
    exit 0
  }

  $script:Key = Lies-Schluessel
  $vorhanden = @{}
  foreach ($v in Vorhandene-Belege) { $vorhanden[$v.datei_hash] = $v }

  $neu = 0; $umbenannt = 0; $fehler = 0
  foreach ($b in $belege) {
    try {
      $hash = (Get-FileHash -LiteralPath $b.Datei.FullName -Algorithm SHA256).Hash.ToLower()
      if ($vorhanden.ContainsKey($hash)) {
        $v = $vorhanden[$hash]
        if ($v.ordner -ne $b.Ordner -or $v.dateiname -ne $b.Datei.Name) {   # Ordner oder Datei umbenannt
          Sb 'PATCH' "/rest/v1/ausgangsbelege?id=eq.$($v.id)" @{ ordner = $b.Ordner; dateiname = $b.Datei.Name; ausgelesen_am = $null } | Out-Null
          $umbenannt++
        }
        continue
      }
      $pfad = "$($b.Quelle)/$hash.pdf"
      Invoke-WebRequest -Method Post -Uri "$SbUrl/storage/v1/object/$Bucket/$pfad" -InFile $b.Datei.FullName -ContentType 'application/pdf' `
        -Headers @{ apikey = $script:Key; Authorization = "Bearer $script:Key"; 'x-upsert' = 'true' } -UserAgent $UA -UseBasicParsing -TimeoutSec 300 | Out-Null
      Sb 'POST' '/rest/v1/ausgangsbelege' @{
        quelle = $b.Quelle; ordner = $b.Ordner; dateiname = $b.Datei.Name; datei_hash = $hash
        groesse_bytes = $b.Datei.Length; datei_geaendert_am = $b.Datei.LastWriteTimeUtc.ToString('o'); storage_pfad = $pfad
      } @{ Prefer = 'return=minimal' } | Out-Null
      $vorhanden[$hash] = [pscustomobject]@{ ordner = $b.Ordner; dateiname = $b.Datei.Name }
      $neu++
    } catch {
      $fehler++
      Schreibe ("FEHLER bei {0}\{1}: {2}" -f $b.Ordner, $b.Datei.Name, $_.Exception.Message)
    }
  }
  Schreibe ("{0} Belege geprüft · {1} neu hochgeladen · {2} umbenannt · {3} Fehler" -f $belege.Count, $neu, $umbenannt, $fehler)

  # Auslesen gleich anstoßen statt bis zum nächsten stündlichen Lauf zu warten (nur wenn GitHub-Programm angemeldet ist)
  if (($neu -or $umbenannt) -and (Get-Command gh -ErrorAction SilentlyContinue)) {
    try { & gh workflow run belege.yml -R PapistBau/app 2>$null | Out-Null; Schreibe 'Auslesen bei GitHub angestoßen' } catch { }
  }
  if ($fehler) { exit 1 }
} catch {
  Schreibe ("FEHLER: {0}" -f $_.Exception.Message)
  exit 1
} finally {
  $mutex.ReleaseMutex()
}

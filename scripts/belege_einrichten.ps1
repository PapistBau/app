<#
  Teil E – einmalige Einrichtung auf Andys PC:
   1. Fenster für den Supabase-Geheimschlüssel „belege-pc“ (Eingabe verdeckt; nie über Chat oder Terminal)
   2. Schlüssel prüfen und verschlüsselt nur für diesen Windows-Benutzer speichern (%LOCALAPPDATA%\PapistBau)
   3. Geplante Aufgabe „Papist Bau – Belege hochladen“: bei der Anmeldung und täglich 12:30 + 18:30 Uhr
   4. Ersten Lauf starten
  Aufruf: powershell -NoProfile -ExecutionPolicy Bypass -STA -File belege_einrichten.ps1
  Nicht direkt aus der Claude-App starten: deren Unterprozesse schreiben AppData in einen eigenen Bereich
  (…\Packages\Claude_…\LocalCache), den die geplante Aufgabe nicht sieht. Stattdessen per Doppelklick/Explorer
  oder über eine einmalige geplante Aufgabe starten.
#>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$SbUrl  = 'https://bthbicznechsnnowkxjz.supabase.co'
$Ablage = Join-Path $env:LOCALAPPDATA 'PapistBau'
$Datei  = Join-Path $Ablage 'belege-schluessel.txt'
$Skript = Join-Path $PSScriptRoot 'belege_hochladen.ps1'
$Aufgabe = 'Papist Bau - Belege hochladen'

function Meldung($text, $titel = 'Papist Bau', $art = 'Information') {
  [void][Windows.Forms.MessageBox]::Show($text, $titel, 'OK', $art)
}

# ── 1. Schlüssel abfragen ──
$f = New-Object Windows.Forms.Form
$f.Text = 'Papist Bau – Belege hochladen einrichten'; $f.Size = New-Object Drawing.Size(560, 230)
$f.StartPosition = 'CenterScreen'; $f.TopMost = $true; $f.FormBorderStyle = 'FixedDialog'; $f.MaximizeBox = $false
$l = New-Object Windows.Forms.Label
$l.Text = "Bitte den Supabase-Geheimschlüssel 'belege-pc' einfügen (Strg+V).`nEr wird nur verschlüsselt auf diesem PC gespeichert."
$l.Location = New-Object Drawing.Point(15, 15); $l.Size = New-Object Drawing.Size(520, 40)
$t = New-Object Windows.Forms.TextBox
$t.UseSystemPasswordChar = $true; $t.Location = New-Object Drawing.Point(15, 65); $t.Size = New-Object Drawing.Size(515, 25)
$ok = New-Object Windows.Forms.Button; $ok.Text = 'Speichern'; $ok.Location = New-Object Drawing.Point(330, 120); $ok.Size = New-Object Drawing.Size(95, 30)
$ok.DialogResult = 'OK'
$ab = New-Object Windows.Forms.Button; $ab.Text = 'Abbrechen'; $ab.Location = New-Object Drawing.Point(435, 120); $ab.Size = New-Object Drawing.Size(95, 30)
$ab.DialogResult = 'Cancel'
$f.Controls.AddRange(@($l, $t, $ok, $ab)); $f.AcceptButton = $ok; $f.CancelButton = $ab
if ($f.ShowDialog() -ne 'OK' -or -not $t.Text.Trim()) { Write-Output 'Abgebrochen – nichts geändert.'; exit 1 }
# Leerzeichen, Zeilenumbrüche, unsichtbare Zeichen und Anführungszeichen vom Kopieren entfernen
$key = $t.Text -replace '[\s\p{Cf}"'']', ''; $t.Text = ''
if ($key -notmatch '^sb_secret_[A-Za-z0-9_-]+$') {
  Meldung 'Das ist kein vollständiger Geheimschlüssel. Er muss mit sb_secret_ beginnen und darf keine Punkte (verdeckte Stellen) enthalten. Bitte in Supabase beim Schlüssel belege-pc auf das Kopieren-Symbol klicken und erneut einfügen.' 'Schlüssel unvollständig' 'Warning'
  Write-Output 'Schlüssel unvollständig – nichts gespeichert.'; exit 1
}

# ── 2. Prüfen und verschlüsselt speichern ──
# Eigene Kennung statt PowerShell-Standard (Mozilla/5.0 …), sonst hält Supabase die Anfrage für einen Browser und lehnt ab
$UA = 'PapistBau-Belege/1.0'
try {
  Invoke-WebRequest -Uri "$SbUrl/rest/v1/ausgangsbelege?select=id&limit=1" -Headers @{ apikey = $key; Authorization = "Bearer $key" } `
    -UserAgent $UA -UseBasicParsing -TimeoutSec 30 | Out-Null
} catch {
  $grund = $_.ErrorDetails.Message
  if (-not $grund) { $grund = $_.Exception.Message }
  Meldung "Der Schlüssel wird von Supabase nicht angenommen.`n`nAntwort von Supabase: $grund" 'Schlüssel abgelehnt' 'Warning'
  Write-Output 'Schlüssel abgelehnt – nichts gespeichert.'; exit 1
}
if (-not (Test-Path $Ablage)) { New-Item -ItemType Directory -Path $Ablage | Out-Null }
ConvertTo-SecureString $key -AsPlainText -Force | ConvertFrom-SecureString | Set-Content -LiteralPath $Datei -Encoding ASCII -NoNewline
$key = $null
Write-Output 'Schlüssel geprüft und verschlüsselt gespeichert.'

# ── 3. Geplante Aufgabe (ohne sichtbares Fenster) ──
$arg = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Skript`""
$aktion = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $arg
$anmeldung = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$anmeldung.Delay = 'PT10M'
$ausloeser = @($anmeldung, (New-ScheduledTaskTrigger -Daily -At '12:30'), (New-ScheduledTaskTrigger -Daily -At '18:30'))
$einst = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $Aufgabe -Action $aktion -Trigger $ausloeser -Settings $einst `
  -Description 'Lädt neue eigene Rechnungen und Auftragsbestätigungen aus OneDrive in die Papist-Bau-App (nur lesen).' -Force | Out-Null
Write-Output "Geplante Aufgabe '$Aufgabe' eingerichtet."

# ── 4. Erster Lauf ──
Start-ScheduledTask -TaskName $Aufgabe
Write-Output 'Erster Lauf gestartet (Protokoll: %LOCALAPPDATA%\PapistBau\belege_hochladen.log).'
Meldung "Fertig eingerichtet.`n`nDie Belege werden jetzt hochgeladen; danach täglich um 12:30 und 18:30 Uhr sowie bei der Anmeldung."

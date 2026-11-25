# Unternehmensrecherche

Das Programm sammelt Unternehmensinformationen aus fünf Quellen. Alle Befehle
werden über `main.py` gestartet.

| Gruppe | Quellen |
| --- | --- |
| `primary` – primär | Unternehmensregister, Kununu, Gewerkschaften, Unternehmenskarriereseiten |
| `secondary` – sekundär | Gespeicherte Implisense-Nachrichten |

## 1. Einrichten

Voraussetzung: Python ab Version 3.11 mit `pip` und Internetzugang.
Den entpackten Projektordner in PowerShell öffnen und einmal ausführen:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Die Browser-Sammler verwenden Playwright-Chromium. Wenn es unter Windows nicht
installiert ist, wird ein vorhandener Edge oder Chrome automatisch ausgewählt.
Mit `--browser-channel msedge` bzw. `--browser-channel chrome` lässt sich der
Browser ausdrücklich festlegen.

Die folgenden Befehle verwenden diese Umgebung direkt; eine Aktivierung ist
nicht nötig. Unter macOS/Linux stattdessen `python3 -m venv .venv` und
`.venv/bin/python` verwenden. Falls nötig, die Browser-Systempakete mit
`.venv/bin/python -m playwright install --with-deps chromium` installieren.

Für Kununu die Datei `.env.example` nach `.env` kopieren und den eigenen
Zyte-API-Schlüssel eintragen. Für Implisense ist eine eigene Anmeldung nötig.

## 2. Firmenliste bearbeiten

Alle Suchen verwenden standardmäßig **`data/sources.csv`**.

| Spalte | Bedeutung |
| --- | --- |
| `company_key` | Firmenname bzw. eindeutiger Firmenschlüssel; erforderlich |
| `company_name`, `company_location`, `company_postal_code` | Firmenname, Ort und PLZ für den Implisense-Abgleich |
| `url` | Offizielle Firmenwebsite; Startpunkt der Karrieresuche und Website für den Implisense-Import |
| `career_url` | Optionale direkte Karriereadresse, Bewerberportal oder XML-Stellensitemap |
| `kununu_url` | Öffentliches Arbeitgeberprofil; ohne Adresse wird Kununu übersprungen |
| `enabled` | `true` oder leer: bearbeiten; `false`: überspringen |
| `Branche` | Branche wie in den Quellenlisten; mehrere mit `;` trennen |

CSV-Felder mit Semikolon als Inhalt in Anführungszeichen setzen. Komma-,
Semikolon- und Tabulatortrennung sowie UTF-8 und Windows-1252 werden gelesen.
Die Eingabedateien werden nicht verändert. Auswahl ohne Netzwerk prüfen:

```powershell
.\.venv\Scripts\python.exe main.py primary --list-companies
```

## 3. Recherche starten

```powershell
# Vier primäre Quellen für alle aktiven Firmen, einschließlich Registerdownloads
.\.venv\Scripts\python.exe main.py primary --download --zyte-max-requests 100 --career-browser-fallback --gewerkschaften-browser-fallback

# Sekundäre Quellen und gespeicherte Implisense-Nachrichten
.\.venv\Scripts\python.exe main.py secondary

# Stellen direkt auf den Websites der Firmen und verlinkten Arbeitgeberportalen
.\.venv\Scripts\python.exe main.py karriere

# Nur eine Firma; Startadressen ohne Netzwerk prüfen
.\.venv\Scripts\python.exe main.py karriere --company-key "mediCAD" --list-career-sites

# Nur Kununu: höchstens 15 Bewertungen je Firma aus den letzten zwei Jahren
.\.venv\Scripts\python.exe main.py kununu --kununu-reviews 15 --zyte-max-requests 100

# Alternativ lokal ohne Zyte: einmal anmelden, danach automatischer Firmenlauf
.\.venv\Scripts\python.exe main.py kununu-browser --login
# Folgeläufe mit gespeicherter Sitzung, ohne Bestätigung je Firma
.\.venv\Scripts\python.exe main.py kununu-browser
```

`primary` startet Register, Kununu über Zyte, Gewerkschaften und Karriereseiten.
`research` beziehungsweise `--source all` bezeichnen ebenfalls diese vier Quellen;
Implisense wird damit nicht gestartet. Alle aktiven Firmen aus `data/sources.csv`
werden ausgewählt, die einzelnen Sammler behalten ihre Seiten-/Trefferlimits.
Die obige Zyte-Grenze erlaubt höchstens 100 kostenpflichtige API-Aufrufe im Lauf.
Die Befehle nacheinander in PowerShell ausführen, nicht mit `&&` verbinden:
Auch ein erfolgreicher Teillauf kann den Exitcode 1 liefern.

Die Karrieresuche folgt den Links der Firmenwebsite auch auf externe
Arbeitgeberportale. Für Personio und Greenhouse werden öffentliche Stellenfeeds
automatisch erkannt; sonst liest der Sammler strukturierte Daten und
HTML-Stellenseiten. Eine bekannte direkte Adresse kann in `career_url` stehen.
Nicht auslesbare Portale und erreichte Limits werden als unvollständig markiert.

**Zyte ist kostenpflichtig.** `--zyte-max-requests` begrenzt die API-Aufrufe für
den gesamten Lauf, nicht je Firma und nicht als Geldbetrag. Mit Bewertungen
beginnt die Sammlung direkt auf der Bewertungsseite (mindestens ein Aufruf je Profil); zusätzliche
Bewertungsseiten brauchen weitere Aufrufe. Bei erreichtem Limit können
Ergebnisse unvollständig bleiben. `--kununu-reviews 0` lädt nur das Profil.
Fehlgeschlagene Anfragen werden nicht automatisch wiederholt.

Für eine einzelne Firma `--company-key "mediCAD"` ergänzen; den Wert durch einen
Schlüssel aus der eigenen CSV ersetzen. `--sources DATEI.csv` wählt eine andere
Firmenliste. Diese Firmenfilter wählen **keine Implisense-Listen** aus.

Einzelne Quellen lassen sich mit `unternehmensregister`, `gewerkschaften`,
oder `karriere` starten. Die Artikelsuchen berücksichtigen standardmäßig sechs
Kalendermonate; beispielsweise `--article-months 12` für zwölf Monate.
Passende Seiten vorab ohne Netzwerk prüfen:

```powershell
.\.venv\Scripts\python.exe main.py gewerkschaften --list-gewerkschaften
```

Für GEW und DJV kann bei HTTP 403/500 ein Browserabruf zugeschaltet werden:
`python main.py gewerkschaften --gewerkschaften-browser-fallback`.
Der Browser läuft unsichtbar mit einem eigenen temporären Profil. Sicherheitsprüfungen
werden als Fehler gemeldet; Einzelheiten stehen in `browser-attempts.json`.

## Implisense anmelden und Firmen importieren

```powershell
# Im geöffneten Browser mit dem eigenen Konto anmelden
.\.venv\Scripts\python.exe main.py implisense login

# Aktive Firmen aus sources.csv einer neuen Liste zuordnen
.\.venv\Scripts\python.exe main.py implisense import --headless

# Bereits vorhandene Nachrichten herunterladen
.\.venv\Scripts\python.exe main.py implisense news --headless
```

Wurde `output/` vollständig geleert, ist auch die lokale Implisense-Anmeldung
entfernt: zuerst wieder `login` ausführen. Bei einer geänderten Firmenliste
`import` erneut ausführen. Firmenlisten im Implisense-Konto werden durch die
lokale Dateibereinigung nicht gelöscht.

`secondary` liest gespeicherte Nachrichten aller vorhandenen Listen. Eine Liste
mit `--implisense-list-id UUID` auswählen; den Zeitraum mit
`--implisense-period 7`, `30` oder `90` festlegen (Standard: 90 Tage).
Ohne gespeicherte Recherche wird kein neuer kostenpflichtiger Suchlauf gestartet.
Neue Recherche mit ausdrücklichem Kostenlimit: [Befehlsreferenz](docs/reference.md).

## Ergebnisse und Hilfe

Karriereseiten lassen sich einzeln mit `python main.py karriere` sammeln.
Standard sind 150 Seiten und 300 Stellen je Firma; das HTTP-Budget wird auf die
verbleibenden Firmen verteilt. `--career-browser-fallback` aktiviert bei HTTP
403/500 einen zusätzlichen Browserabruf. Personio, Greenhouse und Ashby werden
über ihre veröffentlichten Stellenfeeds gelesen, wenn die Firmenwebsite sie verlinkt.

Die Ergebnisse stehen unter **`output/`**, getrennt nach Quelle und Firma.
`output/research/batch.json` enthält die Übersicht des letzten gemeinsamen Laufs.
Texte und Originaldateien bleiben in den jeweiligen Quellenordnern.

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py primary --help
```

Weitere Optionen: [Befehlsreferenz](docs/reference.md).
Arbeitsweise und Grenzen: [Suchverfahren](docs/search-workflows.md).

API-Schlüssel und Browseranmeldung werden auf jedem Rechner neu eingerichtet.
Das Weitergabepaket enthält keine persönliche `.env`, Anmeldedaten, Tests,
alten Ergebnisse oder Caches. Die benötigten Firmen- und Quellenlisten liegen
unter `data/`.

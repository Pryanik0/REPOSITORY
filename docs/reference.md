# Befehlsreferenz

Installation und Beispiele: [Projektanleitung](../README.md).
`python` bezeichnet hier den Interpreter der eingerichteten Umgebung, unter
Windows also `.\.venv\Scripts\python.exe`. Alle Standardpfade und relativen
Dateiangaben beziehen sich beim Start über `main.py` auf den Projektordner.
Absolute Pfade sind ebenfalls möglich.

## Befehle

| Befehl | Umfang |
| --- | --- |
| `primary` | Unternehmensregister, Kununu, Gewerkschaften, Karriereseiten |
| `secondary` | Gespeicherte Implisense-Nachrichten |
| `kununu` | Arbeitgeberprofil und höchstens 15 Mitarbeiterbewertungen |
| `kununu-browser` | Automatische Kununu-Sammlung im lokalen Browser mit gespeicherter Sitzung |
| `unternehmensregister` | Veröffentlichungen, optional Originaldownloads |
| `gewerkschaften` | Unternehmensartikel auf Gewerkschaftsseiten |
| `karriere` | Stellen auf offiziellen Firmenwebsites und verlinkten Arbeitgeberportalen |
| `implisense` | Anmeldung, Firmenlisten und Nachrichten |
| `documents` | Gespeicherte Registerdokumente aufbereiten |
| `output-contract` | Liefer-JSONs nach der PDF-Feldreferenz v0.4 prüfen oder mit Sicherung normalisieren |
| `research` | Gemeinsamer Lauf: Register, Kununu, Gewerkschaften und Karriereseiten |

`python main.py BEFEHL --help` zeigt alle Optionen. Befehls- und Optionsnamen
sowie JSON-Feldnamen bleiben für die technische Verarbeitung unverändert.

## Firmen auswählen

Standard: `data/sources.csv`. `--sources PFAD` wählt eine andere CSV;
`--company-key SCHLÜSSEL` beschränkt sie auf eine Firma.
`--company "Firmenname"` sucht ohne Firmenliste; `--location "Ort"` ergänzt
diese Einzelsuche im Register. `--list-companies` zeigt die Auswahl ohne Netzwerk.

Zusätzliche CSV-Spalten: `company_name` für den Suchnamen,
`company_location` für den Ort, `company_postal_code` für die PLZ und `Suchbegriffe` oder `search_terms` für
Aliasnamen, getrennt durch `;` oder `|`. Ohne `company_name` gilt der führende
Teil von `name`, sonst `company_key`. Wiederholte Schlüssel werden vereinigt;
widersprüchliche Namen, Orte und Kununu-Profile werden abgelehnt.

Branchenbezeichnungen werden in allen Eingaben nach dem gemeinsamen
[Branchenverzeichnis](branches.md) normalisiert.

Alternativ ist für Register, Kununu und Artikelsuchen `--companies DATEI.xlsx`
möglich. Nur Blätter mit `Unternehmen` und `Branche` liefern Firmen. Exakte
Namenstreffer in der CSV ergänzen Profile, Orte und Aliasnamen. Das optionale
Blatt `Quellennachweise` mit `Unternehmen`, `Quelle` und `URL` ergänzt Startlinks
für passende Gewerkschaften. Der Karriere-Sammler durchsucht Firmenwebsites gezielt
nach Stellen; andere Sammler durchsuchen diese Websites nicht allgemein.

## Unternehmenskarriereseiten

```powershell
python main.py karriere
python main.py karriere --company-key "mediCAD"
python main.py karriere --company "Beispielfirma" --career-url "https://firma.example/karriere"
python main.py karriere --list-career-sites
python main.py karriere --career-keyword "Data Engineer" --career-keyword "Transformation Manager"
```

Die Suche startet bei `career_url` und `url` aus der Firmenliste. Ohne beide
Adressen wird die Firma mit `missing_company_url` übersprungen. Es gibt keine
Suchmaschinensuche nach unbekannten Firmendomains. Eine optionale CSV-Spalte
`career_url` kann auf die Karriereseite oder das Arbeitgeberportal zeigen;
wiederholte Firmenzeilen erlauben mehrere Adressen. Excel kann `url` und
`career_url` ebenfalls enthalten. Passende CSV-Zeilen ergänzen diese Adressen.
`--career-url URL` setzt den Karriere-Startlink für genau eine ausgewählte Firma.

Verfolgt werden Karriere- und Stellenlinks, HTML-Paginierung sowie eingebettete
bekannte Bewerberportale. Neue Portaldomains benötigen einen Link, eine
HTTP-Weiterleitung oder einen veröffentlichten Meta-Refresh auf einer bereits
zugeordneten Seite. Auch mehrstufige Domainwechsel werden verfolgt; robots.txt
wird am jeweiligen Ziel erneut geprüft. Ein Portal darf also z. B. bei Personio oder
SmartRecruiters liegen, wenn die Firma es selbst verlinkt. Allgemeine Jobbörsen
wie StepStone, Indeed und LinkedIn werden ausgeschlossen. `sources` im Snapshot
belegt konfigurierte und entdeckte Portale einschließlich ihrer Herkunftsseite
und des Mechanismus (`employer_link`, `http_redirect`, `meta_refresh`,
`robots_redirect` für eine kanonische robots.txt-Domain).
`pages[].redirects` enthält HTTP-Weiterleitungsketten, auch bei Cachetreffern.
Das ist keine unabhängige rechtliche Identitätsprüfung.

Für erkannte Personio-Portale, auch direkte `/job/<ID>`-Links, wird der öffentliche XML-Stellenfeed gelesen;
bei Greenhouse und Ashby die öffentlichen Job Board APIs inklusive Beschreibungen.
Greenhouse-Iframes und das eingebettete Job-Board-Skript werden ebenfalls
erkannt. Auch veröffentlichte `fetch`-Adressen in eingebetteten Skripten werden
gelesen, einschließlich einfacher Stringkonstanten für Ashby. JavaScript wird dabei
nicht ausgeführt. Das Portal muss konfiguriert oder von der Firmenwebsite verlinkt
sein; Firmenkennungen werden nicht aus Namen geraten. Gemeinsame Greenhouse-/Ashby-
Hosts und API-Pfade bleiben auf den zugeordneten Arbeitgeber beschränkt.
Ist der Feed deaktiviert, fehlerhaft oder gesperrt, wird die HTML-Seite versucht.
`adapter_attempts` dokumentiert Erfolg oder Fallback; `sources` belegt den
abgeleiteten Feed mit `mechanism=public_job_feed`. `pages[].portal_redirects`
bewahrt die HTTP-Kette zum Portal vor einem Feedabruf.
Feedabrufe und robots.txt zählen zum gemeinsamen HTTP-Limit; ein Portal mit
Feedversuch und HTML-Fallback zählt als ein Seitenversuch.
Die ausdrücklich öffentliche [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api)
wird direkt angesprochen: `/robots.txt` auf der allgemeinen API-Domain benötigt
Authentifizierung, der dokumentierte Stellen-Endpunkt nicht. Diese Ausnahme gilt
nur für `https://api.ashbyhq.com/posting-api/job-board/<Kennung>`; Domain-, Mandanten-,
Cache- und Budgetprüfungen bleiben aktiv.

| Option | Standard |
| --- | --- |
| `--career-max-pages 150` | Seitenversuche je Firma |
| `--career-max-jobs 300` | Erkannte Stellen je Firma vor Stichwortfilter |
| `--career-max-requests N` | HTTP-Versuche für alle Firmen zusammen, einschließlich robots.txt und Weiterleitungen. Standard: Anzahl der Firmen × (2 × Seitenlimit + 10) |
| `--career-cache-only` | Nur aktuelle Cacheantworten auswerten; keine Netzwerkanfragen |
| `--career-browser-fallback` | Bei HTTP 403/500 zusätzlich HTML im versteckten Browser laden |
| `--career-keyword TEXT` | Optionaler ODER-Filter auf Stellentitel und Beschreibung; wiederholbar |
| `--list-career-sites` | Startlinks ohne Netzwerk und Dateiausgabe anzeigen |

Das verbleibende HTTP-Budget wird durch die Anzahl der noch zu prüfenden Firmen
geteilt. Eine Firma kann den Anteil späterer Firmen nicht verbrauchen; ungenutzte
Anfragen werden weitergegeben. `coverage.http` nennt je Firma Zuteilung, Verbrauch
und Cachetreffer. Ein sehr kleines ausdrücklich gesetztes Gesamtbudget kann
weiterhin für einzelne Firmen nicht reichen. Erfolgreiche Personio-/Ashby-Feeds ersetzen
weitere Abrufe der einzelnen Stellen desselben Portals.
Sprach- und Sortiervarianten von Suchlisten erzeugen keine zusätzlichen Schleifen;
Paginierung und Ortsfilter bleiben erhalten. Bekannte Stellen-IDs verhindern
Doppelzählungen übersetzter rexx-/SuccessFactors-Angebote. Karriereveranstaltungen
und Social-Media-Links werden nicht als Arbeitgeberportale durchsucht.

Der optionale Browser-Fallback verwendet ein isoliertes Profil ohne Benutzer-Cookies
und ohne JavaScript. Er hilft bei abweichender HTTP-Behandlung, z. B. auf der Recor-
Firmenwebsite; dynamische Portale benötigen weiterhin auslesbare Daten. robots.txt,
Arbeitgeberscope und Anfragebudget gelten weiter. CAPTCHA und HTTP 429 werden nicht
automatisch wiederholt. `browser-attempts.json` dokumentiert die Versuche.

Eine als `career_url` eingetragene `sitemap.xml` liefert direkte Stellendetails.
`career_sitemap_locations` begrenzt die Sitemap optional auf Ortspräfixe in
den Stellen-URLs. Bei Daiichi Sankyo sind München und Pfaffenhofen an der Ilm
konfiguriert. `coverage.sitemap_locations` nennt die verwendeten Ortspräfixe.

Die Ausgabe steht unter `<output>/karriere/<Firma-ID>/`: `snapshot.json`, eine
lesbare `README.md` sowie `jobs/<ID>/job.json` und `job.txt`. Erfasst werden
Titel, Beschreibung, direkte URL und, falls vorhanden, Standort, Arbeitgeber,
Arbeitszeitart, Referenznummer und Datumsangaben. `availability=advertised`
bedeutet auf der geprüften Website veröffentlicht, keine Garantie der Besetzbarkeit.
Abgelaufene strukturierte Angebote und ausdrücklich geschlossene Stellen werden
ausgeschlossen. Initiativbewerbungen, Jobagenten und Talentpools zählen ebenfalls
nicht als konkrete offene Stellen. Nur die Stellen in der aktuellen Übersicht gehören zum letzten
Lauf; ältere Dateien können erhalten bleiben.

JSON-LD-Stellenlisten, einzelne HTML-Stellenseiten sowie die unterstützten
Stellenfeeds werden gelesen. Ein gültiger leerer Feed belegt eine leere Liste;
ungültige Antworten tun das nicht. Greenhouse-Prospect-Posts zählen als
allgemeine Bewerbungen. Erstellungs- oder Änderungsdaten aus Feeds werden
nicht als Veröffentlichungsdatum ausgegeben. JavaScript wird nicht ausgeführt;
App-Seiten ohne unterstützten Feed, Nachladebuttons, robots-Sperren und
Abruffehler ergeben einen unvollständigen Suchstatus. `partial` mit null Stellen
ist kein Nachweis fehlender offener Stellen. `coverage` beschreibt die geprüften
Links und Limits, `pending_urls` die wegen Limits nicht besuchten Seiten.
`no_results` setzt eine auswertbare leere/abgelaufene Suche oder einen leeren
Stichwortfilter voraus. Die vollständige Anzahl aller offenen Stellen kann
auch bei `ok` nicht garantiert werden. Cache und robots-Regeln entsprechen
den Artikelsammlern; die Budgets sind getrennt.

Ein von der Firma verlinktes Konzernportal kann auch Stellen anderer
Konzerngesellschaften enthalten. Der veröffentlichte Arbeitgeber wird in
`employer` und in der Stellenübersicht erhalten; diese Angebote werden nicht
automatisch allein anhand eines abweichenden Firmennamens verworfen.
Die Trefferzahl beschreibt Angebote auf den geprüften Seiten, nicht zwingend
die Zahl der Stellen der einzelnen juristischen Person.

## Unternehmensregister

| Option | Bedeutung / Standard |
| --- | --- |
| `--years 2` | Ab 1. Januar zwei Jahre vor dem laufenden Berliner Jahr bis heute |
| `--category both` | Alternativ `finanzberichte` oder `bekanntmachungen` |
| `--max-pages 1` | Seiten je Suchbereich und Firma |
| `--max-publications 30` | Veröffentlichungen insgesamt je Firma |
| `--download` | Dokumente zusätzlich zur Ergebnisliste laden |
| `--max-reports 3` | Downloadlimit je Kategorie und Firma |
| `--report-index N` | Nur Ergebnis N herunterladen; benötigt `--download` |
| `--open N` | Ergebnis N sichtbar öffnen; nur eine Firma |
| `--browser-channel chrome` | Installierten Chrome verwenden; `msedge` für Edge |

Ohne Browseroption wird Playwright-Chromium verwendet, unter Windows ersatzweise
ein installierter Edge oder Chrome. Zusätzliche CAPTCHAs
können den automatischen Download verhindern.

Die Zusammenfassung enthält `coverage`: ungelesene Suchseiten, das
Publikationslimit, ausgewählte/erfolgreiche Downloads und die Textvollständigkeit.
`complete` setzt eine vollständig gelesene Suche und alle gespeicherten
Publikationen mit Text voraus. Erreichte Such-/Downloadlimits ergeben `partial`,
fehlgeschlagene Downloads `incomplete`, eine vollständige leere Suche `no_results`.
Das bestätigt weiterhin nicht die rechtliche Identität eines Unternehmenstreffers.
Auch kurze Mitteilungen werden gespeichert, wenn der Veröffentlichungsbereich
geladen ist. Bei Extraktionsfehlern verweist `diagnostic_html_path` auf das HTML.

## Kununu

`ZYTE_API_KEY` wird aus der Umgebung oder `.env` gelesen. `kununu_url` muss auf
ein öffentliches Arbeitgeberprofil zeigen. Ohne URL wird die Firma übersprungen;
es gibt keine Profilsuche. Für eine einzelne Firma ist `--kununu-url URL` möglich.

`--zyte-max-requests N` ist ein verpflichtendes Aufruflimit für den ganzen Lauf,
kein Geldlimit. Profil, jede Bewertungsseite und Fehlversuche zählen mit.
Mit Bewertungen beginnt der Abruf direkt auf der Bewertungsseite, die auch
Profilkennzahlen enthält: mindestens ein Aufruf je Firma; weitere Seiten
brauchen zusätzliche Aufrufe. `--kununu-reviews` erlaubt 0 bis 15, standardmäßig
15; 0 lädt nur das Profil. Bewertungen müssen aus den letzten zwei Jahren sein.

`--kununu-timeout 60` bestimmt das Zeitlimit je Anfrage. Optional:
`--zyte-country DE` und `--zyte-ip-type auto|datacenter|residential`.
Diese Optionen können Verfügbarkeit und Kosten beim Anbieter beeinflussen.
Das Programm wiederholt fehlgeschlagene Anfragen nicht automatisch.

Für weitere Bewertungen nutzt es vorhandene Navigationslinks oder den sichtbaren
Button „Mehr Bewertungen lesen“ über Zyte-Browseraktionen. Neue sichtbare Karten
werden auch dann gelesen, wenn die eingebetteten Startdaten unverändert bleiben.
Pro Firma werden höchstens fünf Bewertungsabrufe ausgeführt; jeder zählt zum
angegebenen Gesamtlimit. Zyte-Button-Abrufe beginnen frisch und wiederholen die
bisherigen Klicks. Aktionen können den Aufwand und die Kosten je Aufruf erhöhen.
`review_stop_reason` und die Seitendiagnose unterscheiden Stichprobenlimit,
fehlenden Fortschritt und fehlgeschlagene Aktionen. Das Login-Gate für weitere
Bewertungen wird als `reviews_login_required` gemeldet, wenn es tatsächlich im
HTML erscheint; ein Feature-Flag allein reicht nicht. Verfügbare Bewertungen
bleiben erhalten. Unscharfe Beispielkarten
des Gates werden ausgeschlossen. Bei CAPTCHA, 404/410 oder einem
Profilwechsel erfolgt kein zusätzlicher Bewertungsabruf; ein anderes Zielprofil
wird zur Prüfung protokolliert und nicht automatisch übernommen.
Browseraktionen folgen der [Zyte-Dokumentation](https://docs.zyte.com/zyte-api/usage/browser.html).

Beim erneuten Speichern bleibt der vorherige `snapshot.json` unverändert unter
`kununu/<Firma>/history/<SHA256>.json` erhalten. `previous_snapshot_path` verweist
darauf. Historische Bewertungen werden nicht als frisch abgerufen ausgegeben;
die HTML-Diagnosedateien im Ordner `zyte` gehören weiterhin zum letzten Abruf
der jeweiligen Seite. Ein vollständig neuer Ausgabeordner benötigt weiterhin
ein separates Archiv, um frühere Ergebnisse aufzubewahren.

### Lokaler Browser ohne Zyte

Einmalig ein sichtbares Fenster öffnen, anmelden und in der Konsole Enter drücken:

```powershell
python main.py kununu-browser --login
```

Danach startet bereits die automatische Sammlung für alle aktiven Firmen mit
Profiladresse. Weitere Läufe verwenden dieselbe Sitzung:

```powershell
python main.py kununu-browser
```

Es gibt keine Bestätigungen je Firma. Der Browser klickt auf vorhandene
Nachladebuttons und schließt das bekannte Puls-Hinweisfenster automatisch.
Ein eingeblendeter Usercentrics-Banner wird über seine Ablehnen-Schaltfläche
geschlossen; optionale Cookies werden nicht automatisch akzeptiert. Auch
verzögert eingeblendete Dialoge werden vor erneutem Klick geprüft. Nach dem
einmaligen Login wartet die Sammlung auf die Rückkehr zu Kununu und startet
in einer neuen Seite derselben Sitzung, getrennt von der Login-Weiterleitung.
Der Browser verwendet vorhandene
Nachladebuttons, behält die Seite beim Nachladen bei und sammelt höchstens
15 Mitarbeiterbewertungen innerhalb der letzten zwei Jahre. Bei der ersten
CAPTCHA pausiert der sichtbare Browser auf derselben Firma: manuell lösen,
auf die Bewertungsseite warten und in der Konsole Enter drücken. Ohne erneutes
Laden wird die freigegebene Seite ausgewertet. Ist die CAPTCHA danach noch da
oder tritt erneut eine auf, endet der gesamte Lauf. Nicht begonnene Firmen
stehen unter `not_attempted_companies`; ihre bisherigen Snapshots bleiben
unangetastet. Im Headless-Modus stoppt der Lauf sofort bei CAPTCHA.
Erneut erforderliche Anmeldung und andere Fehler werden pro Firma protokolliert.
Die Anmeldung kann serverseitig ablaufen; dann
erneut einmalig `--login` verwenden. Automatisches Lösen von CAPTCHA ist nicht enthalten.

Die separate Sitzung liegt unter `data/browser-profiles/kununu`, Ergebnisse
standardmäßig unter `output/kununu-local`; vorhandene Zyte-Ergebnisse bleiben
zum Vergleich erhalten. Kein Zyte-Schlüssel, Zyte-Limit oder Länderparameter
ist erforderlich. `--company-key "mediCAD"` wählt eine Firma,
`--browser-channel chrome` oder `msedge` einen installierten Browser.
`--headless` ist für Folgeläufe möglich, kann aber vom Website-Verhalten im
sichtbaren Fenster abweichen. `--sources` und `--output` ändern Ein-/Ausgabe.

Die Sitzung nutzt einen separaten [persistenten Playwright-Kontext](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context), nicht das persönliche Standardprofil des Browsers.

## Gewerkschaften

Die Ausgabe ist nach Unternehmen gegliedert: `<output>/<provider>/<Firma-ID>/`.
Jede ausgewählte Firma erhält einen Ordner, auch ohne Treffer oder passende
Quelle. `README.md` erklärt den Status und verlinkt die Artikel; `snapshot.json`
enthält Suchabdeckung, Gründe und Quellen. Artikel liegen unter
`articles/<Artikel-ID>/` als JSON/Text und gegebenenfalls `original.pdf`.
`<output>/<provider>/README.md` ist der Firmenindex. `_sites` und `cache` bleiben
als gemeinsame technische Quelldaten erhalten.

Quellenliste: `data/Gewerkschaften.xlsx`.
Benötigt: `Branche`, `Website` und `Gewerkschaft`.
Die optionale `News-URL` hat Vorrang vor `Website`. Andere Dateien mit
`--gewerkschaften PFAD` wählen.

`--list-gewerkschaften` zeigt die passenden Quellen ohne
Netzwerk. `--gewerkschaft TEXT` schränkt die Quellennamen ein.

| Option | Standard |
| --- | --- |
| `--article-months 6` | Sechs Kalendermonate bis heute, Berliner Zeit |
| `--article-days N` | Tageszeitraum statt Kalendermonaten |
| `--article-keyword TEXT` | Optionaler ODER-Filter; wiederholbar |
| `--gewerkschaften-max-requests 2000` | HTTP-Versuche je Quellenart inklusive robots.txt und Weiterleitungen |
| `--gewerkschaften-max-pages 50` | Inhalts-/Einstiegsseiten je Website; Suchseiten zählen separat |
| `--gewerkschaften-max-search-pages N` | Suchseiten je Website; Standard: größeres Ergebnis aus 15 und zweimal der Firmenanzahl |
| `--gewerkschaften-max-articles 10` | Passende Artikel je Firma und Quellenart |
| `--gewerkschaften-cache-only` | Nur vorhandene aktuelle Cacheantworten auswerten |

`--gewerkschaften-browser-fallback` lädt GEW-/DJV-HTML nach HTTP 403/500 zusätzlich
im versteckten Browser. `--browser-channel` wählt den Browser. robots.txt,
Quelldomains, Wartezeiten und Anfragelimit gelten auch dabei. Ein temporäres,
isoliertes Profil lädt HTML und Skripte derselben Quelldomain, keine Bilder,
Hintergrund-APIs oder Benutzer-Cookies. Auch Skripte zählen zum Anfragelimit.
Erfolgreiche Antworten werden normal gecacht; der Cachemodus öffnet keinen Browser.
`browser-attempts.json` dokumentiert die Versuche. Cloudflare-/CAPTCHA-Prüfungen
bleiben Fehler. Die Pressebereiche sind die Standard-Einstiegsseiten für GEW/DJV.

Startquellen und veröffentlichte Feeds werden vor den Firmensuchen gelesen.
Firmensuchen und Artikel werden zwischen den zugeordneten Firmen verteilt;
Suchseiten verbrauchen kein Artikel-Seitenbudget. Das HTTP-Gesamtlimit gilt
weiterhin einschließlich robots.txt und Weiterleitungen. Die Zusammenfassung
zeigt je Firma ausgeführte, fehlgeschlagene und nicht begonnene Suchanfragen.
`search_incomplete` und `search_not_attempted` sind kein Nachweis fehlender Artikel.
Ein Firmenname oder Alias muss
im Titel oder bereinigten Text stehen. PDFs benötigen Firmenbezug bereits in
Linktitel, lokaler Kurzbeschreibung oder Dateiname und anschließend im Inhalt.

`Branchenübergreifend` dient außerdem als Reserve: Zuerst werden passende
Fachquellen ausgewertet. Firmen ohne zugeordnete Fachquelle (auch ohne Branche)
oder ohne dort gefundene Artikel werden anschließend in den übergreifenden
Quellen gesucht. Bereits für dieselbe Firma geprüfte Quellen werden nicht
erneut durchsucht. Der Namensfilter `--gewerkschaft` gilt auch für die
Reserve. Die Vorschau kennzeichnet diese bedingten Quellen ausdrücklich.
Das gemeinsame HTTP-Limit bleibt unverändert und wird zwischen Fach- und
Reservequellen aufgeteilt. Im Snapshot zeigen `associations[].phase` und
`fallback_reason`, ob und warum eine Reservequelle verwendet wurde. Auch ein
erfolgloser/unvollständiger Fachabruf kann die Reserve auslösen; sein Fehlerstatus
bleibt sichtbar. Die Reserve garantiert weder Treffer noch vollständige Abdeckung.

## Implisense

```powershell
python main.py implisense login
python main.py implisense prepare
python main.py implisense resolve --headless
python main.py implisense import --headless
python main.py implisense lists --headless
python main.py implisense news --headless --period 90
```

`prepare` erstellt eine Importdatei ohne Netzwerk. `resolve` und `import`
übergeben Firmenname, Ort, PLZ und Website an den kostenlosen Website-Abgleich.
Die Zuordnung steht in `match-review.csv`. Berücksichtigt werden aktive
Kandidaten ab Score 0,9 mit passendem Namen. Ab Score 0,8 müssen der vollständige
Name, der Ort und die PLZ übereinstimmen. Widersprüchliche Angaben stehen zur
Prüfung in der Ergebnisdatei. Eine vorab anhand der Registerdaten bestätigte
`implisense_id` erlaubt bei identischem Namen eine abweichende Niederlassung.
`resolve`
speichert die Prüfliste, `import` legt zusätzlich die Liste in Implisense an.
`--company-key` begrenzt diese Schritte und `prepare` auf eine Firma.

`--sources PFAD`, `--list-name NAME` und `--list-id UUID` wählen Eingabe, Namen
und Liste. Bei geändertem Firmenbestand einen neuen Listennamen verwenden.
`news` liest eine ausgewählte oder alle vorhandenen Listen. Zeiträume:
`--period 7`, `30` oder `90` (Standard: 90). Fehlender Cache löst keine Suche aus.

Für **eine neue kostenpflichtige Recherche** zuerst den Preis lesen:

```powershell
python main.py implisense quote --headless
python main.py implisense research --headless --max-implicents 3
```

Das Beispiel gibt höchstens 3 Implicents für eine Liste frei. Höherer oder
unbekannter Preis stoppt den Aufruf; Fehler werden nicht automatisch wiederholt.
Ohne `--list-id` verwenden `quote` und `research` die zuletzt importierte Liste.
Die Anmeldung bleibt unter `output/implisense/browser/profile/` gespeichert.
Auf einem anderen Rechner ist eine eigene Anmeldung nötig. `--headless` ist
bei `login` nicht möglich.

Bei `secondary` heißen die Listenoptionen `--implisense-list-id UUID` und
`--implisense-period 7|30|90`. Firmenfilter wählen keine Implisense-Listen aus.

## Ausgabe und gespeicherte Dokumente

`--output PFAD` ändert den Ausgabeordner. Bei Gruppen ist dies der gemeinsame
Ordner, bei `unternehmensregister` direkt dessen Quellenordner und bei
`implisense` direkt dessen Browserordner. Bei geändertem Gruppen-Ausgabeordner
wird auch die Implisense-Anmeldung dort erwartet. Standard ist wie im README
beschrieben `output/` mit Quellenunterordnern.

Nur Artikel in der aktuellen Übersicht gehören zum letzten Lauf. Ältere Dateien
können erhalten bleiben. Leere Artikelsuchen stehen in der jeweiligen `batch.json`.
Implisense erscheint im gemeinsamen Bericht getrennt von den Firmen, da es
eigene Listen verwendet.

```powershell
python main.py documents --refresh-html output/unternehmensregister
python main.py documents --reclassify output/unternehmensregister
```

Diese Befehle erneuern HTML-Text/JSON bzw. ordnen gespeicherte
Registerveröffentlichungen neu ein. Es werden keine neuen Dokumente angefordert.

### Ausgabeformat prüfen

Die Lieferdateien der fünf PDF-Quellen werden vor dem Schreiben normalisiert
und geprüft. Signalregeln und Karrieredaten sind davon getrennt. Details und
Grenzen: [Ausgabevertrag](output-contract.md).

```powershell
# Nur prüfen, ohne Änderungen oder Netzwerk
python main.py output-contract output output/kununu-local

# Vorhandene Lieferdateien normalisieren; Originale vorher sichern
python main.py output-contract output output/kununu-local --apply
```

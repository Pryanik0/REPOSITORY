# Suchverfahren und Grenzen

Primäre Quellen: Unternehmensregister, Kununu, Gewerkschaften und Unternehmenskarriereseiten.
Sekundäre Quelle: Implisense. Die Sammlung läuft nacheinander.
Optionen stehen in der [Befehlsreferenz](reference.md).

## Unternehmensregister

Playwright bedient die öffentliche deutsche Suchoberfläche. Vor dem Ausfüllen
wartet das Programm auf die Initialisierung der Suchfelder und prüft danach,
ob der Firmenfilter erhalten blieb. Finanzberichte werden getrennt gesucht,
damit sie nicht zwischen Bekanntmachungen untergehen. Ergebnisse müssen die
vollständigen Suchnamensteile enthalten und ein passendes Datum haben.

Die Registerrubrik bestimmt die Kategorie. Offenlegungsbefreiungen nach
§ 264 Abs. 3 bzw. § 264b und ausdrückliche Hinweise zählen zu Bekanntmachungen.
Seiten- und Ergebnislimits begrenzen die Suche. Downloads sind optional.
Zusätzliche CAPTCHAs werden als Zugriffsproblem gemeldet.
Die Zusammenfassung berücksichtigt Such- und Downloadlimits getrennt. Kurze
Registermitteilungen werden anhand ihres geladenen Inhaltsbereichs akzeptiert;
bei Fehlern bleibt Diagnose-HTML erhalten.

HTML-Tabellen behalten ihre Zeilen, Spalten und Zellverbünde. PDF-Text wird
seitenweise gelesen. ZIP-/ESEF-Dateien werden im Speicher mit Größen-, Datei-
und Verschachtelungsgrenzen verarbeitet. Es gibt keine finanzielle Interpretation.

## Kununu

Das Programm benötigt bekannte Profiladressen. Zyte liefert JavaScript-gerendertes
HTML je angeforderter Seite. Keine automatischen Wiederholungen und keine
alternativen kostenpflichtigen Abrufwege. Profiladresse und eingebettete Daten
müssen zur ausgewählten Firma passen. Sicherheitsabfragen gelten als Fehler;
das HTML bleibt zur Diagnose gespeichert.

Ausgelesen werden Name, Gesamtbewertung, Bewertungsanzahl und Weiterempfehlung,
soweit vorhanden. Die Bewertungsstichprobe umfasst höchstens 15 datierte
Mitarbeiterbewertungen aus den letzten zwei Jahren. Arbeitgeberantworten und
Dubletten werden ausgeschlossen. Weitere Seiten sind durch das Anfragelimit
begrenzt. Die Navigation verwendet vorhandene Links oder den sichtbaren
Nachladebutton und prüft neue Karten-IDs. Veraltete eingebettete Startdaten
verdrängen keine nachgeladenen sichtbaren Bewertungen. Kategorie-Mittelwerte beruhen nur auf ausgelesenen Bewertungen und
sind keine repräsentativen Arbeitgebermittelwerte.

## Gewerkschaften

Die Branche verbindet Firmen und Quellenlisten. Jede passende Website wird
gemeinsam für die zugeordneten Firmen durchsucht. Das Programm folgt erkennbaren
GET-Suchformularen, veröffentlichten Suchvorlagen, Artikellinks, Feeds und
angebotenen Sitemaps. Versteckte Suchschnittstellen werden nicht erraten.
Branchen werden nach dem [gemeinsamen Verzeichnis](branches.md) normalisiert.
Suchseiten und Inhaltsseiten haben getrennte Seitenbudgets. Startquellen und
Feeds werden zuerst besucht, Suchanfragen und Artikel zwischen Firmen verteilt.
Nicht begonnene oder gescheiterte Suchen stehen in der Firmenzusammenfassung.

Artikel benötigen ein belegtes Datum im gewählten Zeitraum und eine Firmennennung
im Titel oder bereinigten Text. Kurze Großbuchstabenmarken werden strenger auf
passende Schreibweise geprüft. Navigation, Werbung, Cookiehinweise und Empfehlungen
werden entfernt. HTML-Artikel benötigen mindestens 300 Zeichen und 45 Wörter.

Vor PDF-Downloads wird der Firmenbezug anhand von Linktitel, lokaler Ergebniskarte
oder Dateiname geprüft. Danach gelten dieselben Inhalts- und Datumsfilter wie
für HTML. PDF-Erstellungsdaten gelten nicht als Veröffentlichungsnachweis.
Scans ohne auslesbaren Text benötigen externe OCR und bleiben in der Diagnose.

Die HTTP-Schicht beachtet robots.txt, Wartezeiten und Quelldomains. Erfolgreiche
Antworten bleiben 24 Stunden im Cache. HTML/XML ist auf 3 MB und PDF auf 20 MB
begrenzt. Fehlversuche und Weiterleitungen zählen zum Anfragelimit.

IGBCE verwendet ohne ausdrückliche News-URL den offiziellen Presseportal-Presseraum
56813, da igbce.de den Crawler sperrt. ver.di verwendet die veröffentlichte
Suchroute `/suche?q=...`. Diese Anpassungen betreffen die Quellen, keine lokalen
Benutzerkonten oder Rechnerpfade.

IG Metall verwendet die auf https://www.igmetall.de/rss-feeds veröffentlichten
Feeds `igmetall.rss` und `presse.rss` sowie Pressemitteilungen und Tarifrunden
als Startseiten. Die interne Suche bleibt deaktiviert, weil robots.txt sie
sperrt. `search_mode=feeds_and_links` kennzeichnet diesen Weg. Datumsfilter,
Firmennennung, Seiten-/HTTP-Limits und robots.txt gelten weiterhin; RSS und
Rubriken garantieren keine vollständige historische Suche. Explizit eingetragene
News-URLs bleiben erhalten.

## Unternehmenskarriereseiten

Die Firmenwebsite aus `url` und optionale direkte `career_url`-Adressen bilden
den Einstieg. Karriereverweise, Stellenlinks, bekannte Bewerberportal-Iframes
und vorhandene nächste Seiten führen zu weiteren Angeboten. Externe Portale
werden über tatsächlich gefundene Arbeitgeberlinks oder HTTP-/Meta-Weiterleitungen
aufgenommen. Die Herkunft und HTTP-Weiterleitungsketten bleiben erhalten;
allgemeine Jobbörsen werden ausgeschlossen. Bei gemeinsamen SmartRecruiters-
Hosts bleibt die Suche im Firmenpfad. Quellen und Herkunftslinks bleiben im
Snapshot dokumentiert.

Drei Adapter lesen veröffentlichte Angebote direkt aus dem Bewerbersystem:
[Personio XML](https://developer.personio.de/docs/retrieving-open-job-positions)
und [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html) sowie
[Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api).
Die Feedadresse wird nur aus einem zugeordneten Portal abgeleitet. Das erspart
Einzelabrufe und funktioniert auch bei per Skript eingebetteten Greenhouse-Boards.
Bei fehlerhaften oder nicht aktivierten Feeds bleibt HTML als Fallback erhalten;
Versuche stehen unter `adapter_attempts`, Herkunftslinks unter `sources`.
Cache und HTTP-Limit gelten für alle Abrufe. Die ausdrücklich öffentliche Ashby-
Stellen-API wird direkt angesprochen; ihre allgemeine API-Domain verlangt für
robots.txt eine Anmeldung. Andere Quellen behalten die robots-Prüfung.
Direkte Personio-Stellenlinks und veröffentlichte Feed-URLs in `fetch`-Aufrufen
werden erkannt. Das HTTP-Budget wird für die noch ausstehenden Firmen reserviert.

Strukturierte Daten werden nach [Schema.org JobPosting](https://schema.org/JobPosting)
ausgelesen; einzelne HTML-Stellenseiten benötigen einen Titel und erkennbare
Stellenmerkmale. Navigationslinks zählen nicht als Stellen. Datierte abgelaufene
Angebote sowie ausdrücklich geschlossene Stellen werden ausgeschlossen.
Rexx-Bewerbungsformulare (`-f123.html`) werden nicht zusätzlich zur Stellenanzeige
gezählt. Sprachvarianten von sea-chefs-Anzeigen teilen dieselbe Stellenkennung.
Bei mehreren Hauptüberschriften wird eine eindeutig erkennbare Berufsüberschrift
gegenüber einem Werbeslogan bevorzugt; Bewerbungsinformationen und Talent-Community-
Anmeldungen zählen nicht als konkrete Stellen.
Die Ausgabe enthält die aktuell veröffentlichten Angebote auf den geprüften
Seiten, keine Garantie ihrer tatsächlichen Besetzbarkeit oder einer vollständigen
Erfassung. Die Stichwortsuche filtert Titel und Beschreibung erst nach dem Abruf.

JavaScript wird nicht ausgeführt. JavaScript-Portale ohne unterstützten Feed, nicht auslesbare
Seiten und Nachladebuttons ergeben `partial`; ebenso erreichte Seiten-, Stellen-
oder HTTP-Limits. Fehler und noch offene URLs bleiben sichtbar. Null Treffer
bei unvollständiger Suche bedeuten nicht, dass die Firma keine offenen Stellen
hat. robots.txt, Cache und HTTP-Grenzen entsprechen den Artikelsammlern,
mit einem getrennten Budget für die gesamte Karrieresuche.

## Implisense

Die eigene Anmeldung erfolgt in einem getrennten lokalen Browserprofil.
Der Listenimport sendet Firmenname, Ort, PLZ und Website in Gruppen von bis zu
20 Einträgen. Die Prüfliste nennt den Treffer und mögliche Abweichungen. Mit
`resolve` lässt sich die Zuordnung getrennt vom Anlegen der Liste prüfen.

`news` liest vorhandene Nachrichten und Registerereignisse für 7, 30 oder 90 Tage.
Fehlende gespeicherte Recherche löst keinen kostenpflichtigen Aufruf aus.
Nur `research` mit ausdrücklichem Implicents-Limit darf nach Preisprüfung eine
neue Recherche starten, ohne automatische Wiederholung nach Fehlern. Die
eigentliche Nachrichtensuche findet bei Implisense statt.

Zugriffsprobleme, Seitenänderungen, fehlende Daten und konfigurierte Limits
können Ergebnisse unvollständig machen und stehen in den Ergebnisdateien.
Eine Namensübereinstimmung ist keine rechtliche Identitätsprüfung.

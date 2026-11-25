# Ausgabe nach den JSON-Schemas v0.4

Die vier Quellen Kununu, Unternehmensregister, Gewerkschaften und
Implisense schreiben ihre Liefer-JSONs über eine gemeinsame Formatprüfung.
Grundlage sind die gelieferten Dateien unter `data/json-schemas/`, geprüft mit
`jsonschema` (Draft 2020-12). Diese Dateien werden unverändert mit versioniert.
Signalzuordnung, Scores, KI-Befunde und Karriereseiten gehören nicht zu dieser Änderung.

## Verhalten beim Speichern

- Kununu erhält `reason` sowie alle 13 Schlüssel in `factor_scores` und die drei
  Schlüssel `positive`, `negative`, `suggestion` in `sections`. Fehlende optionale
  Werte stehen als `null`; vorhandene Empfehlungen und Bewertungen bleiben erhalten.
- Ein Review ohne nichtleere ID, Bewertung 1–5, boolean `recommended`, Datum oder
  Typ als Text wird nicht als gültiger Review geliefert. Seine
  vollständigen ursprünglichen Daten stehen unter `contract_rejections` zusammen
  mit der Ursache. Der Status wird `partial`, bei fehlenden nutzbaren Inhalten
  `error`. Dasselbe gilt für ein ungültiges Profil. Keine Empfehlung wird geraten.
- Unbekannte Faktor-/Abschnittsschlüssel bleiben in den ursprünglichen Objekten
  erhalten. `x-bekannte-werte` ist eine offene Beschreibung, kein `enum`.
  Aggregate und Bewertungsdiagnose beziehen sich auf die gelieferten Reviews.
  `complete` bei Teilnoten bedeutet, dass alle 13 bekannten Faktoren vorliegen.
- Browser und Zyte öffnen „Alle anzeigen“, bevor nachgeladene Karten ausgelesen
  werden. DOM-Werte ergänzen fehlende Redux-Teilnoten, überschreiben sie aber nicht.
- Bei unverändertem Profil und Review können fehlende Teilnoten aus dem vorherigen
  Snapshot ergänzt werden. ID, Datum, Gesamtnote, Empfehlung, Typ, Titel und Inhalt
  müssen übereinstimmen; widersprechende vorhandene Teilnoten verhindern die Ergänzung.
  `factor_score_sources` dokumentiert je ergänztem Faktor den alten Snapshot und
  dessen Beobachtungszeit. Neue Reviews werden niemals aus der Historie hinzugefügt.
- Fehler-/Übersprungen-Status erhalten immer einen Grund. Wie in der PDF werden
  bei anderen Status als `ok`/`partial` nur `status` und `reason` validiert.
- Register-PDFs enthalten zusätzlich den aus `pages[].text` zusammengesetzten
  Volltext `text` und `tables: []`, sofern keine Tabellen extrahiert wurden.
  Die ursprünglichen Seiten bleiben erhalten; es werden keine Tabellen erfunden.
- `downloaded_no_text` und `unsupported` werden im Liefer-JSON zu `error`, mit
  `source_status` und Fehlerbeschreibung. Die internen Diagnosen können weiterhin
  zwischen erfolgreichem Dateidownload und erfolgreicher Textextraktion unterscheiden.
- `truncated` ist auch dann `true`, wenn Register-Suchseiten ungelesen bleiben.
  `coverage.publication_limit_reached` beschreibt weiterhin separat das Mengenlimit.
- Register-Sprache `de`/`en` wird zu `Deutsch`/`Englisch`; `source_language`
  bewahrt den ursprünglichen Code. Weitere Sprachen sind laut Schema erlaubt.
- Artikel-Snapshots erhalten `reason: null`, wenn kein Fehlergrund vorliegt.
  Volltexte bleiben in `articles/<id>/article.json`.
- Implisense-Einträge werden auf Pflichtfelder und Typen geprüft. Ein ungültiger
  neuer Export ersetzt keine vorhandene gültige `news.json`. Die vorab gespeicherten
  API-Antworten bleiben für die Diagnose verfügbar.

Weitere strukturell ungültige Lieferdateien werden vor dem Schreiben abgelehnt.
Zusatzfelder sind zugelassen, insbesondere bestehende Coverage- und Diagnosefelder.
Die Formatprüfung garantiert weder Quellenverfügbarkeit noch eine Mindestmenge
an Bewertungen, vollständige Stellensuche oder auswertbare Finanzkennzahlen.

## Vorhandene Daten prüfen oder aktualisieren

```powershell
python main.py output-contract output output/kununu-local
python main.py output-contract output output/kununu-local --apply
python main.py output-contract deliveries/SGL_pbb_WALDEMAR_LINK --apply --backup-root output/contract-backups
```

Flache Lieferpakete (`<firma>/kununu.json`, `<firma>/register/…`) werden ebenfalls
unterstützt. Dort werden `company` aus Name und Firmenordnerschlüssel sowie
`truncated` aus vorhandenen Coverage-Angaben aufgebaut. Fehlende Downloadkategorien
werden ausschließlich über eindeutig passende Veröffentlichungen zugeordnet.
Relative `documents_path`-Verweise bleiben erhalten und müssen innerhalb des
Firmenordners liegen. Unklare Zuordnungen stoppen die gesamte Migration.

Mit `--kununu-history <alter Ausgabeordner>` können bei `--apply` ausdrücklich
ausgewählte frühere Kununu-Snapshots fehlende Teilnoten ergänzen. Es gelten die
oben beschriebenen Identitätsprüfungen und Herkunftsangaben. Ohne dieses Argument
liest die Migration keine historischen Snapshots.

Ohne `--apply` wird ausschließlich gelesen. Mit `--apply` werden zunächst alle
ausgewählten Dateien und ihre Verknüpfungen geprüft, dann nur geänderte Dateien
geschrieben. Unter `<erster Ausgabeordner>/contract-backups/<UTC-Zeit-ID>/` liegen
exakte Originale und ein `manifest.json` mit Originalpfaden und SHA-256-Werten.
Ein erneuter Aufruf verändert bereits normalisierte Daten nicht.

Als Argument ist ein gemeinsamer Ausgabeordner oder direkt ein Quellenordner
wie `output/kununu` möglich. Weitere gespeicherte Testläufe müssen ausdrücklich
als weitere Ausgabeordner angegeben werden.

Die Auswahl umfasst Firmen-Snapshots und die durch aktuelle Artikel-/Downloadlisten
referenzierten Texte. Browserprofile, HTTP-Caches, Kununu-`history`, Backups,
Karrieredaten und nicht mehr referenzierte Altdateien bleiben unverändert.
Ein reiner Register-Suchlauf darf ohne Downloads bestehen; neue Suchläufe ohne
`--download` schreiben eine leere `downloads.json`, damit alte Downloadzuordnungen
nicht zum neuen Suchlauf gerechnet werden.

Die Prüfung kontrolliert eindeutige Review-/Artikel-IDs und Registerpaare
`category + index`, vorhandene Artikeltexte, passende Downloadreferenzen und
Dokumente für erfolgreiche Downloads. Fehlende verknüpfte Dateien sind Fehler.
Ein Verzeichnis ohne Lieferdateien gilt nicht als erfolgreicher Prüflauf.

Es wird kein Server angesprochen, kein Uploadordner veröffentlicht und keine
`_fertig`-Marke gesetzt. Historische Dateien oder Backups nicht rekursiv als neue
Lieferung übertragen; maßgeblich sind die aktuellen Indizes.

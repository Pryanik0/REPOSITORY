# Rechercheergebnisse

Dieser Ordner wird beim Ausführen der Befehle gefüllt.

| Unterordner | Inhalt |
| --- | --- |
| `unternehmensregister/` | Veröffentlichungen und optionale Downloads je Firma |
| `kununu/` | Profil, Bewertungen und Diagnose in `snapshot.json`; HTML unter `zyte/` |
| `gewerkschaften/` | Gewerkschaftsartikel, Originale, Quellenprotokolle und Cache |
| `karriere/` | Stellen je Firma, `snapshot.json`, Stellenbeschreibungen und Herkunft der Arbeitgeberportale |
| `implisense/browser/` | Nachrichtenexporte, Zuordnungsprüfung und lokale Anmeldung |
| `research/batch.json` | Übersicht des letzten gemeinsamen Laufs |

Firmenordner enthalten zur Unterscheidung einen kurzen Hash. Artikelsuchen ohne
Treffer stehen in `batch.json`. Nur in aktuellen Übersichten aufgeführte Artikel
gehören zum letzten Lauf; ältere Dateien können erhalten bleiben.

`checked-pdfs.json` unterscheidet Quelldownloads von tatsächlichen Firmennennungen
und übernommenen PDF-Artikeln. Quelldownloads können mehreren Firmen dienen.

Das Implisense-Unterverzeichnis `profile/` enthält die lokale Anmeldung und
gehört nicht zur Weitergabe. Der Empfänger meldet sich selbst an.
Lokale Sicherungen unter `archive/` gehören ebenfalls nicht zum Weitergabepaket.

Die Lieferdateien für Kununu, Register, Gewerkschaften und Implisense werden
gegen `data/json-schemas/` geprüft. Für die Karrieredaten liegt dort kein Schema vor.
Bestehende Dateien lassen sich mit `python main.py output-contract output output/kununu-local`
ohne Netzwerk prüfen und mit zusätzlichem `--apply` normalisieren.
`contract-backups/` enthält die unveränderten Originale dieser Migration und darf
nicht mitgeliefert werden. Einzelheiten: [Ausgabevertrag](../docs/output-contract.md).

# Einheitliche Branchen

`research_app/branches.py` ist das gemeinsame Verzeichnis für `sources.csv`,
`Gewerkschaften.xlsx` und zusätzliche Firmenarbeitsmappen.
Groß-/Kleinschreibung, Unicode, Leerzeichen sowie `&`/`und` werden beim Vergleich
vereinheitlicht. Mehrere Branchen in einer Zelle werden mit `;` oder `|` getrennt.

| Frühere Bezeichnung | Vereinheitlichte Branchen |
| --- | --- |
| Transport, Logistik und Entsorgung | Logistik; Abfallwirtschaft |
| Transport & Logistik | Logistik |
| Digitale Wirtschaft und IT | Digitale Wirtschaft; IT & Telekommunikation |
| Chemieindustrie, Biotechnologie | Chemieindustrie; Biotechnologie |
| Glass, Kunststoffindustrie und Keramikindustrie | Glasindustrie; Kunststoffindustrie; Keramikindustrie |
| Bildung und Erziehung | Bildung & Wissenschaft |
| Pflege und Gesundheitswesen; Sozialwesen | Gesundheit & Sozialwesen |
| Mittelstand, branchenübergreifend | Mittelstand; Branchenübergreifend |

`Gesundheit & Sozialwesen` ist eine gemeinsame Oberkategorie für diese Recherche.
Die Zuordnung bezeichnet eine thematisch passende Gewerkschaftsquelle.

Unbekannte Branchen werden nicht unscharf geraten. `Rechtsberatung` bleibt
beispielsweise eine eigenständige Branche. Eine branchenübergreifende Quelle
wird nicht automatisch jeder Firma zugeordnet. Fehlt eine passende Zeile in
der Quellenliste, meldet das Programm weiterhin den fehlenden Quellenbezug.
Normalisierung ergänzt keine neuen Organisationen oder fachlichen Zuständigkeiten.

Für Logistik,
Gesundheit & Sozialwesen sowie Rechtsberatung fehlen weiterhin entsprechende
Gewerkschaftszeilen in der aktuellen Auswahl.

Die vorhandenen Dateien sind bereits normalisiert. Für spätere Änderungen:

```powershell
python -m research_app.normalize_branches
python -m research_app.normalize_branches --write
```

Ohne `--write` wird nur eine Vorschau ausgegeben. Vor dem Schreiben wird jede
geänderte Originaldatei unter `data/backups/branch-normalization/` gesichert.
Organisationen, URLs, übrige Spalten und Excel-Zellformate bleiben erhalten.
Ein weiterer Lauf auf bereits normalisierten Dateien schreibt nichts.

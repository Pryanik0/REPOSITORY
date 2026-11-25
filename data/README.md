# Eingabedateien

| Datei | Zweck |
| --- | --- |
| `sources.csv` | Gemeinsame Firmenliste aller Suchbefehle |
| `Gewerkschaften.xlsx` | Branchen, Namen und Websites der Gewerkschaften |

Pflichtspalte der CSV: `company_key`. `enabled=false` deaktiviert eine Firma.
`kununu_url` enthält das Arbeitgeberprofil, `url` die offizielle Firmenwebsite
für Implisense und als Einstieg für die Karrieresuche. Die optionale Spalte
`career_url` setzt eine direkte Karriereadresse oder ein firmeneigenes Bewerberportal.
`Branche` wählt die passenden Gewerkschaften; mehrere Werte mit
`;` oder `|` trennen. CSV-Felder bei Bedarf in Anführungszeichen setzen.

Die Quellenliste benötigt `Gewerkschaft`, `Branche`, `Website`
und optional `News-URL`. Wiederholte Organisationen werden vereinigt.
Ein gemeinsames [Branchenverzeichnis](../docs/branches.md) vereinheitlicht CSV
und Excel beim Einlesen. Sammelbegriffe werden in mehrere Branchen aufgeteilt;
unbekannte Bezeichnungen bleiben erhalten. Die Suchbefehle verändern die
Eingabedateien nicht. Die vorhandenen Eingabedateien wurden am 10.11.2026
normalisiert; unveränderte Sicherungen liegen unter `backups/branch-normalization/`.

Die Organisationen entsprechen den bereitgestellten Screenshots (Stand: 10.11.2026):
26 Gewerkschaftszeilen. Die Branchenbezeichnungen sind vereinheitlicht.

```powershell
python main.py primary --list-companies
python main.py gewerkschaften --list-gewerkschaften
```

Die Vorschau benötigt keinen Netzwerkzugriff. Alte Signal- oder Firmenlisten
werden für diesen Ablauf nicht benötigt und gehören nicht zum Weitergabepaket.
Weitere Spalten und Formate: [Befehlsreferenz](../docs/reference.md).

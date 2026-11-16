# Rechnungsapp

## Versionierung und Release Notes

- Die aktuelle Version steht in `dev/VERSION` (SemVer) und wird im Seitenfuß der App angezeigt.
- Release Notes stehen in `dev/CHANGELOG.md` (neueste Version oben, pro Version Stichpunkte mit `- `) und werden unter `/release-notes` angezeigt.
- Jede Umsetzung sofort im obersten Abschnitt von `dev/CHANGELOG.md` festhalten, nicht erst beim Commit.
- Nach dem Wort „Commit“ des Benutzers: Version in `dev/VERSION` und im obersten CHANGELOG-Abschnitt (mit Datum) abschließen, dann committen. Weitere Arbeit danach beginnt einen neuen Abschnitt mit höherer Version.
- Neue Funktion = Minor-Version, Fehlerkorrektur = Patch, Breaking Change = Major.

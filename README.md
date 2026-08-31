# autonomiq-preise

Börsenpreise für die AutonomIQ-Boxen — geholt von einer GitHub Action,
abgelegt als JSON, statisch ausgeliefert.

**Die Dateien in diesem Verzeichnis gehören in ein eigenes, öffentliches
Repository** und nicht in das Box-Repository. Hier liegen sie zur Übergabe.

---

## Warum das so gebaut ist

**Ein Token statt hundert.** ENTSO-E verlangt eine Registrierung je Konto; wir
legen keine Token je Anlage an und verteilen unseren eigenen nicht auf hundert
`.env`-Dateien.

**Der ausliefernde Pfad hat keine Laufzeit.** Fällt die Action aus, bleibt die
letzte Datei liegen und wird weiter ausgeliefert — die richtige Degradation,
ohne dass jemand sie programmiert. Derselbe Grund, aus dem die Preisdatei auf
der Box vom nginx kommt und nicht vom Agenten.

**Und er hat kein Geheimnis.** Der Token liegt als Action-Secret im Build und
**nie** im Artefakt. Deshalb dürfen die Dateien öffentlich sein — und deshalb
brauchen die Boxen kein Zugriffstoken.

**Er ersetzt die lokale Datei auf der Box nicht.** Fällt der Dienst aus, holt
jede Box selbst bei ENTSO-E und Energy-Charts. Er nimmt Arbeit ab, keine
Fähigkeit.

---

## Einrichten

1. **Repository anlegen**, öffentlich, Name `autonomiq-preise`.
2. **Diese Dateien hineinkopieren** — alles aus `preisdienst/`:

        erzeuge.py
        zonen.json
        quellen/__init__.py
        quellen/entsoe.py
        quellen/energy_charts.py
        .github/workflows/preise.yml
        README.md

3. **Ein Secret setzen**: `ENTSOE_TOKEN` (Settings → Secrets and variables →
   Actions). Das ist das einzige.
   **Ohne Token läuft es auch** — dann trägt Energy-Charts allein, und die
   Lage sagt `entsoe: uebersprungen (kein Token)`.
4. **Actions Schreibrechte geben**: Settings → Actions → General → Workflow
   permissions → *Read and write*. (Der Workflow fordert `contents: write`
   an; ohne diese Einstellung wird es verweigert.)
5. **Einmal von Hand starten**: Actions → „Preise holen" → *Run workflow*.

Danach liegt

    preise/AT/spot.json
    preise/AT/meta.json
    preise/zonen.json

**GitHub Pages ist nicht nötig.** Die Roh-Adresse genügt und ist gemessen
passend: `cache-control: max-age=300` und ein `etag` — genau der
Fünfminutentakt, in dem die Boxen ohnehin fragen.

    https://raw.githubusercontent.com/<org>/autonomiq-preise/main/preise/AT/spot.json

Pages wäre eine zusätzliche Stufe, die ausfallen kann, für eine hübschere
Adresse. Das ist der falsche Tausch.

---

## Eine zweite Zone

Eine Zeile in `zonen.json`:

    {"name": "DE-LU", "eic": "10Y1001A1001A82H"}

Mehr nicht. Die Boxen leiten ihren Pfad aus ihrem EIC-Code ab
(`energy_charts.zone_fuer`), also ändert sich dort nichts.

**Kein Rückfall auf eine Vorgabezone.** Ein unbekannter EIC-Code bedeutet
„keine Quelle", nicht „nimm die erste" — eine falsche Zone liefert plausible
Preise eines fremden Marktes, und das fällt weder im Log noch in der Anzeige
auf.

---

## Was der Dienst NICHT tut

**Keine Preislogik.** Aufschläge, Netzentgelte, Steuer und die Auflösung der
Einspeisung sind Vertragsdetails des Kunden und bleiben in evccs Tarif
(`charges`, `tax`, `formula`). Hier kommt der **Börsenpreis der Zone** heraus,
in EUR/kWh, roh — sonst hätte man Kundenlogik in einem geteilten Artefakt.

**Keine Prognose über die Veröffentlichung hinaus.** Was die Börse nicht hat,
erfinden wir nicht.

---

## Den Verzug messen

GitHub Actions erlaubt fünf Minuten als kürzestes Intervall, und die
Ausführung ist ausdrücklich nicht garantiert: verzögert bei Last, und *„some
queued jobs may be dropped"* — also **still**.

`meta.json` trägt deshalb beide Zeiten:

    "erzeugt_am":        wann der Lauf geschrieben hat
    "lauf_geplant_fuer": wann er laut Cron-Ausdruck hätte laufen sollen

**Der Verzug ist die Differenz.** GitHub gibt den Sollzeitpunkt nicht her —
`github.event.schedule` ist der *Ausdruck* —, also rechnet `erzeuge.py` ihn
aus. Nachträglich wäre er nicht mehr rekonstruierbar.

**Der Abstand zur Veröffentlichung fällt aus der Historie ab**: der erste
Commit, dessen `deckt_bis` über den Folgetag reicht. Die Commit-Nachricht
trägt die Abdeckung, `git log` ist damit das Archiv.

---

## Die Quelldateien in `quellen/`

**Byteweise Kopien** aus dem Box-Repository
(`agent/autonomiq_agent/entsoe.py`, `energy_charts.py`). Geändert wird
**dort**, hier wird kopiert.

`agent/tests/test_preisdienst_kopie.py` im Box-Repository hält beide
identisch — eine Abweichung lässt den Test fallen. Ohne das wäre die
Entduplizierungsregel (`classificationSequence`, niedrigste Position) an zwei
Orten gepflegt, und einer liefe irgendwann hinterher.

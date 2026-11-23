# CLAUDE.md — autonomiq-preise

Antworten an den Betreiber auf Deutsch.

## Keine simulierten Eingaben in sichtbaren Fenstern (Regel seit 23.11.2026)

**Keine simulierten Klicks, Tastendrücke oder Texteingaben in Fenstern, die der
Betreiber sieht** — sein Browser, sein Simulator- oder Emulatorfenster, jede
andere App auf seinem Bildschirm. Simulator, Emulator und Browser werden
weiter voll genutzt, aber **nur ohne Bedienoberfläche oder in eigenen,
isolierten Instanzen**:

| Werkzeug | so |
|---|---|
| Browser | kopflos (`--headless`, Playwright/Puppeteer mit eigenem Profil) — nie die Fenster oder Tabs des Betreibers |
| Android-Emulator | `-no-window`, eigenes AVD; Eingaben nur per `adb` an diese Instanz |
| iOS-Simulator | eigenes Gerät per `simctl`, Fotos mit `simctl io`; kein Fernsteuern der Simulator.app, die der Betreiber offen hat |
| Desktop-Apps | keine Bildschirmsteuerung (Computer Use, AppleScript-Tastendrücke) |

Anlass: am 22.11.2026 ging bei einem Testlauf ein synthetischer Klick samt
Text „simulation-123“ und Enter in das sichtbare Browserfenster des Betreibers.

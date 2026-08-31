#!/usr/bin/env python3
"""Boersenpreise holen und als JSON ablegen — der zentrale Preisdienst.

**Ein Token statt hundert.** ENTSO-E verlangt eine Registrierung je Konto; wir
legen keine Token je Anlage an und verteilen unseren eigenen nicht auf hundert
`.env`-Dateien. Der Dienst holt einmal, die Boxen lesen.

**Er ersetzt die lokale Datei auf der Box nicht.** Die bleibt — sonst waere
dieser Dienst ein gemeinsamer Ausfallpunkt. Faellt er aus, holt jede Box
selbst; sie kann es. Er nimmt Arbeit ab, keine Faehigkeit.

## Was hier NICHT passiert

**Keine Preislogik.** Aufschlaege, Netzentgelte, Steuer und die Aufloesung der
Einspeisung sind Vertragsdetails des Kunden und bleiben in evccs Tarif. Hier
kommt der **Boersenpreis der Zone** heraus, in EUR/kWh, roh.

**Kein Rueckfall auf eine Vorgabezone.** Eine unbekannte Zone wird
uebersprungen und gemeldet; geraten wird nicht. Plausible Preise eines fremden
Marktes faellt niemandem auf.

**Und keine schlechtere Datei.** Reicht das Neue nicht so weit wie das
Vorhandene, bleibt das Vorhandene stehen — dieselbe Regel wie auf der Box.
Ein halb geantworteter Server darf aus einer guten Datei keine schlechte
machen.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from quellen import energy_charts as ec           # noqa: E402
from quellen.entsoe import (                      # noqa: E402
    EntsoeFehler, auf_stunden, pruefe_plausibel,
)
from quellen.entsoe import hole as entsoe_hole    # noqa: E402

#: Wie weit voraus geholt wird.
STUNDEN = 48


def geplanter_lauf(cron: str, jetzt: datetime) -> str | None:
    """Der letzte Zeitpunkt vor `jetzt`, den dieser Cron-Ausdruck trifft.

    **Damit der Verzug messbar wird.** GitHub verzoegert Laeufe bei Last und
    verwirft sie gelegentlich still (*„some queued jobs may be dropped"*);
    ohne den Sollzeitpunkt ist hinterher nicht zu sagen, wie gross der Verzug
    war. `github.event.schedule` liefert nur den **Ausdruck**, keinen
    Zeitstempel — also wird er hier ausgerechnet.

    Ausgewertet werden Minute und Stunde; Tagesfelder haben unsere Ausdruecke
    nicht. `None`, wenn nichts uebergeben wurde (manueller Lauf) — **nicht
    `jetzt`**, denn ein Verzug von null waere eine Behauptung.
    """
    teile = cron.split()
    if len(teile) < 2:
        return None

    def felder(spec: str, hoechst: int) -> set[int]:
        raus: set[int] = set()
        for stueck in spec.split(","):
            if stueck == "*":
                return set(range(hoechst + 1))
            if "-" in stueck:
                a, _, b = stueck.partition("-")
                try:
                    raus.update(range(int(a), int(b) + 1))
                except ValueError:
                    return set()
            else:
                try:
                    raus.add(int(stueck))
                except ValueError:
                    return set()
        return raus

    minuten, stunden = felder(teile[0], 59), felder(teile[1], 23)
    if not minuten or not stunden:
        return None

    kandidat = jetzt.replace(second=0, microsecond=0)
    # Hoechstens 48 Stunden zurueck — mehr waere kein Verzug, sondern ein
    # ausgefallener Zeitplan.
    for _ in range(48 * 60):
        if kandidat.minute in minuten and kandidat.hour in stunden:
            return kandidat.isoformat()
        kandidat -= timedelta(minutes=1)
    return None


def _als_json(punkte) -> list[dict]:
    return [{"start": p.beginn.isoformat(),
             "end": p.ende.isoformat(),
             "value": round(float(p.wert), 6)} for p in punkte]


def _lies(pfad: pathlib.Path):
    try:
        return json.loads(pfad.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _deckt_bis(roh) -> datetime | None:
    enden = []
    for eintrag in roh if isinstance(roh, list) else []:
        try:
            enden.append(datetime.fromisoformat(
                str(eintrag["end"]).replace("Z", "+00:00")))
        except (KeyError, ValueError, TypeError):
            continue
    return max(enden) if enden else None


def hole_zone(zone_eic: str, token: str | None, jetzt: datetime):
    """Beide Quellen der Reihe nach. Gibt `(punkte, quelle, meldungen)`.

    **Erst ENTSO-E, dann Energy-Charts** — und weitergefragt wird auch nach
    einem Erfolg, solange morgen fehlt. Liefert die erste Quelle nur den
    laufenden Tag, waere „nach dem ersten Treffer aufhoeren" der teure
    Abbruch.
    """
    morgen = (jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
              + timedelta(days=2))
    meldungen: list[str] = []
    beste, beste_quelle = None, None

    def besser(punkte) -> bool:
        if not punkte:
            return False
        if beste is None:
            return True
        return max(p.ende for p in punkte) > max(p.ende for p in beste)

    quellen = []
    if token:
        quellen.append(("entsoe",
                        lambda: entsoe_hole(zone_eic, token, stunden=STUNDEN,
                                            jetzt=jetzt)))
    else:
        meldungen.append("entsoe: uebersprungen (kein Token)")

    bzn = ec.zone_fuer(zone_eic)
    if bzn is None:
        meldungen.append(f"energy-charts: uebersprungen (Zone {zone_eic!r} "
                         "unbekannt)")
    else:
        quellen.append(("energy-charts",
                        lambda: ec.hole(bzn, stunden=STUNDEN, jetzt=jetzt)))

    for name, holen in quellen:
        try:
            punkte = holen()
            # **Vor dem Vergleich pruefen.** Eine unplausible Reihe darf nicht
            # gewinnen, nur weil sie weiter reicht.
            pruefe_plausibel(punkte)
        except Exception as fehler:
            meldungen.append(f"{name}: {fehler}")
            continue
        if not punkte:
            meldungen.append(f"{name}: keine Punkte")
            continue
        if besser(punkte):
            beste, beste_quelle = punkte, name
            meldungen.append(f"{name}: {len(punkte)} Punkte")
        else:
            meldungen.append(f"{name}: {len(punkte)} Punkte, "
                             "vorhandene reicht weiter")
        if beste is not None and max(p.ende for p in beste) >= morgen:
            break

    return beste, beste_quelle, meldungen


def schreibe(ziel: pathlib.Path, zone: str, punkte, quelle: str,
             jetzt: datetime, geplant: str | None) -> bool:
    """Ablegen — **nur, wenn es die vorhandene Datei verbessert**."""
    ordner = ziel / zone
    datei, meta = ordner / "spot.json", ordner / "meta.json"

    neu_bis = max(p.ende for p in punkte)
    alt = _deckt_bis(_lies(datei))
    if alt is not None and neu_bis < alt:
        print(f"  {zone}: vorhandene Reihe reicht weiter ({alt.isoformat()})")
        return False

    inhalt = json.dumps(_als_json(punkte), indent=1)
    if datei.exists() and datei.read_text("utf-8") == inhalt:
        print(f"  {zone}: unveraendert")
        return False

    ordner.mkdir(parents=True, exist_ok=True)
    datei.write_text(inhalt, encoding="utf-8")
    meta.write_text(json.dumps({
        "zone": zone,
        "quelle": quelle,
        # **Beide Zeiten, und das ist Absicht.** Der Verzug ist ihre
        # Differenz, und nachtraeglich ist er nicht mehr rekonstruierbar:
        # GitHub Actions verzoegert Laeufe bei Last und verwirft sie
        # gelegentlich still.
        "erzeugt_am": jetzt.isoformat(),
        "lauf_geplant_fuer": geplant,
        "punkte": len(punkte),
        "deckt_von": min(p.beginn for p in punkte).isoformat(),
        "deckt_bis": neu_bis.isoformat(),
        "einheit": "EUR/kWh",
    }, indent=1), encoding="utf-8")
    print(f"  {zone}: {len(punkte)} Punkte bis {neu_bis.isoformat()} "
          f"({quelle}) geschrieben")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ziel", default="preise")
    ap.add_argument("--zonen", default="zonen.json")
    args = ap.parse_args(argv)

    token = os.environ.get("ENTSOE_TOKEN") or None
    jetzt = datetime.now(timezone.utc)
    # **Der geplante Zeitpunkt wird aus dem Cron-Ausdruck abgeleitet.**
    # GitHub gibt ihn nicht her — `github.event.schedule` ist der Ausdruck,
    # kein Zeitstempel. Nachtraeglich ist der Verzug nicht mehr
    # rekonstruierbar, also muss er hier entstehen.
    geplant = geplanter_lauf(os.environ.get("LAUF_CRON") or "", jetzt)

    zonen = json.loads(pathlib.Path(args.zonen).read_text("utf-8"))
    ziel = pathlib.Path(args.ziel)

    geaendert = False
    uebersicht = []
    for eintrag in zonen.get("zonen") or []:
        zone, eic = eintrag["name"], eintrag["eic"]
        print(f"{zone} ({eic}):")
        punkte, quelle, meldungen = hole_zone(eic, token, jetzt)
        for m in meldungen:
            print(f"  {m}")
        if not punkte:
            print(f"  {zone}: nichts geholt — die vorhandene Datei bleibt")
            uebersicht.append({"zone": zone, "ok": False,
                               "meldungen": meldungen})
            continue
        if schreibe(ziel, zone, punkte, quelle, jetzt, geplant):
            geaendert = True
        uebersicht.append({"zone": zone, "ok": True, "quelle": quelle,
                           "meldungen": meldungen})

    # Die Zonenliste ist selbst ein Artefakt: eine Box soll nachsehen koennen,
    # was es gibt, ohne das Repository zu kennen.
    ziel.mkdir(parents=True, exist_ok=True)
    liste = json.dumps({"zonen": [e["name"] for e in
                                  (zonen.get("zonen") or [])]}, indent=1)
    if not (ziel / "zonen.json").exists() or \
            (ziel / "zonen.json").read_text("utf-8") != liste:
        (ziel / "zonen.json").write_text(liste, encoding="utf-8")
        geaendert = True

    print("\nGEAENDERT" if geaendert else "\nunveraendert")
    # **Kein Fehlercode, wenn nichts zu holen war.** Ein roter Lauf jedes Mal,
    # wenn eine Quelle schweigt, waere eine Meldung, die man uebersieht — die
    # Datei bleibt ja gueltig. Rot wird es nur, wenn NICHTS mehr geht und auch
    # keine alte Datei da ist.
    ohne_datei = [e["zone"] for e in uebersicht
                  if not e["ok"] and not (ziel / e["zone"] / "spot.json").exists()]
    if ohne_datei:
        print(f"FEHLER: keine Daten und keine Datei fuer {ohne_datei}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

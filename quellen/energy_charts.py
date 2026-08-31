"""Day-Ahead-Preise von Energy-Charts — die zweite Quelle.

**Der Anlass** (31.08.2026). ENTSO-E antwortete zwei Tage lang mit 503 und
lieferte statt der Schnittstelle die HTML-Seite der Transparency Platform.
Vier Versuche, vier Fehler. Die Datei trug uns bis Mitternacht; danach faellt
der Plan auf den laufenden Tag zusammen.

**Energy-Charts (Fraunhofer ISE) hat dieselben Boersenpreise**, braucht keine
Anmeldung und war am Samstag verfuegbar, als ENTSO-E es nicht war. Es ist
damit keine zweite Meinung, sondern derselbe Wert aus einer anderen Leitung —
und genau das macht es als Rueckfall brauchbar.

## Was gemessen ist, nicht angenommen

    price?bzn=AT              96 Punkte, 900 s Schritt, „EUR / MWh"
    price?bzn=AT&start=…&end=…  dieselbe Menge, solange morgen nicht
                                veroeffentlicht ist

**Die Umrechnung ist dieselbe wie bei ENTSO-E**: EUR/MWh geteilt durch 1000.
Die Antwort sagt es selbst im Feld `unit`, und das wird **geprueft** — eine
Quelle, die eines Tages EUR/kWh liefert, waere sonst um den Faktor 1000
daneben, und niemand saehe es.

**Die Schrittweite wird abgeleitet, nicht gesetzt.** Sie steht nicht in der
Antwort; sie ergibt sich aus dem Abstand der Zeitstempel. Eine feste 900 —
wie in evccs Vorlage — waere auf die Viertelstundenzone geeicht und in einer
Stundenzone still falsch. Dieselbe Klasse wie `stufenBreiteH` in der
Kundenansicht.

**Und die Quelle drosselt** (HTTP 429, gemessen bei vier Anfragen in zwei
Sekunden). Ein 429 ist deshalb kein Fehler wie jeder andere: er bekommt eine
eigene Ruhezeit, statt im naechsten Durchlauf sofort wiederzukommen.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from .entsoe import Preispunkt

log = logging.getLogger(__name__)

BASIS_URL = "https://api.energy-charts.info/price"

#: Die Antwort nennt ihre Einheit selbst. Alles andere wird abgelehnt.
ERWARTETE_EINHEIT = "eur / mwh"

#: Wie lange nach einem 429 nicht wieder gefragt wird.
DROSSEL_RUHE_S = 900.0

#: EIC-Code (ENTSO-E) zu Gebotszone (Energy-Charts). Nur was belegt ist —
#: **geraten wird nicht**, denn eine falsche Zone liefert plausible Preise
#: eines fremden Marktes, und das faellt nie auf.
ZONEN = {
    "10YAT-APG------L": "AT",
    "10Y1001A1001A83F": "DE-LU",
    "10Y1001A1001A82H": "DE-LU",
    "10YCH-SWISSGRIDZ": "CH",
    "10YCZ-CEPS-----N": "CZ",
    "10YHU-MAVIR----U": "HU",
    "10YSI-ELES-----O": "SI",
    "10YIT-GRTN-----B": "IT-North",
    "10YSK-SEPS-----K": "SK",
    "10YPL-AREA-----S": "PL",
}


class EnergyChartsFehler(Exception):
    """Der Abruf ist gescheitert."""


class Gedrosselt(EnergyChartsFehler):
    """HTTP 429 — die Quelle bittet um Ruhe. Ein eigener Zustand."""


def zone_fuer(eic: str) -> str | None:
    """Gebotszone zum EIC-Code — `None`, wenn unbekannt.

    **Kein Rueckfall auf einen Vorgabewert.** Eine falsche Zone liefert
    plausible Preise eines fremden Marktes; das faellt weder im Log noch in
    der Anzeige auf und verfaelscht jede Planung.
    """
    return ZONEN.get((eic or "").strip().upper())


def auswerten(roh: dict[str, Any]) -> list[Preispunkt]:
    """Die Antwort in Preispunkte. Prueft die Einheit und leitet den Schritt ab."""
    einheit = str(roh.get("unit") or "").strip().lower()
    if einheit and einheit != ERWARTETE_EINHEIT:
        raise EnergyChartsFehler(
            f"unerwartete Einheit {roh.get('unit')!r} — erwartet EUR/MWh")

    sekunden = roh.get("unix_seconds")
    preise = roh.get("price")
    if not isinstance(sekunden, list) or not isinstance(preise, list):
        raise EnergyChartsFehler("Antwort ohne unix_seconds/price")
    if not sekunden:
        return []
    paare = [(int(s), float(p)) for s, p in zip(sekunden, preise)
             if isinstance(s, (int, float)) and isinstance(p, (int, float))]
    if not paare:
        return []
    paare.sort()

    # **Der Schritt kommt aus den Daten.** Der haeufigste Abstand, nicht der
    # erste — eine einzelne Luecke soll ihn nicht bestimmen.
    abstaende = [b[0] - a[0] for a, b in zip(paare, paare[1:]) if b[0] > a[0]]
    if abstaende:
        schritt = max(set(abstaende), key=abstaende.count)
    else:
        schritt = 900
    if schritt <= 0:
        raise EnergyChartsFehler("unbrauchbare Zeitstempel")

    raus: list[Preispunkt] = []
    for i, (s, p) in enumerate(paare):
        dauer = (paare[i + 1][0] - s) if i + 1 < len(paare) else schritt
        if dauer <= 0 or dauer > 2 * schritt:
            dauer = schritt
        beginn = datetime.fromtimestamp(s, tz=timezone.utc)
        raus.append(Preispunkt(beginn, beginn + timedelta(seconds=dauer),
                               p / 1000.0))
    return raus


def hole(zone: str, *, stunden: int = 48, jetzt: datetime | None = None,
         oeffner: Any = None) -> list[Preispunkt]:
    """Preise fuer die naechsten `stunden`. Wirft `EnergyChartsFehler`.

    `zone` ist die **Gebotszone** (`AT`), nicht der EIC-Code — `zone_fuer()`
    uebersetzt.
    """
    jetzt = jetzt or datetime.now(timezone.utc)
    von = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    bis = von + timedelta(hours=stunden)
    ziel = f"{BASIS_URL}?" + urllib.parse.urlencode({
        "bzn": zone,
        "start": von.strftime("%Y-%m-%d"),
        "end": bis.strftime("%Y-%m-%d"),
    })
    anfrage = urllib.request.Request(ziel, headers={"Accept": "application/json"})
    oeffnen = oeffner or urllib.request.urlopen
    try:
        with oeffnen(anfrage, timeout=30) as antwort:
            roh = json.loads(antwort.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as fehler:
        if fehler.code == 429:
            raise Gedrosselt("HTTP 429") from None
        raise EnergyChartsFehler(f"HTTP {fehler.code}") from None
    except json.JSONDecodeError:
        # **Eine HTML-Seite statt JSON ist genau der Fall von ENTSO-E.**
        raise EnergyChartsFehler("Antwort ist kein JSON") from None
    except Exception as fehler:
        raise EnergyChartsFehler(f"{type(fehler).__name__}") from None
    return auswerten(roh)

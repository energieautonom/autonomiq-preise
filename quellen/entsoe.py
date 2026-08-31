"""Day-Ahead-Preise direkt bei ENTSO-E holen.

**Warum der Agent das selbst tut, und nicht evcc fuer ihn** (30.08.2026).

evcc verwirft einen Tarif, dessen **erster** Abruf scheitert
(`tariff/helper.go:84`), und die Goroutine beendet sich — kein zweiter
Versuch, bis jemand neu startet. Gemessen an beiden Boxen:

    WARN planner: tariff not available: unexpected status: 503
    (Service Unavailable) GET https://web-api.tp.entsoe.eu/api?...

Die Abhilfe ist eine **lokale Datei**, aus der evcc liest — sie antwortet
immer, also stirbt der Tarif beim Start nicht mehr. Damit muss aber jemand
anderes die Datei fuellen, und dieser jemand kann nicht evcc sein:

    evcc liest aus der Datei  ->  Agent speichert, was evcc meldet
      ->  die Datei bekommt nie neue Preise

Das ist der Fixpunkt „tue nichts" in Reinform: jeder Schritt richtig, die
Schleife falsch. **Die Quelle muss ausserhalb liegen**, und das ist ENTSO-E.

**`merged` traegt es nicht** — geprueft, nicht angenommen. evccs
`NewMergedFromConfig` baut **beide** Kinder mit `NewFromConfig`; scheitert
eines beim Start, scheitert der Verbund. Es fuellt Luecken zur Laufzeit und
schuetzt nicht vor dem Startfehler.

## Was hier bewusst NICHT passiert

**Keine Preislogik.** Aufschlaege, Netzentgelte und Steuern rechnet evcc ueber
`embed` (`charges`, `tax`, `formula`) — je Kunde konfiguriert. Hier kommt der
**Boersenpreis der Zone** heraus, in EUR/kWh, und sonst nichts. Alles andere
waere Kundenlogik an der falschen Stelle.

**Kein Ersatzwert.** Faellt der Abruf aus, gibt es `None` — nicht eine leere
Liste und nicht den letzten Wert. Was auf der Platte liegt, entscheidet die
Datei (`preisdatei.py`), nicht dieser Abruf.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

log = logging.getLogger(__name__)

BASIS_URL = "https://web-api.tp.entsoe.eu/api"

#: A44 = Day-Ahead-Preise. Dasselbe Dokument, das evcc anfragt.
DOKUMENTTYP = "A44"

#: ENTSO-E nimmt Zeiten ohne Zonenangabe — sie sind **UTC**.
ZEITFORMAT = "%Y%m%d%H%M"

#: Aufloesungen, die vorkommen. Der Wert ist die Schrittdauer in Minuten.
AUFLOESUNG = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT1H": 60}


@dataclass(frozen=True)
class Preispunkt:
    beginn: datetime
    ende: datetime
    #: EUR je kWh. ENTSO-E liefert EUR/MWh.
    wert: float


class EntsoeFehler(Exception):
    """Der Abruf ist gescheitert. Traegt **keinen** Token im Text."""


def _url(zone: str, von: datetime, bis: datetime) -> str:
    p = urllib.parse.urlencode({
        "documentType": DOKUMENTTYP,
        "in_Domain": zone,
        "out_Domain": zone,
        "periodStart": von.astimezone(timezone.utc).strftime(ZEITFORMAT),
        "periodEnd": bis.astimezone(timezone.utc).strftime(ZEITFORMAT),
    })
    return f"{BASIS_URL}?{p}"


def _ohne_namensraum(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


#: Reihenfolge der Aufloesungen, wenn ENTSO-E fuer denselben Zeitraum mehrere
#: liefert: die feinste gewinnt. Waehrend der Umstellung auf
#: Viertelstundenprodukte kommen PT60M und PT15M nebeneinander vor.
AUFLOESUNG_VORZUG = ("PT15M", "PT30M", "PT60M", "PT1H")


def _zeitraum(period) -> tuple[str, str] | None:
    intervall = next((k for k in period
                      if _ohne_namensraum(k.tag) == "timeInterval"), None)
    if intervall is None:
        return None
    von = next((k.text for k in intervall
                if _ohne_namensraum(k.tag) == "start"), None)
    bis = next((k.text for k in intervall
                if _ohne_namensraum(k.tag) == "end"), None)
    return (von, bis) if von and bis else None


def _punkte(period, minuten: int, beginn: datetime) -> list[Preispunkt]:
    """Die Punkte eines Zeitraums — fehlende Positionen wiederholen die vorige.

    ENTSO-E laesst unveraenderte Punkte weg; wer das nicht nachbildet, bekommt
    Luecken, wo keine sind (dieselbe Regel wie in evccs
    `ExtractPeriodPriceData`).
    """
    nach_position: dict[int, float] = {}
    for punkt in period:
        if _ohne_namensraum(punkt.tag) != "Point":
            continue
        felder = {_ohne_namensraum(k.tag): (k.text or "") for k in punkt}
        try:
            nach_position[int(felder["position"])] = float(felder["price.amount"])
        except (KeyError, ValueError):
            continue
    if not nach_position:
        return []

    schritt = timedelta(minutes=minuten)
    raus: list[Preispunkt] = []
    letzter: float | None = None
    for pos in range(1, max(nach_position) + 1):
        if pos in nach_position:
            letzter = nach_position[pos]
        if letzter is None:
            continue
        ab = beginn + (pos - 1) * schritt
        raus.append(Preispunkt(ab, ab + schritt, letzter / 1000.0))
    return raus


def auswerten(xml_text: str) -> list[Preispunkt]:
    """Ein A44-Dokument in Preispunkte.

    **ENTSO-E liefert fuer denselben Zeitraum mehrere `TimeSeries`**, und wer
    sie aneinanderhaengt, bekommt doppelte Zeitstempel mit **verschiedenen
    Werten**. Gemessen am 30.08.2026 an der Referenzanlage: 384 Punkte fuer
    48 Stunden, davon 96 Startzeiten doppelt bis dreifach belegt (0,16784
    gegen 0,20060 EUR/kWh fuer dieselbe Viertelstunde).

    Die Unterscheidung steht im Dokument, und evcc benutzt sie
    (`tariff/entsoe/api.go:55`): **`classificationSequence_…position`, und die
    NIEDRIGSTE gewinnt** je Zeitraum. Dazu die Aufloesung — bei mehreren fuer
    denselben Zeitraum die feinste, denn waehrend der Umstellung auf
    Viertelstundenprodukte kommen PT60M und PT15M nebeneinander vor.

    **Die erste Fassung hat beides ignoriert.** Sie sah richtig aus, lieferte
    Preise, und die Datei war trotzdem falsch — der Fehler faellt erst auf,
    wenn jemand die Zeitstempel zaehlt.
    """
    wurzel = ET.fromstring(xml_text)
    if _ohne_namensraum(wurzel.tag) == "Acknowledgement_MarketDocument":
        grund = next((e.text for e in wurzel.iter()
                      if _ohne_namensraum(e.tag) == "text"), "ohne Angabe")
        raise EntsoeFehler(f"ENTSO-E lehnt ab: {grund}")

    # je Zeitraum der beste Kandidat: (Position, Aufloesungsrang, Punkte)
    beste: dict[tuple[str, str], tuple[int, int, list[Preispunkt]]] = {}

    for reihe in wurzel.iter():
        if _ohne_namensraum(reihe.tag) != "TimeSeries":
            continue
        position = 0
        for kind in reihe:
            if _ohne_namensraum(kind.tag).startswith("classificationSequence"):
                try:
                    position = int((kind.text or "0").strip())
                except ValueError:
                    position = 0
        for period in reihe:
            if _ohne_namensraum(period.tag) != "Period":
                continue
            zeitraum = _zeitraum(period)
            if zeitraum is None:
                continue
            roh_aufl = next((k.text or "" for k in period
                             if _ohne_namensraum(k.tag) == "resolution"), "")
            aufl = roh_aufl.strip()
            minuten = AUFLOESUNG.get(aufl)
            if minuten is None:
                log.warning("ENTSO-E: unbekannte Aufloesung %r — Zeitraum "
                            "uebersprungen", aufl)
                continue
            rang = (AUFLOESUNG_VORZUG.index(aufl)
                    if aufl in AUFLOESUNG_VORZUG else len(AUFLOESUNG_VORZUG))
            try:
                beginn = datetime.fromisoformat(
                    zeitraum[0].replace("Z", "+00:00"))
            except ValueError:
                continue
            punkte = _punkte(period, minuten, beginn)
            if not punkte:
                continue
            vorher = beste.get(zeitraum)
            # **Niedrigste Position gewinnt**, bei Gleichstand die feinere
            # Aufloesung. Kein „der letzte gewinnt" — das waere die
            # Reihenfolge im Dokument, und die sagt nichts.
            if vorher is None or (position, rang) < (vorher[0], vorher[1]):
                beste[zeitraum] = (position, rang, punkte)

    raus: list[Preispunkt] = []
    for _, _, punkte in beste.values():
        raus.extend(punkte)
    raus.sort(key=lambda p: p.beginn)
    return raus


def hole(zone: str, token: str, *, stunden: int = 48,
         jetzt: datetime | None = None, oeffner: Any = None) -> list[Preispunkt]:
    """Preise fuer die naechsten `stunden`. Wirft `EntsoeFehler`.

    **Der Token steht in der URL und darf in keiner Meldung auftauchen.** evcc
    redigiert ihn fuer seinen `entsoe`-Logger, aber nicht im Planer — dort
    stand er am 30.08.2026 im Klartext im Log. Hier wird die URL nie
    protokolliert.
    """
    jetzt = jetzt or datetime.now(timezone.utc)
    # Volle Stunde zurueck, damit der laufende Zeitschritt mitkommt.
    von = jetzt.replace(minute=0, second=0, microsecond=0)
    ziel = _url(zone, von, von + timedelta(hours=stunden))
    anfrage = urllib.request.Request(
        ziel + "&" + urllib.parse.urlencode({"securityToken": token}),
        headers={"Accept": "application/xml"})
    oeffnen = oeffner or urllib.request.urlopen
    try:
        with oeffnen(anfrage, timeout=30) as antwort:
            roh = antwort.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as fehler:
        # **Ohne URL.** Sie traegt den Token.
        raise EntsoeFehler(f"HTTP {fehler.code}") from None
    except Exception as fehler:
        raise EntsoeFehler(f"{type(fehler).__name__}") from None
    return auswerten(roh)


def auf_stunden(punkte: Sequence[Preispunkt]) -> list[Preispunkt]:
    """Viertelstundenpreise zu Stundenmitteln zusammenfassen.

    **Weil evcc es nicht kann** (geprueft 30.08.2026). `SlotWrapper`
    (`tariff/slots.go:15`) **entfaltet** laengere Zeitschritte in
    Viertelstunden — „For price tariffs, the value is constant over all
    sub-slots" —, aber es gibt keinen Weg in die Gegenrichtung und keine
    Einstellung dafuer. Wer stuendlich abgerechnet wird, muss es vorher
    rechnen.

    **Das ist eine Vertragsgroesse, keine Dateneigenschaft.** Die zweite Box
    rechnet die Einspeisung stuendlich ab, die Referenzanlage
    viertelstuendlich — dieselbe Boerse, dieselbe Zone, zwei Vertraege.

    Gemittelt wird **arithmetisch ueber die Kalenderstunde**, denn genau das
    ist die Abrechnung: alle Viertelstunden einer Stunde zaehlen gleich viel.
    Eine angeschnittene Stunde am Rand wird ueber das gemittelt, was da ist —
    nicht verworfen, sonst fehlte der laufende Zeitschritt.
    """
    nach_stunde: dict[datetime, list[Preispunkt]] = {}
    for p in punkte:
        schluessel = p.beginn.replace(minute=0, second=0, microsecond=0)
        nach_stunde.setdefault(schluessel, []).append(p)

    raus: list[Preispunkt] = []
    for beginn in sorted(nach_stunde):
        teile = nach_stunde[beginn]
        mittel = sum(x.wert for x in teile) / len(teile)
        raus.append(Preispunkt(beginn, max(x.ende for x in teile), mittel))
    return raus


#: Was ein Boersenpreis in EUR/kWh hoechstens und mindestens sein kann.
#:
#: **Keine wirtschaftliche Schwelle, sondern eine Schranke gegen kaputte
#: Daten.** Negative Preise sind echt (Ueberschuss im Netz), Spitzen ueber
#: 1 EUR/kWh auch. Was darueber hinausgeht, ist ein Fehler und keine
#: Marktlage — ein Faktor 1000 aus einer verwechselten Einheit landet
#: zuverlaessig ausserhalb.
#:
#: **Sie wird wichtig, sobald eine Quelle geteilt wird** (Entwurf
#: `docs/entwurf-zentraler-preisdienst.md`): solange jede Box selbst holt,
#: trifft ein Fehler eine Box; mit einem gemeinsamen Dienst trifft er alle
#: zugleich.
PREIS_MIN_EUR_KWH = -1.0
PREIS_MAX_EUR_KWH = 10.0


def pruefe_plausibel(punkte: Sequence[Preispunkt]) -> None:
    """Wirft, wenn ein Wert ausserhalb der Schranke liegt.

    **Die ganze Reihe faellt, nicht nur der Punkt.** Ein einzelner
    unmoeglicher Wert sagt, dass die Quelle oder die Umrechnung nicht stimmt;
    die uebrigen Werte derselben Antwort sind dann nicht vertrauenswuerdiger,
    nur unauffaelliger.
    """
    for p in punkte:
        if not (PREIS_MIN_EUR_KWH <= p.wert <= PREIS_MAX_EUR_KWH):
            raise EntsoeFehler(
                f"unplausibler Preis {p.wert:.4f} EUR/kWh um "
                f"{p.beginn.isoformat()} — erlaubt sind "
                f"{PREIS_MIN_EUR_KWH} bis {PREIS_MAX_EUR_KWH}")

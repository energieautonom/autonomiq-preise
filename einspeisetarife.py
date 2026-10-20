"""Einspeisetarife, die sich regelmaessig aendern — zentral geholt (20.10.2026).

**Von Hand nachtragen ist keine Loesung**: wird es einmal vergessen, rechnen
alle Boxen eines Netzbereichs mit einem alten Wert. Hier holt der Dienst die
Werte einmal, und die Boxen lesen `katalog/einspeisung.json` — dieselbe
Bauform wie die Boersenpreise.

| Eintrag | Quelle | Rhythmus |
|---|---|---|
| `oemag` | Tabelle „Marktpreise <Jahr>" auf oem-ag.at/marktpreis | monatlich, im Nachhinein |
| `ikb-pv` | Liste „Qn JJJJ (…) x,xx Cent/kWh" auf ikb.at | quartalsweise, vor Quartalsbeginn |
| `tiwag-pv` | **dieselbe Formel** wie IKB (EEX Grundlast Quartalsfuture Phelix-AT × 80 %, letzte fuenf Handelstage des Vorquartals) | wie IKB |

**TIWAG ist von hier aus nicht lesbar**: die Seite steht hinter einer
Cookie-Zustimmung, die nur im Browser per Skript laeuft — die wird nicht
umgangen. Weil beide Anbieter dieselbe Formel veroeffentlichen und alle vier
Quartale 2026 gleich sind (gelesen 20.10.2026), traegt `tiwag-pv` den
IKB-Wert mit `abgeleitet_aus: ikb-pv`. Ist TIWAG eines Tages lesbar, wird
verglichen: weichen beide ab, wird fuer TIWAG **nichts** veroeffentlicht.

**Selbst rechnen geht nicht ohne Lizenz**: die EEX-Abrechnungspreise der
Quartalsfutures gibt es nur ueber ihre Datendienste. Die Gegenprobe ist
deshalb die zweite Veroeffentlichung, nicht eine eigene Rechnung.

**Nie schlechter**: ein vorhandener Wert verschwindet nie, weil eine Seite
ihn nicht mehr zeigt (IKB listet nur die letzten Quartale). Ein Wert ausser
0–50 ct kippt die ganze Antwort dieser Quelle — ein unmoeglicher Wert sagt,
dass das Lesen nicht stimmt.
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import urllib.request
from datetime import datetime
from typing import Callable

QUELLEN = {
    "oemag": "https://www.oem-ag.at/marktpreis",
    "ikb-pv": "https://www.ikb.at/energie/photovoltaik/einspeisevertrag",
    "tiwag-pv": "https://www.tiwag.at/privat/photovoltaik/tiwag-pv-einspeisung/",
}
DATEI = pathlib.Path("katalog") / "einspeisung.json"
GRENZEN_CT = (0.0, 50.0)
MONATE = {"Jänner": 1, "Januar": 1, "Februar": 2, "März": 3, "April": 4, "Mai": 5,
          "Juni": 6, "Juli": 7, "August": 8, "September": 9, "Oktober": 10,
          "November": 11, "Dezember": 12}


class LeseFehler(Exception):
    """Die Seite liess sich nicht lesen oder lieferte Unplausibles."""


def _text(roh: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", roh)))


def _zahl(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _pruefe(werte: dict[str, float], was: str) -> dict[str, float]:
    if not werte:
        raise LeseFehler(f"{was}: keine Werte gefunden")
    lo, hi = GRENZEN_CT
    for k, v in werte.items():
        if not lo < v < hi:
            raise LeseFehler(f"{was}: {k} = {v} ct liegt nicht zwischen {lo} und {hi}")
    return werte


def lies_quartale(roh: str, was: str) -> dict[str, float]:
    """`Q4 2026 (01.10.2026 – 31.12.2026) 15,31 Cent/kWh` -> `{"2026-Q4": 15.31}`."""
    werte: dict[str, float] = {}
    for q, jahr, ct in re.findall(
            r"Q([1-4])\s*(20\d\d)\s*\([^)]*\)\s*(\d+,\d+)\s*Cent/kWh", _text(roh)):
        schluessel = f"{jahr}-Q{q}"
        if schluessel in werte and werte[schluessel] != _zahl(ct):
            raise LeseFehler(f"{was}: {schluessel} zweimal mit verschiedenen Werten")
        werte[schluessel] = _zahl(ct)
    return _pruefe(werte, was)


def lies_oemag(roh: str) -> dict[str, float]:
    """Die Tabelle „Marktpreise <Jahr>": erste Zahl je Monat ist Photovoltaik."""
    text = _text(roh)
    m = re.search(r"Marktpreise (20\d\d) Monat", text)
    if not m:
        raise LeseFehler("oemag: Tabellenkopf „Marktpreise <Jahr> Monat“ nicht gefunden")
    jahr = int(m.group(1))
    tabelle = text[m.end():]
    werte: dict[str, float] = {}
    for name, ct in re.findall(
            r"(Jänner|Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|"
            r"November|Dezember) (\d+,\d+) ct/kWh", tabelle):
        schluessel = f"{jahr}-{MONATE[name]:02d}"
        werte.setdefault(schluessel, _zahl(ct))      # erste Spalte = PV
    return _pruefe(werte, "oemag")


def _hole(url: str) -> str:
    anfrage = urllib.request.Request(url, headers={"User-Agent": "AutonomIQ-preisdienst/1.0"})
    with urllib.request.urlopen(anfrage, timeout=30) as antwort:
        return antwort.read().decode("utf-8", errors="replace")


def _lies(pfad: pathlib.Path) -> dict:
    try:
        return json.loads(pfad.read_text("utf-8"))
    except (OSError, ValueError):
        return {"eintraege": {}}


def aktualisiere(ziel: pathlib.Path, jetzt: datetime,
                 holen: Callable[[str], str] = _hole) -> tuple[bool, list[str]]:
    """Holen, pruefen, zusammenfuehren. (geaendert, Meldungen)."""
    pfad = ziel / DATEI
    alt = _lies(pfad)
    neu = json.loads(json.dumps(alt))                     # tiefe Kopie
    eintraege = neu.setdefault("eintraege", {})
    meldungen: list[str] = []

    def uebernimm(name: str, feld: str, werte: dict[str, float], **mehr) -> None:
        e = eintraege.setdefault(name, {})
        vorher = dict(e.get(feld) or {})
        e[feld] = {**vorher, **werte}                     # nie schlechter
        e.update(quelle=QUELLEN.get(mehr.pop("quelle_von", name)), **mehr)
        if e[feld] != vorher:
            e["geaendert_am"] = jetzt.isoformat(timespec="seconds")
            meldungen.append(f"{name}: {sorted(set(e[feld].items()) - set(vorher.items()))}")

    try:
        uebernimm("oemag", "monate", lies_oemag(holen(QUELLEN["oemag"])))
    except Exception as f:                                # noqa: BLE001
        meldungen.append(f"oemag: nicht gelesen — {f}")
    ikb = None
    try:
        ikb = lies_quartale(holen(QUELLEN["ikb-pv"]), "ikb-pv")
        uebernimm("ikb-pv", "quartale", ikb)
    except Exception as f:                                # noqa: BLE001
        meldungen.append(f"ikb-pv: nicht gelesen — {f}")
    try:
        tiwag = lies_quartale(holen(QUELLEN["tiwag-pv"]), "tiwag-pv")
    except Exception as f:                                # noqa: BLE001
        tiwag = None
        meldungen.append(f"tiwag-pv: nicht lesbar ({f}) — abgeleitet aus ikb-pv")
    if tiwag is not None:
        uneinig = sorted(k for k in tiwag if ikb and k in ikb and abs(tiwag[k] - ikb[k]) > 0.005)
        for k in uneinig:
            meldungen.append(f"UNEINIG {k}: tiwag {tiwag[k]} gegen ikb {ikb[k]} — "
                             "fuer TIWAG nichts veroeffentlicht")
        uebernimm("tiwag-pv", "quartale", {k: v for k, v in tiwag.items() if k not in uneinig},
                  abgeleitet_aus=None, uneinig=uneinig or None)
    elif ikb is not None:
        uebernimm("tiwag-pv", "quartale", ikb, quelle_von="ikb-pv", abgeleitet_aus="ikb-pv")

    if neu != alt:
        neu["erzeugt_am"] = jetzt.isoformat(timespec="seconds")
        pfad.parent.mkdir(parents=True, exist_ok=True)
        tmp = pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(neu, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
        tmp.replace(pfad)
        return True, meldungen
    return False, meldungen

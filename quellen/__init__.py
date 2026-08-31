"""Die Preisquellen — **byteweise Kopien** aus dem Box-Repository.

Sie liegen hier, damit der Dienst ohne das Box-Repository laeuft. Geaendert
wird **dort**, nicht hier: `agent/autonomiq_agent/entsoe.py` und
`energy_charts.py` sind die Quelle, diese Dateien sind die Kopie.

`agent/tests/test_preisdienst_kopie.py` im Box-Repository haelt beide
identisch — eine Abweichung laesst den Test fallen. Ohne das waere die
Entduplizierungsregel (`classificationSequence`, niedrigste Position) an zwei
Orten gepflegt, und einer davon liefe irgendwann hinterher.
"""

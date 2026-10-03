# ADR-0025: Stabile Modulidentität und editierbare Anzeigenamen

Status: umgesetzt im Arbeitsbranch, 2026-10-03.

`PlaneConfig.name` steckt bereits in Shademap-Kanälen, elektrischen Gruppen,
physikalischen Ergebnissen, issued Archiven und externen Aktionsparametern.
Das Ersetzen durch neue UUIDs würde sämtliche Referenzen und Legacy-Pools
migrieren müssen. Ein partieller Wechsel könnte Lernen falsch zuordnen.

Wir behalten `name` als stabile ID und ergänzen optional `display_name`.
`PlaneConfig.label` fällt für Alt-Konfigurationen auf die bisherige ID zurück.
Die ID entsteht beim Anlegen aus dem gewählten ursprünglichen Namen; spätere
Anzeigeänderungen verwenden ausschließlich `display_name`. Gruppen, Dienste
und Archivschlüssel behalten ihre bisherigen IDs. Neue Anzeigeattribute sind
additiv; das optionale Config-Feld wird nur gesetzt serialisiert.

Modellfingerprint und Bootstrap-Signatur ignorieren den Anzeigenamen. Die
Rekonfigurationsvorschau meldet eine Anzeigeänderung ohne Modelländerung.
Jedes effektive Label muss eindeutig sein und 1–100 Zeichen ohne Steuerzeichen
enthalten. Das vermeidet mehrdeutige Auswahlwerte, auch bei gemischten Legacy-
und neuen Modulen.

Die HA-Modulauswahl zeigt Labels, übersetzt sie intern zu IDs und speichert
`module_id` im wiederherstellbaren Zustand. Ein später geändertes Label lässt
damit die Auswahl intakt. Alte Zustände ohne dieses Attribut sind weiterhin
über die ursprüngliche ID auflösbar. Power-Kartenquellen und Shade-Profilantwort
geben das Label weiter; der Aktionsparameter `module` bleibt eine ID.

Keine Store- oder Entry-Migration ist erforderlich: unveränderte Alt-Dicts,
Learner- und Archivschlüssel gehen bytegetreu weiter. Ein manuelles Ändern von
`name` bleibt ein bewusster Identitätswechsel mit Hinzufügen/Entfernen in der
Vorschau. Automatische Zuordnung anhand von Sensor oder ähnlicher Geometrie
unterbleibt, weil sie beim Modultausch eine falsche Kontinuität behaupten könnte.

Nachweis: Legacy-Roundtrip, unveränderte Physik/Fingerprints/Bootstrap-Signatur,
eindeutige Labels, ungültige Eingaben und Restore über eine Anzeigeumbenennung.

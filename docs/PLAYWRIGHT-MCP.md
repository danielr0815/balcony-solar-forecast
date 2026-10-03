# Playwright MCP im VM-Devcontainer

## Aufbau und einmalige Einrichtung

Codex läuft im Linux-Devcontainer. Der Playwright-MCP-Server startet dort einen
**eigenen sichtbaren Chromium-Browser auf dem Desktop der VM** über deren
weitergereichten Wayland-Socket. Er übernimmt keine bereits geöffnete Sitzung
eines anderen VM-Browsers. Der Benutzer meldet sich bei Bedarf selbst an HA an.

Die projektlokale `.codex/config.toml` registriert den Server `playwright` über
STDIO. Codex startet ihn bei Bedarf mit `scripts/playwright-mcp.sh`; ein separater
Daemon, SSH-Zugang, MCP-Plugin oder offener Netzwerkport ist nicht erforderlich.
Das Projekt muss in Codex als vertrauenswürdig gelten. VS Code aus der grafischen
VM-Sitzung starten und das Projekt im Devcontainer öffnen.

Im Projektverzeichnis einmal ausführen:

```bash
bash scripts/playwright-mcp.sh install
codex mcp list
```

Die Installation pinnt `@playwright/mcp` auf **0.0.82** und installiert genau den
zugehörigen Chromium-Build samt Linux-Systembibliotheken. Sie benötigt Netzwerk
und für die Systembibliotheken sudo im Devcontainer. Es kommen keine
Runtime-Abhängigkeiten zur HA-Integration hinzu; `uv.lock` bleibt davon unabhängig.

Danach in der **Codex-Erweiterung** die MCP-Einstellungen öffnen und
**Restart extension** ausführen; alternativ das VS-Code-Fenster neu laden.
Bereits laufende Unterhaltungen können noch die alte Werkzeugliste haben.
Ein erfolgreicher `codex mcp list`-Aufruf ersetzt diesen Neustart nicht.
Die Projektkonfiguration gehört zu Codex, nicht zu VS Codes separater
Copilot-MCP-Konfiguration (`.vscode/mcp.json`).

## Prüfung und tägliche Nutzung

Bei zwei VS-Code-Instanzen für jeden MCP-Prozess einen eigenen Namen setzen,
zum Beispiel `BSF_PLAYWRIGHT_INSTANCE=review` bzw. `BSF_PLAYWRIGHT_INSTANCE=dev`.
Den Wert in der jeweiligen MCP-Prozessumgebung setzen, bevor der Server startet.
Der Launcher verwendet dann `.ha-dev/playwright-profile-<Name>` und
`.playwright-mcp/<Name>`. Erlaubt sind Buchstaben, Zahlen, `_` und `-`.
Ohne Variable bleibt das bisherige Profil erhalten. Jedes neue Profil benötigt
seinen eigenen manuellen HA-Login. Laufende Browser und deren Locks bleiben
unangetastet; eine neue Variable trennt bereits gestartete Prozesse nicht nachträglich.

1. `codex mcp list` muss `playwright` als `enabled` aufführen.
   `Auth: Unsupported` ist für diesen lokalen STDIO-Server normal und sagt
   nichts über den HA-Login aus.
2. Über MCP `browser_tabs` mit `action: list` aufrufen. Vorhandenen HA-Tab nutzen;
   andernfalls mit `browser_navigate` die URL aus `.ha-dev/live-access.md` öffnen.
3. Der Browser erscheint auf dem VM-Desktop. Bei der HA-Anmeldeseite meldet sich
   der Benutzer manuell an. Keine Passwörter, Tokens, Cookies oder Local-Storage-
   Inhalte auslesen, exportieren oder in Notizen/Diagnoseprotokolle übernehmen.
4. Mit `browser_snapshot` prüfen, ob das HA-Dashboard erreichbar ist. Erst dann
   gilt der Live-Zugriff als angemeldet. Für Analysen ausschließlich beauftragte
   Messdaten/Diagnosen lesen; HA-Konfiguration nicht verändern.

Die Werkzeugnamen können je nach Client ein Präfix wie
`mcp__playwright__browser_tabs` tragen. Eine leere CUA-Browserliste ist kein
Playwright-Test. Ein TCP-Test auf 8931/9222 ist bei dieser STDIO-Lösung ebenfalls
kein Verfügbarkeitstest: sie verwendet keinen dieser Ports.

Das normale Browserprofil liegt unter `.ha-dev/playwright-profile/`, MCP und
Browser unter `.ha-dev/playwright-runtime/` bzw. `.ha-dev/playwright-browsers/`.
Diese Verzeichnisse liegen auf dem Projekt-Mount der VM und überleben deshalb
einen Container-Rebuild. Sie sind Git-ignoriert; das Profil enthält die normale
Anmeldung und darf weder committed noch als Analyse-Artefakt geteilt werden.
Automatische Browser-Ausgaben liegen unter `.playwright-mcp/` (ebenfalls ignoriert).
Der Launcher setzt für neu angelegte Dateien `umask 077`.

## Fehlerbehebung und Wiederherstellung

| Symptom | Ursache / Maßnahme |
|---|---|
| `No MCP servers configured` | Projektwurzel öffnen, `.codex/config.toml` und Projektvertrauen prüfen. Eine Host-Konfiguration ist nicht automatisch die Container-Konfiguration. |
| Server gelistet, Tools fehlen | Codex-Erweiterung neu starten; danach nochmals die tatsächliche Werkzeugliste prüfen. |
| `Playwright MCP fehlt` oder Browser-Binary fehlt | `bash scripts/playwright-mcp.sh install` ausführen. |
| Fehlende `.so`-Bibliotheken nach Rebuild | Ebenfalls den Installationsbefehl ausführen; Browserdateien bleiben erhalten, Systempakete gehören zum neuen Container. |
| `Authorization required` / `Missing X server` | Die weitergereichte X11-Anzeige kann ohne passende Xauthority unbrauchbar sein. Der Launcher bevorzugt Wayland mit `scripts/playwright-wayland.json` und löst VS Codes Socket unter `/tmp` auf. |
| Wayland-Socket fehlt oder ist nicht erreichbar | VS Code in der grafischen VM-Sitzung neu öffnen und den Devcontainer erneut verbinden. Keine wechselnden Socket-Namen oder `DISPLAY`-Nummern fest eintragen. |
| `No usable sandbox` | Die untersuchte VM verhindert verschachtelte User-Namespaces. Im Docker-Devcontainer setzt der Launcher deshalb `--no-sandbox`; auf einem nativen Host nicht. Damit fehlt die zusätzliche Chromium-Prozesssandbox. Diese Browserinstanz nur für die beauftragte HA-Analyse verwenden. Die VM-Sicherheitsrichtlinien werden nicht global verändert. |
| Profil bereits in Benutzung | Den anderen MCP-Client sauber beenden. Dasselbe Profil nicht gleichzeitig durch zwei Server öffnen; weder Profil noch Lockdateien eines laufenden Browsers löschen. |
| HA zeigt die Anmeldeseite | MCP funktioniert; HA ist in diesem Profil noch nicht angemeldet. Benutzer meldet sich im sichtbaren Browser an. |

Für ein neues Checkout oder nach einem Rebuild reichen die versionierten Dateien
und der Installationsbefehl oben. Es wird bewusst kein Browser für jeden Entwickler
beim allgemeinen `uv sync` installiert. Ein Update der MCP-Version erfolgt gezielt
im Launcher, anschließend Installation und Browser-Smoke-Test wiederholen.

## Verifikation am 26.09.2026

Vorher: keine MCP-Registrierung im Container, kein installierter Browser;
anschließend beim echten Start nachgewiesene Namespace- und X11-Fehler.
Nach Einrichtung: MCP-Handshake und Werkzeugauflistung erfolgreich,
`browser_tabs` liefert einen sichtbaren Browser-Tab; `browser_navigate` und
`browser_snapshot` erreichen Home Assistants Anmeldeseite über Wayland.
Der HA-Login ist damit **noch nicht** bestätigt. Die Codex-Erweiterung muss nach
der Einrichtung neu geladen werden, um den Server in ihrer Werkzeugliste zu laden.
Die HA-Integration und ihre SPEC ändern sich durch dieses Entwicklerwerkzeug nicht.

Quellen: [Codex MCP-Konfiguration](https://developers.openai.com/codex/mcp/),
[offizieller Playwright-MCP-Server](https://github.com/microsoft/playwright-mcp).

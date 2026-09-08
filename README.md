# netmon

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

Overvaager netvaerket ved at pinge et sat testservere med jaevne mellemrum, og logger
naar noget er nede. Skelner mellem **lokalt netvaerk nede** og **ISP/internet nede**.

Ingen dependencies - kun Python 3 stdlib og systemets `ping`.

## Installation

Kraever kun Python 3.9+ og systemets `ping`. Ingen pip-pakker.

```
git clone https://github.com/GlennLeving/netmon.git
cd netmon
python3 netmon.py --set-password     # saet adgangskode til indstillinger
./start.sh
```

Foerste start opretter `config.json` ud fra standardvaerdierne - se
`config.example.json`. Resten kan aendres i web-UI'et.

## Start

```
./start.sh                      # http://127.0.0.1:8420
./start.sh --host 0.0.0.0       # tilgaengelig fra andre maskiner paa nettet
./start.sh --port 9000
```

## Hvordan nedetid bestemmes

Hver testserver markeres som **LAN** (fx din router, `192.168.1.1`) eller **internet**
(fx `1.1.1.1`, `8.8.8.8`, `8.8.4.4`).

| Situation | Logges som |
|---|---|
| En enkelt server svarer ikke | `target`-nedetid for den server |
| Alle LAN-vaerter svarer ikke | `LAN` - lokalt netvaerk/kabel/router nede |
| LAN oppe, men alle internet-vaerter nede | `ISP` - din udbyder er nede |
| Ingen LAN-vaert defineret, alle internet-vaerter nede | `INTERNET` (aarsag ukendt) |

En server skal fejle `fail_threshold` gange i traek foer den regnes for nede - det
undgaar falske alarmer ved en enkelt tabt pakke.

## Adgangskode

Status-siden er aaben for alle der kan naa den, men **aendring af indstillinger kraever login**.
Beskyttede kald: gem indstillinger, ryd nedetids-log, skift adgangskode.

* Koden gemmes kun som PBKDF2-hash (240.000 runder, tilfaeldigt salt) i `auth.json` (chmod 600) -
  aldrig i klartekst, og aldrig i `config.json` som er offentligt laesbar.
* Login giver en tilfaeldig session-cookie (HttpOnly, SameSite=Strict) der holder 12 timer.
* 5 fejlede forsoeg fra samme IP spaerrer login i 5 minutter.

Skift kode i web-UI'et under Indstillinger, eller fra terminalen:

```
python3 netmon.py --set-password
```

Har du glemt koden: slet `auth.json` og koer `--set-password` igen.

> Bemaerk: trafikken er ualmindelig HTTP, saa koden sendes ukrypteret over netvaerket.
> Det er fint paa et hjemmenetvaerk, men brug ikke en kode du genbruger andre steder.

## Indstillinger (i web-UI eller `config.json`)

| Felt | Betydning |
|---|---|
| `interval_seconds` | Frekvens - sekunder mellem maalerunder |
| `timeout_seconds` | Wait-time - hvor laenge der ventes paa ping-svar |
| `ping_count` | Antal pings pr. maaling (bedste svartid bruges) |
| `fail_threshold` | Fejl i traek foer status = nede |
| `recover_threshold` | OK i traek foer status = oppe igen |
| `retention_days` | Hvor laenge raa maalinger gemmes |
| `targets[]` | `{name, host, kind: internet\|lan, enabled}` |

Aendringer via web-UI gemmes i `config.json` og traeder i kraft med det samme.

## Data

* `netmon.db` (sqlite) - `samples` (hver enkelt ping) og `outages` (nedetids-haendelser).
* Log kan hentes som CSV fra UI'et eller `GET /api/export.csv`.

Direkte forespoergsel:

```
sqlite3 netmon.db "SELECT scope, name, datetime(started,'unixepoch','localtime') AS start, ROUND(COALESCE(ended, strftime('%s','now')) - started) AS sekunder FROM outages ORDER BY started DESC LIMIT 20;"
```

## API

| Endpoint | Beskrivelse |
|---|---|
| `GET /api/status` | Live status for alle servere + LAN/ISP-vurdering |
| `GET /api/config` / `POST /api/config` | Laes/skriv indstillinger |
| `GET /api/outages?scope=all\|link\|target&limit=N` | Nedetids-log |
| `GET /api/history?host=1.1.1.1&hours=24` | Raa maalinger |
| `GET /api/export.csv` | Log som CSV |
| `POST /api/check-now` | Koer en maalerunde med det samme |
| `POST /api/clear-log` | Slet afsluttede haendelser (kraever login) |
| `POST /api/login` / `POST /api/logout` | `{"password": "..."}` - giver session-cookie |
| `GET /api/session` | `{authed, lockout}` |
| `POST /api/set-password` | `{"current": "...", "new": "..."}` (kraever login) |

`POST /api/config` kraever ogsaa login.

## Koer som baggrundstjeneste (systemd)

Servicen er allerede installeret som **bruger-service** i
`~/.config/systemd/user/netmon.service` og starter automatisk ved boot.

```
systemctl --user status netmon      # koerer den?
systemctl --user restart netmon     # genstart (fx efter kodeaendring)
systemctl --user stop netmon
systemctl --user disable --now netmon   # slaa auto-start fra
journalctl --user -u netmon -f      # foelg loggen live
```

Detaljer:

* `Restart=always` - starter igen 5 sekunder efter et crash.
* `loginctl enable-linger glenn` er slaaet til, saa den koerer selvom du ikke er logget ind.
* Loggen gaar til journalen (`journalctl --user -u netmon`), ikke til `netmon.log`.
* Servicen koerer med `ProtectSystem=strict` og maa kun skrive i `~/netmon`.

Skal port eller lytte-adresse aendres, ret `ExecStart` i unit-filen og koer:

```
systemctl --user daemon-reload && systemctl --user restart netmon
```

## Noter

* Selve status-visningen er aaben uden login - bind kun til `0.0.0.0` paa et netvaerk du stoler paa.
* Stop tjenesten med `pkill -f netmon.py` (husk at matche hele kommandolinjen hvis du bruger flag).
* Nogle vaerter (fx enkelte routere/firewalls) svarer ikke paa ICMP selv om de er oppe -
  test med `ping <ip>` i terminalen foer du tilfoejer dem.

## Licens

MIT - se [LICENSE](LICENSE). Author: Glenn Leving.

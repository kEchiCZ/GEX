# GEXLens â€” UĹľivatelskĂ˝ manuĂˇl

*Verze 1.3 Â· ÄŤervenec 2026 Â· pro aplikaci GEXLens v0.1*

GEXLens je aplikace pro intradennĂ­ tradery futures opcĂ­ (ES, NQ a dalĹˇĂ­ CME podklady). Vizualizuje **opÄŤnĂ­ positioning** â€” kde sedĂ­ koncentrace open interestu a volume, kde je zero-gamma flip, kde jsou call/put walls a Max Pain â€” a jak se to vĹˇechno vyvĂ­jĂ­ v ÄŤase. JedinĂ˝m zdrojem dat je tvĹŻj ĂşÄŤet u **Interactive Brokers** (TWS/IB Gateway API); ĹľĂˇdnĂˇ data neodchĂˇzejĂ­ mimo tvĹŻj poÄŤĂ­taÄŤ.

---

## Obsah

1. [Co aplikace umĂ­](#1-co-aplikace-umĂ­)
2. [Co potĹ™ebujeĹˇ pĹ™ed prvnĂ­m spuĹˇtÄ›nĂ­m](#2-co-potĹ™ebujeĹˇ-pĹ™ed-prvnĂ­m-spuĹˇtÄ›nĂ­m)
3. [SpuĹˇtÄ›nĂ­ a vypnutĂ­ aplikace](#3-spuĹˇtÄ›nĂ­-a-vypnutĂ­-aplikace)
4. [HlavnĂ­ obrazovka â€” Graf](#4-hlavnĂ­-obrazovka--graf)
5. [Heatmapa podrobnÄ›](#5-heatmapa-podrobnÄ›)
6. [CenovĂˇ vrstva â€” kĹ™ivka a svĂ­ÄŤky](#6-cenovĂˇ-vrstva--kĹ™ivka-a-svĂ­ÄŤky)
7. [Strike profil (pravĂ˝ panel)](#7-strike-profil-pravĂ˝-panel)
8. [SpodnĂ­ panely â€” Vol, Opt Vol, Cum Î”](#8-spodnĂ­-panely--vol-opt-vol-cum-Î´)
9. [Playback â€” pĹ™ehrĂˇvĂˇnĂ­ dne](#9-playback--pĹ™ehrĂˇvĂˇnĂ­-dne)
10. [Anotace â€” kreslenĂ­ do grafu](#10-anotace--kreslenĂ­-do-grafu)
11. [Dashboard](#11-dashboard)
12. [IBKR Console](#12-ibkr-console)
13. [Settings](#13-settings)
14. [Notifikace a alerty](#14-notifikace-a-alerty)
15. [StavovĂˇ liĹˇta â€” co znamenajĂ­ Ăşdaje](#15-stavovĂˇ-liĹˇta--co-znamenajĂ­-Ăşdaje)
16. [Deep-linky](#16-deep-linky)
17. [ĹeĹˇenĂ­ potĂ­ĹľĂ­](#17-Ĺ™eĹˇenĂ­-potĂ­ĹľĂ­)
18. [ObchodnĂ­ ÄŤtenĂ­ â€” reĹľimy trhu, Dyn GEX a flip](#18-obchodnĂ­-ÄŤtenĂ­--reĹľimy-trhu-dyn-gex-a-flip)
19. [SlovnĂ­ÄŤek pojmĹŻ](#19-slovnĂ­ÄŤek-pojmĹŻ)

---

## 1. Co aplikace umĂ­

- **Heatmapa ÄŤas Ă— strike** â€” barevnĂˇ mapa opÄŤnĂ­ho positioningu pĹ™es celĂ˝ obchodnĂ­ den. ZelenĂˇ (teal) = call strana, ÄŤervenĂˇ = put strana. Sedm pĹ™epĂ­natelnĂ˝ch metrik (**Mode**: OI, Vol OTM/ITM, Vol Â±, OI+OTM, OIâ’ITM, OIÂ±All) a ÄŤtyĹ™i ĹˇkĂˇly (**Scale**: Linear, âš, Log, Powâ…“).
- **GEX ĂşrovnÄ›** â€” automaticky poÄŤĂ­tanĂ˝ **flip** (zero-gamma), **call wall**, **put wall**, **centroid** a **Max Pain**, vykreslovanĂ© jako ÄŤasovĂ© linie i horizontĂˇlnĂ­ ĂşrovnÄ› s cenovkami, pĹ™epoÄŤĂ­tĂˇvanĂ© kaĹľdou minutu. VolitelnĂ© **Walls** mĂłdy (Peak/Center/Smooth/Flip/Ridge).
- **Multi-instrument** â€” watchlist v sidebaru: pĹ™idej ticker (ES, NQ, RTYâ€¦) a engine ho zaÄŤne sbĂ­rat; kliknutĂ­m pĹ™epĂ­nĂˇĹˇ celou aplikaci.
- **VĂ­ce expiracĂ­ najednou** â€” vedle aktivnĂ­ho Ĺ™etÄ›zu se sbĂ­rĂˇ i nĂˇsledujĂ­cĂ­ expirace (ÄŤtenĂ­ positioningu pĹ™Ă­ĹˇtĂ­ seance) a pravĂ˝ profil umĂ­ **ÎŁ souhrn pĹ™es expirace**.
- **Ĺ˝ivĂ˝ tok** â€” kumulativnĂ­ delta flow (Cum Î”) s klasifikacĂ­ agresora + **Î” Flow C/P** (tok zvlĂˇĹˇĹĄ za call/put stranu).
- **Î”OI vs. vÄŤera** â€” kde pĹ™es noc pĹ™ibyly/ubyly otevĹ™enĂ© pozice (tooltip profilu).
- **Replay** â€” skrytĂ˝ za tlaÄŤĂ­tkem âŹ® Replay; slider pĹ™ehraje vĂ˝voj dne rychlostĂ­ 1Ă—/5Ă—/20Ă—. Aplikace defaultnÄ› jede vĹľdy live.
- **Anotace** â€” Ĺˇipky, linie a kreslenĂ­ od ruky pĹ™Ă­mo do grafu; uloĹľenĂ© k instrumentu a dni, pĹ™eĹľijĂ­ restart.
- **Dashboard, IBKR Console, Settings** â€” provoznĂ­ obrazovky pro pĹ™ehled, diagnostiku a konfiguraci.

![HlavnĂ­ obrazovka](img/graf.png)

---

## 2. Co potĹ™ebujeĹˇ pĹ™ed prvnĂ­m spuĹˇtÄ›nĂ­m

KompletnĂ­ checklist je v GitHub issue [#1 â€” Setup uĹľivatelskĂ©ho prostĹ™edĂ­](https://github.com/kEchiCZ/GEX/issues/1). StruÄŤnÄ›:

1. **ĂšÄŤet u Interactive Brokers** s aktivnĂ­ market data subskripcĂ­ **CME Real-Time â€“ North America** (levnĂˇ L1 varianta za ~1.55 USD/mÄ›s. staÄŤĂ­ â€” ovÄ›Ĺ™eno).
2. **TWS nebo IB Gateway** nainstalovanĂ©, pĹ™ihlĂˇĹˇenĂ© a se zapnutĂ˝m API:
   - *Edit â†’ Global Configuration â†’ API â†’ Settings* â†’ âś… **Enable ActiveX and Socket Clients**
   - Socket port **7496** (live) / **7497** (paper)
   - Do *Trusted IPs* pĹ™idej `127.0.0.1`
   - *Read-Only API* nech zapnutĂ© â€” GEXLens nikdy neobchoduje, jen ÄŤte data
3. **Docker Desktop** (na Windows s WSL2 backendem).

> âš ď¸Ź **Jedno pĹ™ihlĂˇĹˇenĂ­ na username:** kdyĹľ se stejnĂ˝m IBKR loginem pĹ™ihlĂˇsĂ­Ĺˇ jinde (mobil, druhĂ© PC), TWS na tomto poÄŤĂ­taÄŤi spadne a aplikace ztratĂ­ data. Po nĂˇvratu se staÄŤĂ­ v TWS znovu pĹ™ihlĂˇsit â€” aplikace se pĹ™ipojĂ­ sama.

---

## 3. SpuĹˇtÄ›nĂ­ a vypnutĂ­ aplikace

### Ikonou na ploĹˇe (doporuÄŤeno)

Poklepej na ikonu **GEXLens** na ploĹˇe. Skript spustĂ­ vĹˇechny sluĹľby na pozadĂ­ a otevĹ™e prohlĂ­ĹľeÄŤ na adrese aplikace. PrvnĂ­ start po vypnutĂ­ poÄŤĂ­taÄŤe trvĂˇ ~30â€“60 s.

### RuÄŤnÄ› (PowerShell)

```powershell
cd "D:\Documents\Visual Studio Code\GEX"
docker compose up -d        # start na pozadĂ­
# prohlĂ­ĹľeÄŤ: http://127.0.0.1:8080
```

### VypnutĂ­

```powershell
docker compose stop         # zastavĂ­ sluĹľby (data zĹŻstĂˇvajĂ­)
```

Aplikaci mĹŻĹľeĹˇ nechat bÄ›Ĺľet trvale â€” engine sbĂ­rĂˇ data, jen kdyĹľ bÄ›ĹľĂ­ a je pĹ™ihlĂˇĹˇenĂ© TWS; mimo seance prostÄ› ÄŤekĂˇ.

### PoĹ™adĂ­ pĹ™i startu dne

1. Zapni/pĹ™ihlas **TWS** (nebo IB Gateway)
2. SpusĹĄ **GEXLens** (pokud nebÄ›ĹľĂ­)
3. Do minuty se ve stavovĂ© liĹˇtÄ› objevĂ­ `IBKR: connected` a zaÄŤnou pĹ™ibĂ˝vat data

---

## 4. HlavnĂ­ obrazovka â€” Graf

Obrazovka se sklĂˇdĂˇ z (shora dolĹŻ, zleva doprava):

| Prvek | Popis |
|---|---|
| **Sidebar (vlevo)** | PĹ™epĂ­nĂˇnĂ­ obrazovek (Graf / Dashboard / IBKR Console / Settings), pĹ™epĂ­naÄŤ tĂ©matu Dark/Light, **editovatelnĂ˝ watchlist** (kliknutĂ­ na ticker pĹ™epne instrument, Ă— odebere, polĂ­ÄŤko dole pĹ™idĂˇ novĂ˝), verze. TlaÄŤĂ­tkem Â« se sbalĂ­. |
| **HlaviÄŤka** | Ticker a nĂˇzev instrumentu, **poslednĂ­ cena + dennĂ­ zmÄ›na v %**, **selektor expirace** s typem (dennĂ­/tĂ˝dennĂ­/mÄ›sĂ­ÄŤnĂ­/kvartĂˇlnĂ­/EOM) a odpoÄŤtem â€žexpiruje â‰ za X h" â€” velkĂ© expirace nesou velkĂ© OI. V selektoru najdeĹˇ i **nĂˇsledujĂ­cĂ­ expiraci** (sbĂ­rĂˇ se soubÄ›ĹľnÄ› â€” ÄŤtenĂ­ positioningu pĹ™Ă­ĹˇtĂ­ seance). IndikĂˇtor â—Ź Live / â—‹ Offline, zvonek notifikacĂ­. |
| **ĹĂˇdek timeframe** | **Intraday/Daily** a rozliĹˇenĂ­ **1m, 2m, 3m, 5m, 10m, 15m, 30m, 45m, 1h, 2h, 3h, 4h, 1d**. Intraday agreguje minutovĂˇ data do zvolenĂ˝ch koĹˇĹŻ (svĂ­ÄŤky OHLC, objemy se sÄŤĂ­tajĂ­); Daily zobrazĂ­ sloupec za kaĹľdĂ˝ uloĹľenĂ˝ den (roste s historiĂ­, max 14 dnĂ­). |
| **ĹĂˇdek pĹ™epĂ­naÄŤĹŻ** | Checkboxy vrstev: **Zdi** (call/put wall linie; dĹ™Ă­v se jmenoval â€žDyn GEX" â€” nĂˇzev teÄŹ patĹ™Ă­ heatmap mĂłdu), **GEX Levels** (flip/centroid/Max Pain), **Sessions** (automatickĂ© markery svÄ›tovĂ˝ch seancĂ­), **Vol / Opt Vol / Delta / Î” Flow C/P** (spodnĂ­ panely), **Vol + OI Î”**, **Projekce**, **News**. Co odĹˇkrtneĹˇ, zmizĂ­ â€” layout se pĹ™esklĂˇdĂˇ. |
| **LiĹˇta grafu** | **Mode** (7 metrik heatmapy), **Scale** (Linear/âš/Log/Powâ…“), **Walls** (Off/Peak/Center/Smooth/Flip/Ridge), Styl (Gradient/Blobs), Contours (Off/Major/All), **Cena** (SvĂ­ÄŤky/KĹ™ivka) + **Viditelnost**, nĂˇstroje anotacĂ­ + barva, indikĂˇtor zdroje dat, tlaÄŤĂ­tko **âŹ® Replay**. |
| **Heatmapa** | HlavnĂ­ plocha â€” viz kapitola 5. |
| **Strike profil** | PravĂ˝ panel; **pĹ™edÄ›l mezi grafem a panelem jde tĂˇhnout** (kurzor â†”) â€” viz kapitola 7. |
| **SpodnĂ­ panely** | Vol / Opt Vol / Î” Flow / Cum Î” â€” viz kapitola 8. |
| **Playback liĹˇta** | DefaultnÄ› skrytĂˇ (aplikace jede vĹľdy live) â€” zobrazĂ­ ji tlaÄŤĂ­tko **âŹ® Replay**; viz kapitola 9. |
| **StavovĂˇ liĹˇta** | ZdravĂ­ datovĂ© pipeline â€” viz kapitola 15. |

---

## 5. Heatmapa podrobnÄ›

Heatmapa zobrazuje matici **ÄŤas (osa X) Ă— strike (osa Y)**. KaĹľdĂˇ buĹka je jedna minuta jednoho striku; intenzita barvy odpovĂ­dĂˇ hodnotÄ› zvolenĂ© metriky. **Teal/zelenĂˇ = call strana, ÄŤervenĂˇ = put strana.**

### OvlĂˇdĂˇnĂ­ myĹˇĂ­ (styl TradingView)

| Akce | Efekt |
|---|---|
| KoleÄŤko nad plochou | Zoom obou os **ukotvenĂ˝ ke kurzoru** (bod pod myĹˇĂ­ zĹŻstĂˇvĂˇ na mĂ­stÄ›) |
| KoleÄŤko nad pruhem osy | Zoom **jen danĂ© osy** (levĂ˝ okraj = osa strikes, spodnĂ­ okraj = osa ÄŤasu) |
| TaĹľenĂ­ za pruh osy Y (levĂ˝ okraj) | RoztahovĂˇnĂ­/stahovĂˇnĂ­ cenovĂ© osy â€” zvÄ›tĹˇĂ­/zmenĹˇĂ­ svĂ­ÄŤky svisle |
| TaĹľenĂ­ / koleÄŤko na **pravĂ©m panelu** (profil) | **OvlĂˇdĂˇ stejnou cenovou osu Y** jako levĂ˝ okraj â€” tĂˇhni svisle nebo koleÄŤkuj pĹ™Ă­mo nad profilem (kurzor â†•) |
| TaĹľenĂ­ za pruh osy X (spodnĂ­ okraj) | RoztahovĂˇnĂ­/stahovĂˇnĂ­ ÄŤasovĂ© osy â€” zvÄ›tĹˇĂ­/zmenĹˇĂ­ svĂ­ÄŤky vodorovnÄ› (kotva u pravĂ©ho okraje: poslednĂ­ svĂ­ÄŤka drĹľĂ­ pozici) |
| TaĹľenĂ­ v ploĹˇe | Posun (pan) |
| **Dvojklik** nebo tlaÄŤĂ­tko **âź˛** (pravĂ˝ hornĂ­ roh) | **Reset zobrazenĂ­** na vĂ˝chozĂ­ pohled |
| Pohyb myĹˇĂ­ | Crosshair â€” svislĂˇ linka snapnutĂˇ na svĂ­ÄŤku, vodorovnĂˇ sleduje kurzor; synchronizovanĂ˝ se strike profilem i spodnĂ­mi panely + tooltip buĹky (minuta, strike, hodnoty call/put) |

Crosshair navĂ­c ukazuje **osovĂ© ĹˇtĂ­tky jako TradingView**: dole na ose X **datum + ÄŤas** pod svislou linkou, vpravo na ose Y **cenu** na Ăşrovni kurzoru. Cena je zaokrouhlenĂˇ na **minimĂˇlnĂ­ tick instrumentu** (ES/NQ = 0,25) â€” mezi 7530,00 a 7530,25 tedy nic nenĂ­. Crosshair **zĹŻstĂˇvĂˇ viditelnĂ˝ i mimo svĂ­ce** â€” kdyĹľ posuneĹˇ graf a jedeĹˇ myĹˇĂ­ pĹ™es prĂˇzdnou/budoucĂ­ plochu, nezmizĂ­.

**Pohled se nesmĂ˝kĂˇ sĂˇm.** Graf se automaticky napasuje (fit na cenovĂ© pĂˇsmo + ukotvenĂ­ historie) **jen pĹ™i zmÄ›nÄ› datasetu** â€” jinĂ˝ symbol, expirace, timeframe nebo den. Resize pravĂ©ho panelu, ĹľivĂ˝ pĹ™Ă­rĹŻstek minut ani Ăşprava os **tvĹŻj pan/zoom nepĹ™epĂ­Ĺˇou**. Kdykoli se vrĂˇtĂ­Ĺˇ na napasovanĂ˝ pohled dvojklikem nebo tlaÄŤĂ­tkem âź˛.

SpodnĂ­ panely (Vol / Opt Vol / Cum Î”) **sledujĂ­ ÄŤasovou osu heatmapy** â€” pĹ™i posunu ÄŤi zoomu osy X se roztahujĂ­ synchronnÄ›.

### Mode â€” osm metrik heatmapy

Select **Mode** pĹ™epĂ­nĂˇ, co buĹky zobrazujĂ­ (pĹ™epoÄŤet je okamĹľitĂ˝, bez ÄŤekĂˇnĂ­ na server; dostupnĂ© nad ĹľivĂ˝mi/replay daty):

| Mode | Co ukazuje |
|---|---|
| **OI** | Open interest per strike (vĂ˝chozĂ­). Dokud rannĂ­ OI nedorazĂ­, automaticky se pouĹľije volume. |
| **Vol OTM** | Volume jen OTM opcĂ­ (call nad spotem, put pod spotem) â€” ÄŤerstvĂˇ spekulace/zajiĹˇtÄ›nĂ­ |
| **Vol ITM** | Volume ITM opcĂ­ |
| **Vol Â±** | RozdĂ­l call â’ put volume (zeleno-ÄŤervenĂˇ divergenÄŤnĂ­ mapa) |
| **OI+OTM** | VĂˇĹľenĂˇ kombinace OI (60 %) a OTM volume (40 %) â€” â€žkde sedĂ­ i kde se dnes hraje" |
| **OIâ’ITM** | OI oÄŤiĹˇtÄ›nĂ© o ITM volume |
| **OIÂ±All** | RozdĂ­l call â’ put OI (divergenÄŤnĂ­) |
| **Dyn GEX** | ModelovanĂ© pole NetGEX (zelenĂˇ = dealeĹ™i tlumĂ­, ÄŤervenĂˇ = zesilujĂ­) pĹ™es celĂ© pĂˇsmo a celĂ˝ den â€” vlevo od pĹ™edÄ›lu namÄ›Ĺ™enĂˇ historie, vpravo modelovanĂˇ budoucnost do settle. Jak ho ÄŤĂ­st a obchodovat: **kapitola 18**. |

Select **Scale** mÄ›nĂ­ ĹˇkĂˇlu hodnot: **Linear**, **âš** (zvĂ˝raznĂ­ slabĹˇĂ­), **Log**, **Powâ…“**. ZnamĂ©nko se zachovĂˇvĂˇ.

### Walls â€” detekce zdĂ­

Select **Walls** kreslĂ­ bĂ­lĂ© ÄŤĂˇrkovanĂ© linie poÄŤĂ­tanĂ© z prĂˇvÄ› zobrazenĂ© vrstvy:

- **Peak** â€” strike s maximem metriky per minuta (call i put strana)
- **Center** â€” vĂˇĹľenĂ© tÄ›ĹľiĹˇtÄ› per minuta
- **Smooth** â€” vyhlazenĂ˝ Peak (EMA 15 minut)
- **Flip** â€” kopie zero-gamma Ĺ™ady
- **Ridge** â€” soubÄ›ĹľnĂ© hĹ™ebeny koncentracĂ­ (vĂ­c zdĂ­ najednou, s filtrem Ĺˇumu)

### Styl vykreslenĂ­

- **Gradient** â€” hladkĂ© bilineĂˇrnĂ­ pĹ™echody (vĂ˝chozĂ­)
- **Blobs** â€” gaussovskĂ© â€žbublinyâ€ś kolem koncentracĂ­; zvĂ˝raznĂ­ ohniska positioningu

### Contours (izolinie)

BĂ­lĂ© pĹ™eruĹˇovanĂ© vrstevnice nad vyhlazenĂ˝m polem:
- **Off** â€” vypnuto
- **Major** â€” dvÄ› ĂşrovnÄ› (p75 a p90) â€” jen hlavnĂ­ koncentrace
- **All** â€” pÄ›t ĂşrovnĂ­ â€” detailnĂ­ tvar

### Linie v heatmapÄ› (overlaye)

| Linie | Barva | ZapĂ­nĂˇ |
|---|---|---|
| **Flip (zero-gamma)** | ĹľlutĂˇ | GEX Levels |
| **Centroid (HVL)** | fialovĂˇ | GEX Levels |
| **Max Pain** | magenta | GEX Levels (poÄŤĂ­tĂˇ se z OI) |
| **Call wall** | zelenĂˇ | Zdi |
| **Put wall** | ÄŤervenĂˇ | Zdi |
| **Walls mĂłdy** | bĂ­lĂ© ÄŤĂˇrkovanĂ© | select Walls |
| **Sessions markery** | ĹˇedĂ© svislĂ© (Tokio, LondĂ˝n, US Openâ€¦) | Sessions |
| **CenovĂˇ vrstva** | zelenĂˇ/ÄŤervenĂˇ | vĹľdy (viz kap. 6) |

KaĹľdĂˇ ĂşroveĹ se navĂ­c promĂ­tĂˇ jako **horizontĂˇlnĂ­ ÄŤĂˇrkovanĂˇ linka pĹ™es celou ĹˇĂ­Ĺ™ku s barevnou cenovkou** u levĂ©ho okraje (poslednĂ­ znĂˇmĂˇ hodnota) â€” na prvnĂ­ pohled vidĂ­Ĺˇ, kde ĂşrovnÄ› prĂˇvÄ› leĹľĂ­. Vpravo na ose je **ĹˇtĂ­tek aktuĂˇlnĂ­ ceny**; v pravĂ©m dolnĂ­m rohu **timestamp** poslednĂ­ch dat.

### Stale buĹky

Pokud se nÄ›kterĂ˝ strike nepodaĹ™ilo obnovit (vĂ˝padek dat), jeho buĹky jsou **vyĹˇedlĂ© s niĹľĹˇĂ­ sytostĂ­** â€” poznĂˇĹˇ tak starĂˇ data od ĹľivĂ˝ch. Souhrn bÄ›ĹľĂ­ ve stavovĂ© liĹˇtÄ› (`Repair: retrying Nâ€¦`).

---

## 6. CenovĂˇ vrstva â€” kĹ™ivka a svĂ­ÄŤky

V liĹˇtÄ› grafu volbou **Cena**:

- **SvĂ­ÄŤky** (vĂ˝chozĂ­) â€” plnohodnotnĂ© OHLC svĂ­ÄŤky (knot highâ€“low, tÄ›lo openâ€“close) v rozliĹˇenĂ­ zvolenĂ©ho timeframe.
- **KĹ™ivka** â€” spojitĂˇ linie zbarvenĂˇ podle smÄ›ru ticku (zelenĂˇ nahoru, ÄŤervenĂˇ dolĹŻ).

Graf se pĹ™i naÄŤtenĂ­ **automaticky napasuje na cenovĂ© pĂˇsmo dne** (svĂ­ÄŤky vyplnĂ­ vĂ˝Ĺˇku); okolnĂ­ zĂłny heatmapy dosĂˇhneĹˇ taĹľenĂ­m/koleÄŤkem za osu Y, reset vracĂ­ napasovanĂ˝ pohled. Po startu s **mĂˇlem dat** (rĂˇno) majĂ­ svĂ­ÄŤky **fixnĂ­ ĹˇĂ­Ĺ™ku** ukotvenou k pravĂ©mu okraji â€” neroztahujĂ­ se pĹ™es celĂ˝ graf jako dĹ™Ă­v.

PosuvnĂ­kem **Viditelnost** (10â€“100 %) cenovou vrstvu zeslabĂ­Ĺˇ, aby nepĹ™ebĂ­jela heatmapu pod nĂ­ â€” uĹľiteÄŤnĂ© hlavnÄ› u svĂ­ÄŤek. **Ĺ tĂ­tek aktuĂˇlnĂ­ ceny zĹŻstĂˇvĂˇ vĹľdy plnÄ› viditelnĂ˝.**

![SvĂ­ÄŤkovĂ˝ reĹľim](img/svicky.png)

---

## 7. Strike profil (pravĂ˝ panel)

HorizontĂˇlnĂ­ sklĂˇdanĂ© pruhy pro kaĹľdĂ˝ strike, **na stejnĂ© vĂ˝ĹˇkovĂ© ose jako heatmapa** â€” strike v profilu je vĹľdy na stejnĂ© Ăşrovni obrazovky jako v grafu a pĹ™i zoomu/posunu osy Y se hĂ˝bou synchronnÄ›:

- **Call doprava (teal), put doleva (ÄŤervenĂˇ)** od symetrickĂ© osy; popisky **Put/Call** nahoĹ™e
- KaĹľdĂ˝ pruh mĂˇ dvÄ› sloĹľky odliĹˇenĂ© sytostĂ­: **Vol** (sytĂˇ) a **OI Î”** (svÄ›tlejĹˇĂ­)
- U konce pruhĹŻ jsou **ÄŤĂ­selnĂ© hodnoty** (Î”-vĂˇĹľenĂ© kontrakty) â€” na kaĹľdĂ©m k-tĂ©m Ĺ™Ăˇdku, aĹĄ se nepĹ™ekrĂ˝vajĂ­. Pruhy konÄŤĂ­ kousek pĹ™ed okrajem, takĹľe nepĹ™etĂ©kajĂ­ mimo panel.
- Dole **osa mnoĹľstvĂ­** (Î”-vĂˇĹľenĂ© kontrakty, formĂˇt â€ž5k") â€” vidĂ­Ĺˇ, jak velkĂ© zdi reĂˇlnÄ› jsou; mÄ›Ĺ™Ă­tko reaguje na zoom
- **Ĺ edĂˇ pĹ™eruĹˇovanĂˇ linka** = aktuĂˇlnĂ­ cena (spot)
- TlaÄŤĂ­tko **GEX** = kĹ™ivka modelovanĂ©ho **Dyn GEX profilu** (zelenĂˇ doprava = dealeĹ™i tlumĂ­, ÄŤervenĂˇ doleva = zesilujĂ­); **ĹľlutĂˇ pĹ™eruĹˇovanĂˇ linka** = **dynamickĂ˝ flip** (prĹŻchod kĹ™ivky nulou). Detaily ÄŤtenĂ­: kapitola 18.
- TlaÄŤĂ­tko **Rel / Abs** pĹ™epĂ­nĂˇ mÄ›Ĺ™Ă­tko: **Rel** = normalizace na nejvÄ›tĹˇĂ­ pruh ve vĂ˝Ĺ™ezu (vĂ˝chozĂ­), **Abs** = zaokrouhlenĂ˝ â€žhezkĂ˝" strop (kulatĂ© hodnoty na ose, stabilnÄ›jĹˇĂ­ dĂ©lky pruhĹŻ mezi snĂ­mky)
- TlaÄŤĂ­tka **1Ă— / 2Ă— / 4Ă—** zvÄ›tĹˇujĂ­ mÄ›Ĺ™Ă­tko pruhĹŻ
- TlaÄŤĂ­tko **ÎŁ** = souhrn pĹ™es vĹˇechny sbĂ­ranĂ© expirace tohoto instrumentu (pondÄ›lnĂ­ + ĂşternĂ­ Ĺ™etÄ›zâ€¦). HlaviÄŤka se zmÄ›nĂ­ na â€žÎŁ expiracĂ­"; heatmapa zĹŻstĂˇvĂˇ u zvolenĂ© expirace. CelkovĂ˝ positioning bez pĹ™epĂ­nĂˇnĂ­.
- NajetĂ­ myĹˇĂ­ na Ĺ™Ăˇdek zvĂ˝raznĂ­ strike v celĂ© aplikaci (crosshair) a dole zobrazĂ­ **tooltip**: OI call/put, Vol call/put, **Î”OI vs. vÄŤera C/P** (kde pĹ™es noc pĹ™ibyly/ubyly pozice; jen per expirace, v ÎŁ reĹľimu se neukazuje), vzdĂˇlenost od spotu
- **Ĺ Ă­Ĺ™ku panelu zmÄ›nĂ­Ĺˇ taĹľenĂ­m pĹ™edÄ›lu** mezi grafem a panelem (kurzor â†”). Panel jde roztĂˇhnout hodnÄ› doleva (aĹľ ~360 px zbyde na graf), aby byla vidÄ›t celĂˇ dĂ©lka pruhu i s ÄŤĂ­slem.
- **Profilem ovlĂˇdĂˇĹˇ i cenovou osu Y grafu** â€” taĹľenĂ­ svisle nebo koleÄŤko nad profilem stlaÄŤuje/roztahuje ceny stejnÄ› jako levĂ˝ okraj heatmapy (kurzor â†•).

ÄŚteĹˇ z nÄ›j na prvnĂ­ pohled, **kde sedĂ­ dominantnĂ­ call a put koncentrace** â€” typicky walls.

> **ProÄŤ veÄŤer â€žzmizĂ­" jedna strana pruhĹŻ?** Pruhy jsou **Î”-vĂˇĹľenĂ©** â€” nĂˇsobĂ­ se deltou opce (kolik futures dealer na kontrakt reĂˇlnÄ› drĹľĂ­). Ke konci seance se delta polarizuje (viz gamma crunch, kap. 18): OTM opce majĂ­ deltu skoro 0, ITM skoro 1. Nad spotem proto zbĂ˝vajĂ­ hlavnÄ› ÄŤervenĂ© (ITM puty) a pod spotem zelenĂ© (ITM cally). NenĂ­ to chyba â€” surovĂ© OI/Vol obou stran poĹ™Ăˇd vidĂ­Ĺˇ v tooltipu Ĺ™Ăˇdku; zapnutĂ­m **ÎŁ** se pĹ™imĂ­chĂˇ zĂ­tĹ™ejĹˇĂ­ expirace s mÄ›kÄŤĂ­mi deltami a obÄ› strany se zase objevĂ­.

---

## 8. SpodnĂ­ panely â€” Vol, Opt Vol, Cum Î”

TĹ™i panely se **sdĂ­lenou ÄŤasovou osou** s heatmapou. KaĹľdĂ˝ zvlĂˇĹˇĹĄ vypneĹˇ checkboxem v hornĂ­ liĹˇtÄ› (Vol / Opt Vol / Delta).

| Panel | Co ukazuje |
|---|---|
| **Vol** | MinutovĂ˝ objem podkladu (futures) â€” ĹˇedĂ© sloupce |
| **Opt Vol** | MinutovĂ˝ objem opcĂ­, **barevnÄ› call (teal) / put (ÄŤervenĂˇ)** vedle sebe |
| **Î” Flow C/P** | Delta-vĂˇĹľenĂ˝ opÄŤnĂ­ tok zvlĂˇĹˇĹĄ za call a put stranu (\|Î”\| Ă— pĹ™Ă­rĹŻstek volume). Z nÄ›j ÄŤteĹˇ, **na kterĂ© stranÄ› se prĂˇvÄ› obchoduje** â€” napĹ™. â€žuzavĂ­rĂˇnĂ­ callĹŻ" = call sloupce slĂˇbnou. Default vypnutĂ˝ (checkbox Î” Flow C/P). |
| **Cum Î”** | KumulativnĂ­ delta flow jako plocha **nad nulou (zelenĂˇ) / pod nulou (ÄŤervenĂˇ)**. Roste = agresivnĂ­ kupci call delty / prodejci put delty; klesĂˇ = opaÄŤnÄ›. PoÄŤĂ­tĂˇ se s plnou klasifikacĂ­ agresora (tick-by-tick v hot zĂłnÄ›, midpoint test jinde) a resetuje se na zaÄŤĂˇtku dne. |

Pohyb myĹˇĂ­ v kterĂ©mkoli panelu hĂ˝be crosshairem ve vĹˇech panelech i heatmapÄ›. PĹ™i najetĂ­ navĂ­c uvidĂ­Ĺˇ **hodnoty ukazatele**:

- **vpravo nahoĹ™e** hodnotu pro **minutu pod crosshairem** (Opt Vol a Î” Flow zvlĂˇĹˇĹĄ C/P);
- **vpravo na ose Y** hodnotu podle **vĂ˝ĹˇkovĂ© ĂşrovnÄ› kurzoru** (ne max danĂ©ho ÄŤasu) + **vodorovnou crosshair linku** na tĂ© Ăşrovni. U Cum Î” je ĹˇkĂˇla znamĂ©nkovĂˇ kolem nuly.

---

## 9. Playback â€” pĹ™ehrĂˇvĂˇnĂ­ dne

**Aplikace defaultnÄ› jede vĹľdy live** â€” replay liĹˇta je skrytĂˇ, aby neruĹˇila. ZobrazĂ­Ĺˇ ji tlaÄŤĂ­tkem **âŹ® Replay** v liĹˇtÄ› grafu:

- **Slider** â€” tĂˇhni kamkoli v dni; heatmapa, strike profil i spodnĂ­ panely se **synchronnÄ› pĹ™etoÄŤĂ­** k danĂ©mu okamĹľiku
- **â–¶ / âŹ¸** â€” automatickĂ© pĹ™ehrĂˇvĂˇnĂ­; rychlosti **1Ă— / 5Ă— / 20Ă—** (1Ă— = 2 minuty dne za sekundu)
- **â—Ź Live** â€” skok zpÄ›t na aktuĂˇlnĂ­ okamĹľik; pĹ™ehrĂˇvĂˇnĂ­ na konci dne se zastavĂ­ samo
- **ZavĹ™enĂ­ liĹˇty** (druhĂ˝ klik na âŹ® Replay) graf automaticky vrĂˇtĂ­ na live

CelĂ˝ den je po naÄŤtenĂ­ v pamÄ›ti â€” pĹ™etĂˇÄŤenĂ­ je okamĹľitĂ©, bez ÄŤekĂˇnĂ­ na server. PĹ™i pĹ™epnutĂ­ timeframe zĹŻstĂˇvĂˇ live pozice live a rozehranĂ˝ replay se pĹ™emapuje proporcionĂˇlnÄ›.

---

## 10. Anotace â€” kreslenĂ­ do grafu

V liĹˇtÄ› grafu vyber nĂˇstroj:

| NĂˇstroj | PouĹľitĂ­ |
|---|---|
| **Kurzor** | BÄ›ĹľnĂ˝ reĹľim (pan/zoom/crosshair) |
| **Ĺ ipka** | TĂˇhni odâ€“do; Ĺˇipka s hlaviÄŤkou |
| **Linie** | TĂˇhni odâ€“do; rovnĂˇ ÄŤĂˇra |
| **Freehand** | Kresli od ruky |
| **Guma** | Klikni poblĂ­Ĺľ anotace â€” smaĹľe ji |

Vedle nĂˇstrojĹŻ je **vĂ˝bÄ›r barvy**. Anotace jsou ukotvenĂ© k **ÄŤasu a striku** (ne k pixelĹŻm) â€” drĹľĂ­ na svĂ©m mĂ­stÄ› pĹ™i zoomu, panu i pĹ™etĂˇÄŤenĂ­, **pĹ™eĹľijĂ­ restart aplikace** a jsou uloĹľenĂ© zvlĂˇĹˇĹĄ pro kaĹľdĂ˝ instrument a den.

---

## 11. Dashboard

Karty instrumentĹŻ z watchlistu: aktuĂˇlnĂ­ cena, stav dat (â—Ź live / offline), **mini NetGEX profil** (zelenĂ©/ÄŤervenĂ© sloupeÄŤky = ÄŤistĂ˝ positioning po stricĂ­ch) a vzdĂˇlenosti k call/put wall. SlouĹľĂ­ jako rychlĂ˝ pĹ™ehled, kdyĹľ sledujeĹˇ vĂ­c instrumentĹŻ.

![Dashboard](img/dashboard.png)

---

## 12. IBKR Console

DiagnostickĂˇ obrazovka:

- **PĹ™ipojenĂ­** â€” host/port/client ID, tlaÄŤĂ­tko **Reconnect** (vyĹľĂˇdĂˇ znovupĹ™ipojenĂ­ enginu), aktuĂˇlnĂ­ stav spojenĂ­
- **Subskripce** â€” prĹŻbÄ›h Greeks (X/Y), velikost repair fronty, vytĂ­ĹľenĂ­ market data lines
- **Log** â€” chronologickĂ˝ zĂˇznam udĂˇlostĂ­ (zmÄ›ny stavu spojenĂ­, alerty)

Sem se podĂ­vej jako prvnĂ­, kdyĹľ nÄ›co nevypadĂˇ dobĹ™e.

![IBKR Console](img/console.png)

---

## 13. Settings

ZmÄ›ny se **uklĂˇdajĂ­ okamĹľitÄ›** (bez tlaÄŤĂ­tka UloĹľit) a engine si je prĹŻbÄ›ĹľnÄ› pĹ™ebĂ­rĂˇ:

| Sekce | PoloĹľky |
|---|---|
| **IBKR** | Host, port (7496 live / 7497 paper), client ID |
| **Engine** | **Rozsah strikes (Â± body od spotu)** â€” engine si zmÄ›nu pĹ™ebere do 5 minut za bÄ›hu a rozĹˇĂ­Ĺ™Ă­ sbĂ­ranĂ© pĂˇsmo (max 400; vidÄ›t vzdĂˇlenĂˇ kĹ™Ă­dla Ă  la pojistky hluboko OTM), velikost dĂˇvky subskripcĂ­, ĹˇĂ­Ĺ™ka hot zĂłny, retence dat (dny), disk limit (GB) |
| **Vzhled** | TĂ©ma **Dark/Light** (pĹ™epne se ihned), jazyk |
| **Seance** | HistorickĂ© pole (JSON) â€” markery seancĂ­ se novÄ› generujĂ­ **automaticky** z ÄŤasĹŻ svÄ›tovĂ˝ch burz; checkbox Sessions v grafu |

![Settings](img/settings.png)

Light tĂ©ma:

![Light tĂ©ma](img/light.png)

---

## 14. Notifikace a alerty

**Zvonek** v hlaviÄŤce ukazuje badge s poÄŤtem nepĹ™eÄŤtenĂ˝ch alertĹŻ; kliknutĂ­m otevĹ™eĹˇ historii (otevĹ™enĂ­ badge vynuluje). Alerty chodĂ­ i za bÄ›hu do IBKR Console logu.

Zvonek je **globĂˇlnĂ­ â€” sbĂ­rĂˇ alerty napĹ™Ă­ÄŤ vĹˇemi instrumenty** ve watchlistu, ne jen z toho na grafu. Proto je u kaĹľdĂ©ho alertu **datum + ÄŤas** notifikace a **symbol instrumentu** (napĹ™. `[NQ Â· setup]`). Naproti tomu **karty a linie setupĹŻ pĹ™Ă­mo v grafu jsou jen pro instrument, kterĂ˝ mĂˇĹˇ zobrazenĂ˝.**

Druhy alertĹŻ:

| Alert | Kdy |
|---|---|
| Cena Ă— ĂşroveĹ | Cena protne flip nebo wall |
| Cum Î” skok | Skok kumulativnĂ­ delty o nastavenĂ˝ prĂˇh |
| DominantnĂ­ strike | ZmÄ›na striku s nejvÄ›tĹˇĂ­ koncentracĂ­ |
| VĂ˝padek spojenĂ­ | TWS/Gateway nedostupnĂ© |
| Disk limit | PĹ™ekroÄŤen limit mĂ­sta na disku |
| **OI nedorazilo** | IBKR nedodalo Open Interest â€” GEX vrstvy jedou doÄŤasnÄ› z volume (viz ĹeĹˇenĂ­ potĂ­ĹľĂ­) |
| **Instrument nejde spustit** | Ticker z watchlistu nenĂ­ futures s opÄŤnĂ­m Ĺ™etÄ›zem (napĹ™. akcie) â€” engine to zkusĂ­ znovu za 30 minut |
| ObĂˇlka na stropu | PĂˇsmo strikes dosĂˇhlo maxima ĹˇĂ­Ĺ™ky â€” vzdĂˇlenĂ˝ okraj se posouvĂˇ za cenou |
| **SvĂ­ÄŤky se pĹ™estaly kreslit** | Real-time bary z TWS nechodĂ­, ale cena Ĺľije (mrtvĂ© TWS farmy po noÄŤnĂ­ pĹ™estĂˇvce) â€” pomĂˇhĂˇ restart TWS; dĂ­ra se po nĂˇvratu doplnĂ­ sama |
| SvĂ­ÄŤky zase jedou | Bary se vrĂˇtily â€” dĂ­ra ve svĂ­ÄŤkĂˇch se doplnĂ­ backfillem |
| **NovĂ˝ setup** | Detektor naĹˇel obchodnĂ­ setup (odraz od zdi / neĂşspÄ›ĹˇnĂ˝ prĹŻraz / Max Pain pin / gamma momentum) |

### Setupy

KdyĹľ detektor najde setup, pĹ™ijde alert **NovĂ˝ setup** a nad grafem se ukĂˇĹľe **karta setupu** pro danĂ˝ instrument: smÄ›r (LONG/SHORT), Ĺˇablona, **datum a ÄŤas vzniku** (kdy se splnily podmĂ­nky), ĂşrovnÄ› **Entry / CĂ­l / Stop**, RRR a dĹŻvÄ›ra, plus krĂˇtkĂ© zdĹŻvodnÄ›nĂ­. StejnĂ© ĂşrovnÄ› se kreslĂ­ jako linie pĹ™Ă­mo v heatmapÄ›. Kartu skryjeĹˇ kĹ™Ă­Ĺľkem (setup dĂˇl bÄ›ĹľĂ­). Historii, ĂşspÄ›Ĺˇnost a hodnocenĂ­ đź‘Ť/đź‘Ž najdeĹˇ na obrazovce **Setupy** v sidebaru.

---

## 15. StavovĂˇ liĹˇta â€” co znamenajĂ­ Ăşdaje

| Ăšdaj | VĂ˝znam | V poĹ™Ăˇdku |
|---|---|---|
| `Greeks X/Y` | Kolik kontraktĹŻ Ĺ™etÄ›zce mĂˇ kompletnĂ­ data (bid/ask/volume/Greeks) | X = Y |
| `Repair: retrying Nâ€¦` | Kontrakty ÄŤekajĂ­cĂ­ na opakovanĂ© naÄŤtenĂ­ | Nezobrazuje se, nebo malĂ© N |
| `Lines NN %` | VytĂ­ĹľenĂ­ market data lines ĂşÄŤtu | < 100 % |
| `Disk X / Y` | ObsazenĂ­ disku daty / limit | X < Y |
| `IBKR: connected :7496` | Stav spojenĂ­ + port | connected |
| `â—Ź Live HH:MM` / `Stale` | ÄŚas poslednĂ­ch dat | â—Ź Live, ÄŤas se hĂ˝be |

---

## 16. Deep-linky

Aplikaci lze otevĹ™Ă­t rovnou v konkrĂ©tnĂ­m stavu pomocĂ­ URL parametrĹŻ:

```
http://127.0.0.1:8080/?view=dashboard          # obrazovka: chart | dashboard | console | settings
http://127.0.0.1:8080/?theme=light             # tĂ©ma
http://127.0.0.1:8080/?price=line&opacity=60   # cenovĂˇ kĹ™ivka s viditelnostĂ­ 60 % (default jsou svĂ­ÄŤky)
```

Parametry lze kombinovat.

---

## 17. ĹeĹˇenĂ­ potĂ­ĹľĂ­

| PĹ™Ă­znak | PĹ™Ă­ÄŤina a Ĺ™eĹˇenĂ­ |
|---|---|
| StavovĂˇ liĹˇta `IBKR: offline` | TWS nebÄ›ĹľĂ­ / nenĂ­ pĹ™ihlĂˇĹˇenĂ© / vypnutĂ© API / ĹˇpatnĂ˝ port. Zkontroluj TWS, pak IBKR Console â†’ Reconnect. |
| Alert â€ždelayed market dataâ€ś | ChybĂ­ live subskripce CME v Client Portal (viz kap. 2). |
| `Stale` mĂ­sto `â—Ź Live` | Data se pĹ™estala hĂ˝bat â€” obvykle vĂ˝padek TWSâ†”IB (v TWS bĂ˝vĂˇ hlĂˇĹˇka o connectivity). VyĹ™eĹˇĂ­ se samo, pĹ™Ă­padnÄ› re-login TWS. |
| LiĹˇta grafu ukazuje â€ždemo dataâ€ś | Aplikace se nedostala k API / ĹľĂˇdnĂˇ data pro dneĹˇek. Zkontroluj, Ĺľe sluĹľby bÄ›ĹľĂ­ (`docker compose ps`) a engine je online. |
| **Alert â€žOI nedoraziloâ€ś** | IBKR dodĂˇvĂˇ Open Interest pro ES opce jen jednou dennÄ› (rĂˇno, po publikaci CME). Do tĂ© doby heatmapa jede z volume a GEX ĂşrovnÄ› mohou bĂ˝t plochĂ©. Engine to zkouĹˇĂ­ kaĹľdĂ˝ch 30 minut sĂˇm â€” nenĂ­ tĹ™eba nic dÄ›lat. |
| PrĂˇzdnĂˇ heatmapa | Mimo obchodnĂ­ hodiny nevznikajĂ­ novĂ© snapshoty â€” pouĹľij playback pro pĹ™ehrĂˇnĂ­ poslednĂ­ho dne. |
| TWS spadlo po pĹ™ihlĂˇĹˇenĂ­ na mobilu | Limit jednoho pĹ™ihlĂˇĹˇenĂ­ IBKR. Znovu se pĹ™ihlas v TWS; aplikace se sama pĹ™ipojĂ­. |
| Aplikace nejde otevĹ™Ă­t (:8080) | `docker compose up -d` v adresĂˇĹ™i projektu; prvnĂ­ start po rebootu chvĂ­li trvĂˇ. |

---

## 18. ObchodnĂ­ ÄŤtenĂ­ â€” reĹľimy trhu, Dyn GEX a flip

Tahle kapitola pĹ™eklĂˇdĂˇ vrstvy aplikace do rozhodnutĂ­: **kdy hledat long, kdy short a kdy si sedÄ›t na rukĂˇch.**

### Co dÄ›lajĂ­ dealeĹ™i: tlumenĂ­ vs. zesilovĂˇnĂ­

Market makeĹ™i (dealeĹ™i) drĹľĂ­ protistranu opcĂ­ a prĹŻbÄ›ĹľnÄ› se zajiĹˇĹĄujĂ­ futures:

- **Long gamma (kladnĂ˝ NetGEX, zelenĂˇ):** zajiĹˇĹĄovĂˇnĂ­ je nutĂ­ **prodĂˇvat do rĹŻstu a nakupovat do poklesu** â†’ jdou proti pohybu, trh **tlumĂ­**. Cena se drĹľĂ­ v range, odrazy fungujĂ­.
- **Short gamma (zĂˇpornĂ˝ NetGEX, ÄŤervenĂˇ):** musĂ­ **kupovat do rĹŻstu a prodĂˇvat do poklesu** â†’ jdou s pohybem, trh **zesilujĂ­**. Trendy a prudkĂ© pohyby.

**KlĂ­ÄŤovĂ© pravidlo: reĹľim neĹ™Ă­kĂˇ smÄ›r, Ĺ™Ă­kĂˇ, KTERĂť TYP obchodu dnes funguje.** ZelenĂ˝ reĹľim = obchoduj nĂˇvraty (fade od hran). ÄŚervenĂ˝ reĹľim = obchoduj prĹŻrazy (momentum). NejÄŤastÄ›jĹˇĂ­ ztrĂˇty = fade v ÄŤervenĂ©m dni, honÄ›nĂ­ breakoutu v zelenĂ©m.

### Settle a gamma crunch

**Settle** = vypoĹ™ĂˇdĂˇnĂ­ expirace: u dennĂ­ch ES/NQ opcĂ­ **20:00 UTC (22:00 SELÄŚ, 16:00 New York)**. V tu chvĂ­li opce zaniknou a jejich gamma z trhu zmizĂ­ â€” model i mapa poÄŤĂ­tajĂ­ prĂˇvÄ› do tohoto okamĹľiku.

**Gamma crunch:** gamma opce je nejvÄ›tĹˇĂ­ pĹ™esnÄ› na striku a ÄŤĂ­m mĂ©nÄ› ÄŤasu zbĂ˝vĂˇ, tĂ­m je ĹˇpiÄŤatÄ›jĹˇĂ­ â€” rĂˇno ĹˇirokĂ˝ kopec pĹ™es nÄ›kolik strikes, veÄŤer ĂşzkĂˇ jehla na jednom striku. ZajiĹˇĹĄovacĂ­ toky tak majĂ­ poslednĂ­ 1â€“2 hodiny nejvÄ›tĹˇĂ­ pĂˇku: cena se buÄŹ **pĹ™ilepĂ­** k velkĂ©mu striku (pin), nebo po proraĹľenĂ­ **prudce akceleruje**. V Dyn GEX mapÄ› to vidĂ­Ĺˇ jako stahovĂˇnĂ­ barev do tenkĂ˝ch jasnĂ˝ch pĂˇsĹŻ smÄ›rem doprava.

### Jak ÄŤĂ­st Dyn GEX mapu (Mode â†’ Dyn GEX)

![Dyn GEX pole â€” modelovanĂ˝ NetGEX pĹ™es pĂˇsmo a ÄŤas](img/dyn-gex-pole.png)

- **Vlevo od pĹ™edÄ›lu** = namÄ›Ĺ™enĂˇ historie (profil minutu po minutÄ›, tehdejĹˇĂ­ IV/OI). **Vpravo od pĹ™edÄ›lu** (ztlumenĂ©, svislĂ˝ pĹ™edÄ›l = â€žteÄŹ") = modelovanĂˇ budoucnost do settle z aktuĂˇlnĂ­ho snapshotu.
- **ZĂˇĹ™ivÄ› zelenĂˇ = brzda i magnet zĂˇroveĹ.** Cena tam zpomalĂ­, lepĂ­ se, odrazy od hrany zĂłny jsou pravdÄ›podobnÄ›jĹˇĂ­ a cena se k nĂ­ vracĂ­ (pin).
- **ZĂˇĹ™ivÄ› ÄŤervenĂˇ = klouzaÄŤka.** Pohyb tudy zrychluje â€” odraz neÄŤekej, spĂ­Ĺˇ projetĂ­.
- **BledĂ© oblasti = vzduchoprĂˇzdno.** Nikdo tam nic nedrĹľĂ­, cena projde bez odporu.
- **RozhranĂ­ zelenĂ© a ÄŤervenĂ© v ÄŤase = drĂˇha dynamickĂ©ho flipu.**

### Flip: namÄ›Ĺ™enĂ˝ vs. dynamickĂ˝ = flip ZĂ“NA

V aplikaci jsou dva flipy â€” **obÄ› ÄŤĂˇry mÄ›Ĺ™Ă­ totĂ©Ĺľ dvÄ›ma metodami**:

| | Kde | Barva | Metoda |
|---|---|---|---|
| **NamÄ›Ĺ™enĂ˝ flip** | hlavnĂ­ graf | ĹľlutĂˇ ÄŤĂˇrkovanĂˇ | prĹŻchod nulou kumulativnĂ­ho NetGEX z reĂˇlnĂ©ho OI, interpolace mezi striky; driftuje pomalu (hlavnĂ­ vstup OI se pĹ™es den mÄ›nĂ­ mĂˇlo) |
| **DynamickĂ˝ flip** | pravĂ˝ panel (+ rozhranĂ­ barev v Dyn GEX mapÄ›) | ĹľlutĂˇ ÄŤĂˇrkovanĂˇ (slabĹˇĂ­) | nula Black-Scholes modelu na jemnÄ›jĹˇĂ­ mĹ™Ă­Ĺľce â€” hladĹˇĂ­ odhad â€žteÄŹ" |

**RozdĂ­l obou ÄŤar ber jako flip ZĂ“NU.** BlĂ­zko sebe = ostrĂˇ hranice reĹľimĹŻ, signĂˇly ÄŤitelnĂ©. RozjetĂ© = hranice rozmazanĂˇ â†’ **uvnitĹ™ zĂłny neobchoduj**, ÄŤekej, aĹľ cena opustĂ­ celĂ© pĂˇsmo.

### Playbook: zelenĂ˝ reĹľim (spot NAD flip zĂłnou)

- **LONG:** cena spadne na hranu silnĂ© zelenĂ© zĂłny / put wall. PotvrzenĂ­: svĂ­ÄŤky se zkracujĂ­, dolnĂ­ knoty, CumÎ” prodejnĂ­ tlak zpomaluje nebo diverguje. Vstup od hrany, cĂ­l stĹ™ed pĂˇsma / nejbliĹľĹˇĂ­ silnĂ˝ strike, **stop kousek POD zelenou zĂłnu** (kdyĹľ brzda selĹľe, nemĂˇĹˇ tam co dÄ›lat).
- **SHORT:** zrcadlovÄ› od call wall / hornĂ­ hrany zelenĂ©, zpÄ›t do stĹ™edu range. ProraĹľenĂ­ zdi o pĂˇr tickĹŻ bez follow-through = extra palivo pro fade.
- **Ruce pryÄŤ:** od honÄ›nĂ­ breakoutĹŻ â€” v zelenĂ©m dni vÄ›tĹˇinou selĹľou.

### Playbook: ÄŤervenĂ˝ reĹľim (spot POD flip zĂłnou / v ÄŤervenĂ©)

- **SHORT:** prĹŻraz poslednĂ­ zelenĂ© / flipu dolĹŻ s potvrzenĂ­m CumÎ” (padĂˇ s cenou). Vstup **s pohybem**, cĂ­l dalĹˇĂ­ zelenĂ˝ pĂˇs / put wall pod tebou, stop nad proraĹľenou ĂşroveĹ. RychlĂ© pohyby â†’ kratĹˇĂ­ drĹľenĂ­, rychlejĹˇĂ­ posun stopu.
- **LONG:** jedinĂ˝ spolehlivĂ˝ = **reclaim flipu** â€” nĂˇvrat nad Ĺľlutou, retest shora drĹľĂ­. CĂ­l prvnĂ­ zelenĂˇ zĂłna nad flipem.
- **Ruce pryÄŤ:** od â€žuĹľ to spadlo hodnÄ›" longĹŻ â€” zesilujĂ­cĂ­ dealeĹ™i je pĹ™ejedou.

### Playbook: veÄŤer (crunch, poslednĂ­ ~90 minut)

- **Pin trade (nad flipem, cena u jasnĂ©ho zelenĂ©ho pĂˇsu):** spot krouĹľĂ­ Â±pĂˇr bodĹŻ kolem velkĂ©ho striku a kaĹľdĂ© odskoÄŤenĂ­ se vracĂ­ â†’ **fade obou smÄ›rĹŻ k tomu striku**, malĂ© cĂ­le, tÄ›snĂ© stopy. VeÄŤer je brzda nejsilnÄ›jĹˇĂ­, tohle je nejspolehlivÄ›jĹˇĂ­ verze fade.
- **Akcelerace (cena opustĂ­ poslednĂ­ jasnĂ˝ pĂˇs):** dalĹˇĂ­ zelenĂˇ aĹľ o nÄ›kolik strikes dĂˇl, mezi tĂ­m vzduchoprĂˇzdno â†’ **jdi s prĹŻrazem**, cĂ­l dalĹˇĂ­ jasnĂ˝ pĂˇs, nic proti tomu nestav.
- **Ruce pryÄŤ:** od fade uprostĹ™ed vzduchoprĂˇzdna a od drĹľenĂ­ pin obchodu potĂ©, co cena pĂˇs opustĂ­ â€” crunch je binĂˇrnĂ­: buÄŹ lepĂ­, nebo katapultuje.

### Limity modelu (kdy mu nevÄ›Ĺ™it)

PravĂˇ (modelovanĂˇ) ÄŤĂˇst mapy vychĂˇzĂ­ z **aktuĂˇlnĂ­ho snĂ­mku IV a rannĂ­ho OI**: velkĂ˝ pĹ™Ă­liv novĂ©ho OI nebo skok volatility pĂˇsy pĹ™esklĂˇdĂˇ; dneĹˇnĂ­ ÄŤerstvĂ˝ positioning model nevidĂ­. Ber veÄŤernĂ­ plĂˇn z mapy ~hodinu pĹ™edem a prĹŻbÄ›ĹľnÄ› ovÄ›Ĺ™uj, Ĺľe pĂˇsy stojĂ­. Zdi navĂ­c nejsou stejnÄ› silnĂ©: cenovka zdi ukazuje **dominanci v %** (podĂ­l zdi na sĂ­le celĂ© strany profilu) a Ăşseky, kde zeÄŹ drĹľĂ­ mĂ©nÄ› neĹľ 15 % sĂ­ly strany, se kreslĂ­ ztlumenÄ› teÄŤkovanÄ› â€” slabĂˇ zeÄŹ je jen statistickĂ© maximum, ne opora pro obchod. **VĹľdy kĹ™Ă­ĹľovÄ› potvrÄŹ s mÄ›Ĺ™enou vrstvou** (walls, GEX Levels, CumÎ”) â€” model je navigace, mÄ›Ĺ™enĂ­ je terĂ©n.

---

## 19. SlovnĂ­ÄŤek pojmĹŻ

| Pojem | VĂ˝znam |
|---|---|
| **GEX** (Gamma Exposure) | Odhad, kolik dolarĹŻ musĂ­ dealeĹ™i hedgeovat na 1 bod pohybu podkladu. KladnĂ˝ = dealeĹ™i tlumĂ­ pohyb, zĂˇpornĂ˝ = zesilujĂ­. |
| **Flip (zero-gamma)** | Cena, kde kumulativnĂ­ NetGEX prochĂˇzĂ­ nulou â€” hranice mezi reĹľimem komprese a expanze volatility. |
| **DynamickĂ˝ flip** | ModelovĂˇ verze flipu: nula Dyn GEX kĹ™ivky (BS model, jemnÄ›jĹˇĂ­ mĹ™Ă­Ĺľka). S namÄ›Ĺ™enĂ˝m flipem tvoĹ™Ă­ **flip zĂłnu** (kap. 18). |
| **Dyn GEX (mĂłd)** | ModelovanĂ© pole NetGEX pro hypotetickĂ© ceny pĹ™es pĂˇsmo a ÄŤas â€” â€žjakou gammu potkĂˇ cena na Ăşrovni X v ÄŤase T". ZelenĂˇ tlumĂ­, ÄŤervenĂˇ zesiluje. |
| **Settle** | VypoĹ™ĂˇdĂˇnĂ­/konec Ĺľivotnosti expirace â€” dennĂ­ ES/NQ opce 20:00 UTC (22:00 SELÄŚ). Pak jejich gamma z trhu zmizĂ­. |
| **Gamma crunch** | RĹŻst ATM gammy s blĂ­ĹľĂ­cĂ­ se expiracĂ­ â€” zajiĹˇĹĄovacĂ­ toky majĂ­ veÄŤer nejvÄ›tĹˇĂ­ sĂ­lu (pin, nebo akcelerace). |
| **Î”-vĂˇĹľenĂ­** | Pruhy profilu Ă— delta opce = kolik futures dealer reĂˇlnÄ› drĹľĂ­. VeÄŤer polarizuje (OTMâ†’0, ITMâ†’1), proto strana pruhĹŻ â€žmizĂ­". |
| **Call wall / Put wall** | Strike s nejvÄ›tĹˇĂ­ koncentracĂ­ NetGEX nad/pod spotem. PravdÄ›podobnostnĂ­ zĂłna, ne bariĂ©ra â€” trĹľnĂ­ vĂ˝znam mĂˇ jen pĹ™i dostateÄŤnĂ© dominanci vĹŻÄŤi zbytku profilu (viz cenovka zdi s %); ĂşrovnÄ› se bÄ›hem dne pĹ™elĂ©vajĂ­, u 0DTE vĂ˝raznÄ›. |
| **Centroid (HVL)** | VĂˇĹľenĂ© tÄ›ĹľiĹˇtÄ› |NetGEX| profilu. |
| **Max Pain** | Strike, kde by pĹ™i expiraci vyprĹˇelo nejmĂ©nÄ› hodnoty opcĂ­ â€” trh k nÄ›mu v expiracĂ­ch ÄŤasto â€žpĹ™iĹˇpendlĂ­" (pinning). |
| **OI (Open Interest)** | PoÄŤet otevĹ™enĂ˝ch kontraktĹŻ; mÄ›nĂ­ se jednou dennÄ› (CME publikuje rĂˇno). |
| **Î”OI vs. vÄŤera** | ZmÄ›na OI proti pĹ™edchozĂ­mu dni â€” kde pĹ™es noc vznikly/zanikly pozice. |
| **Î” Flow C/P** | Delta-vĂˇĹľenĂ˝ opÄŤnĂ­ tok zvlĂˇĹˇĹĄ za call/put stranu â€” na kterĂ© stranÄ› se prĂˇvÄ› obchoduje. |
| **Cum Î”** | KumulativnĂ­ delta flow â€” souÄŤet (smÄ›r obchodu Ă— velikost Ă— delta Ă— multiplikĂˇtor) pĹ™es den. |
| **Hot zĂłna** | PĂˇsmo ATM strikes sledovanĂ© tick-by-tick pro pĹ™esnou klasifikaci agresora. |
| **Stale** | Data starĹˇĂ­ neĹľ 5 minut â€” vizuĂˇlnÄ› odliĹˇenĂˇ. |

---

*GEXLens Â· dokumentace je souÄŤĂˇstĂ­ repozitĂˇĹ™e [kEchiCZ/GEX](https://github.com/kEchiCZ/GEX). TechnickĂ˝ manuĂˇl pro sprĂˇvce a vĂ˝vojĂˇĹ™e: `docs/manual/ADMIN-MANUAL.md`.*

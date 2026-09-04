/** Legenda grafu (#346, rozšířeno #348): ukázka prvku + kdy podle něj cena roste a kdy klesá.

Obsah je **data**, ne JSX — ukázky se kreslí generickým `LegendSwatch` ze
stejných barevných konstant, jaké používá graf (`LEVEL_COLORS`, `SETUP_COLORS`,
`callColor`/`putColor`). Legenda se tak nemůže rozejít s tím, co uživatel
opravdu vidí; kdyby byly barvy opsané, tichý rozchod by nikdo neodhalil.

Každá položka nese `up`/`down` — konkrétní obraz v grafu pro růst a pokles.
Samotné „ukazuje sílu call strany" traderovi nepomůže; potřebuje vědět, jak to
vypadá, když cena poroste, a jak když bude klesat.
*/
import { useEffect } from 'react'
import { LEVEL_COLORS, OI_WALL_DASH, SECONDARY_WALL_DASH, SETUP_COLORS } from '../heatmap/overlays'
import { callColor, putColor } from '../heatmap/color'

const UP_COLOR = '#3ecf8e'
const DOWN_COLOR = '#f0616d'
/** Žebřík a GEX křivka mají vlastní odstíny — zrcadlí App.tsx a StrikeProfile. */
const LADDER_CALL = 'rgba(62,207,142,0.85)'
const GEX_FLIP = '#e8c14b'

function rgba(color: [number, number, number, number]): string {
  return `rgba(${color[0]},${color[1]},${color[2]},${(color[3] / 255).toFixed(2)})`
}

/** Jak se ukázka nakreslí. Odpovídá tomu, co kreslí canvas heatmapy. */
export type Swatch =
  /** Vodorovná čára úrovně. */
  | { kind: 'line'; color: string; dash?: number[]; width?: number; opacity?: number }
  /** Svislá čára (news marker, seance). */
  | { kind: 'vline'; color: string; width?: number; dash?: number[] }
  /** Barevný přechod buněk heatmapy od nuly k maximu. */
  | { kind: 'ramp'; to: string }
  /** Divergentní rampa signed vrstev (záporné ← 0 → kladné); barvy lze
      přebít pro palety ploch (#204). */
  | { kind: 'diverging'; pos?: string; neg?: string }
  /** Svíčky ceny. */
  | { kind: 'candles' }
  /** Plocha panelu nad/pod nulou. */
  | { kind: 'area'; positive: string; negative: string }
  /** Sloupce panelu. */
  | { kind: 'bars'; color: string; second?: string }
  /** GEX křivka v pravém profilu: zelená doprava, červená doleva, žlutý flip. */
  | { kind: 'gex' }
  /** Šipka signálu na cenové křivce se stopou platnosti (#295). */
  | { kind: 'signal' }
  /** Chip z hlavičky (stav sentimentu, tendence). */
  | { kind: 'chip'; color: string; label: string }

export interface LegendItem {
  name: string
  swatch: Swatch
  /** Kde to v aplikaci je — u prvků mimo hlavní graf. */
  where?: string
  /** Co to je. */
  what: string
  /** Konkrétní obraz v grafu, když cena roste. */
  up?: string
  /** …a když klesá. */
  down?: string
  /** Doplňující čtení nebo varování. */
  how?: string
}

export interface LegendSection {
  title: string
  note?: string
  items: LegendItem[]
}

export const LEGEND_SECTIONS: LegendSection[] = [
  {
    title: 'Úrovně v hlavním grafu',
    note: 'Vodorovné čáry přes celou šířku. Popisek nad čarou nese název a aktuální cenu, u zdí i dominanci v procentech. Úrovně nejsou signály — říkají, KDE trh nejspíš zareaguje, ne KDY se otočí.',
    items: [
      {
        name: 'Max Pain',
        swatch: { kind: 'line', color: LEVEL_COLORS.max_pain },
        where: 'Hlavní graf, plná čára; popisek vpravo u osy.',
        what: 'Strike, na kterém by při expiraci vypršelo bez hodnoty nejvíc otevřených opcí — cena, kde drtivá většina držitelů opcí prodělá nejvíc. Je to jedno číslo za celý den a mění se pomalu.',
        up: 'Cena je POD Max Painem a blíží se expirace → tah nahoru k němu. Typicky pomalé plíživé stoupání bez velkých svíček, které se u Max Painu zastaví.',
        down: 'Cena je NAD Max Painem před expirací → stejně plíživý tah dolů k němu. Odpoledne v den expirace je to nejsilnější.',
        how: 'Je to gravitace, ne příkaz. Silná zpráva Max Pain přebije a cena od něj utíká celý den.',
      },
      {
        name: 'Gamma Flip',
        swatch: { kind: 'line', color: LEVEL_COLORS.flip, dash: [6, 5] },
        where: 'Hlavní graf, žlutá čárkovaná čára.',
        what: 'Úroveň, kde kumulativní NetGEX mění znaménko — hranice mezi kladnou a zápornou gamma dealerů. Ze všech úrovní mění charakter dne nejvíc.',
        up: 'Cena NAD flipem: dealeři prodávají do růstu a kupují do poklesu, takže růst je pomalý a plynulý, propady se rychle vykupují a rozpětí svíček je malé. Nákup poklesu tady funguje.',
        down: 'Cena PROPADNE POD flip: dealeři pohyb zesilují, svíčky se prodlouží, propad zrychlí a nevykupuje se. Tady vznikají prudké výprodeje — pod flipem se poklesy nekupují.',
        how: 'Nejdůležitější je samotný průraz. Když cena flip prorazí dolů a udrží se, čekej vyšší volatilitu do konce dne.',
      },
      {
        name: 'Call zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.call_wall },
        where: 'Hlavní graf, zelená čára nad cenou.',
        what: 'Strike nad spotem s největší koncentrací call gamma. Číslo za popiskem je dominance — jak velký podíl síly call strany zeď drží. Nad 30 % je silná, pod 15 % se kreslí ztlumeně.',
        up: 'Cena zeď PRORAZÍ a udrží se nad ní → hedging se obrátí, dealeři začnou dokupovat a růst zrychlí (gamma squeeze). Poznáš to podle rychlé svíčky, která zeď protne a nevrátí se pod ni.',
        down: 'Cena se o zeď opře a odmítne ji — dlouhý horní knot těsně pod čarou → obrat dolů. To je typičtější obraz než průraz.',
      },
      {
        name: 'Put zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.put_wall },
        where: 'Hlavní graf, červená čára pod cenou.',
        what: 'Strike pod spotem s největší koncentrací put gamma, opět s dominancí v popisku.',
        up: 'Cena o zeď zavadí a odrazí se — dlouhý spodní knot na čáře → pokračování nahoru. Dealeři u put zdi kupují.',
        down: 'Cena zeď PRORAZÍ dolů a udrží se pod ní → podpora zmizela a otevírá se prostor k rychlému pádu na druhou put zeď nebo další strike.',
      },
      {
        name: '2. call zeď / 2. put zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.call_wall_2, dash: [...SECONDARY_WALL_DASH] },
        where: 'Hlavní graf, tečkovaně a poloprůhledně; zapíná přepínač „2. zeď“.',
        what: 'Druhá nejsilnější zeď na dané straně. Zobrazí se jen když existuje — některé dny na jedné straně žádná není.',
        up: 'Po průrazu call zdi je druhá call zeď nejbližší další brzda, tedy přirozený cíl pohybu nahoru.',
        down: 'Po průrazu put zdi míří pokles obvykle na druhou put zeď. Je to nejbližší místo, kde se pád má o co zastavit.',
      },
      {
        name: 'OI zeď (call / put)',
        swatch: { kind: 'line', color: LEVEL_COLORS.oi_call_wall, dash: [...OI_WALL_DASH] },
        where: 'Hlavní graf, jemně tečkovaně; studenější odstín než gamma zdi.',
        what:
          'Strike s největším OTEVŘENÝM ZÁJMEM na své straně — jiná veličina než call/put zeď výš. ' +
          'Ty jsou maximem gamma profilu (kde dealeři hedgují teď), tohle je maximum OI (kolik pozic ' +
          'tam leží). Počítá se z denního archivu, který pokrývá i křídla mimo dosah gamma profilu, ' +
          'takže OI zeď bývá dál od ceny. Číslo v popisku je podíl na OI své strany: nízké procento ' +
          'znamená plochý profil, kde je „zeď“ jen nejvyšší z mnoha srovnatelných striků. ' +
          'Druhé číslo „% outright“ (v1.14, #1007) říká, kolik dnešního objemu na striku vytiskly ' +
          'outright obchody: zeď z outright drží dealer jako plnou pozici, zeď ze spreadů má ' +
          'gammu částečně vykompenzovanou — zatím jen informace, bez vlivu na kreslení.',
        up: 'Blíž k expiraci působí velké OI jako magnet — cena k němu bývá tažena, protože se tam zavírají pozice. Shoda OI zdi s call zdí je silná úroveň; když se rozejdou, gamma zeď působí teď, OI zeď až k expiraci.',
        down: 'Velké put OI pod trhem je místo, kde se pád má o co opřít, i když tam gamma zeď není. Pozor: sám o sobě to není hedging, takže reakce nebývá tak okamžitá jako u put zdi.',
        how: 'Nic se tu nedopočítává — bez OI se hladina nekreslí. Gamma pro tyhle striky neznáme, takže je záměrně nemícháme mezi gamma zdi.',
      },
      {
        name: 'GEX žebřík',
        swatch: { kind: 'line', color: LADDER_CALL, dash: [6, 5] },
        where:
          'Hlavní graf, zapíná přepínač „GEX žebřík“. Zelené příčky nad cenou, červené pod ní.',
        what: 'Všechny významné striky k aktuální pozici přehrávání, ne jen ta nejsilnější zeď. Číslo za cenou je podíl na síle dané strany, takže vidíš i pořadí důležitosti.',
        up: 'Cena stoupá mezi příčkami. Příčka s vysokým podílem je brzda; příčky s malým podílem cena obvykle projede bez zastavení. Řídká oblast nad cenou = volný prostor k růstu.',
        down: 'Při poklesu fungují červené příčky jako schody — u silných se cena zastaví, přes slabé propadne. Prázdno pod cenou znamená, že pád nemá kde zpomalit.',
      },
      {
        name: 'Těžiště',
        swatch: { kind: 'line', color: LEVEL_COLORS.centroid, dash: [6, 5] },
        where: 'Hlavní graf, fialová čárkovaná čára.',
        what: 'Vážený střed opčního pozicingu — kde leží masa otevřených kontraktů. Pomalá a slabá úroveň.',
        up: 'Cena hluboko pod těžištěm → mírný tah nahoru, trh se vzdálil od hlavní koncentrace pozic.',
        down: 'Cena vysoko nad těžištěm → mírný tah dolů. Sama o sobě obrat nezpůsobí, ber ji jako kontext.',
      },
      {
        name: 'Slabá zeď',
        swatch: { kind: 'line', color: LEVEL_COLORS.call_wall, dash: [2, 3], opacity: 0.4 },
        where: 'Hlavní graf, ztlumený tečkovaný úsek zdi.',
        what: 'Úsek, kde dominance klesla pod 15 % — síla strany je roztříštěná mezi víc strikes.',
        how: 'Odraz tady nečekej a nestav na ni vstup. Cena přes takovou úroveň obvykle projde, jako by tam nebyla.',
      },
    ],
  },
  {
    title: 'Navržený setup',
    note: 'Objeví se, jen když detektor najde příležitost. Ke grafu patří karta s popisem, poměrem rizika a důvěrou.',
    items: [
      {
        name: 'Vstup',
        swatch: { kind: 'line', color: SETUP_COLORS.entry, dash: [6, 5] },
        where: 'Hlavní graf, modrá čárkovaná čára.',
        what: 'Cena, na které setup počítá se vstupem do pozice. Směr je na kartě setupu.',
        up: 'U LONG setupu leží cíl NAD vstupem a stop pod ním.',
        down: 'U SHORT setupu je to obráceně — cíl POD vstupem, stop nad ním.',
      },
      {
        name: 'Cíl',
        swatch: { kind: 'line', color: SETUP_COLORS.target, dash: [6, 5] },
        where: 'Hlavní graf, zelená čárkovaná čára.',
        what: 'Cílová cena setupu. Zásah cílem setup uzavírá jako úspěšný.',
      },
      {
        name: 'Stop',
        swatch: { kind: 'line', color: SETUP_COLORS.stop, dash: [6, 5] },
        where: 'Hlavní graf, červená čárkovaná čára.',
        what: 'Ochranná úroveň. Zásah stopem setup uzavírá se ztrátou — v rámci jedné svíčky se vyhodnocuje vždy dřív než cíl, aby statistika nelhala.',
      },
    ],
  },
  {
    title: 'Heatmapa',
    note: 'Barva buňky = velikost hodnoty na daném striku a v dané minutě. Co se měří, určuje přepínač Mode (OI, Vol, VEX…), jak se to škáluje, přepínač Scale. Svislá osa jsou striky, vodorovná čas.',
    items: [
      {
        name: 'Call vrstva',
        swatch: { kind: 'ramp', to: rgba(callColor(1)) },
        where: 'Hlavní graf, zelenomodré buňky.',
        what: 'Sytost roste s velikostí hodnoty na call straně. Souvislý sytý pruh přes několik minut je zeď.',
        up: 'Zelený pruh nad cenou bledne nebo se posouvá výš → odpor slábne a nad cenou se uvolňuje prostor.',
        down: 'Nový sytý zelený pruh se rozsvítí těsně nad cenou → čerstvě postavený strop, růst má kde narazit.',
      },
      {
        name: 'Put vrstva',
        swatch: { kind: 'ramp', to: rgba(putColor(1)) },
        where: 'Hlavní graf, červené buňky.',
        what: 'Totéž pro put stranu, obvykle pod cenou.',
        up: 'Sytý červený pruh těsně pod cenou = pevná podložka, o kterou se dá opřít růst.',
        down: 'Červená pod cenou vybledne nebo zmizí → podpora se rozpustila a pod cenou je prázdno.',
      },
      {
        name: 'Vrstva ±',
        swatch: { kind: 'diverging' },
        where: 'Hlavní graf v módech Vol ±, OI±All, VEX ±.',
        what: 'Kreslí převahu, ne součet: zeleně kde vede call strana, červeně kde put.',
        up: 'Zelená převaha na strikách nad cenou → call strana staví pozici výš, trh počítá s růstem.',
        down: 'Červená převaha pod cenou → put strana sílí, poptávka po ochraně roste.',
      },
      {
        name: 'Zdroj OI: FA odhad',
        swatch: { kind: 'chip', color: '#e8c14b', label: 'FA' },
        where:
          'Přepínač „OI" v řádku přepínačů (Měřené / FA odhad). Aktivní FA značí tečkovaný okraj přepínače a badge „FA odhad" nad grafem.',
        what: 'OI vrstvy heatmapy, Dyn GEX podklad i GEX křivka pravého profilu se místo měřeného ranního archivu počítají z odhadu OI_est = ranní OI + α·čistý klasifikovaný tok (ADR-0011). Vidí tedy i positioning postavený DNES, který ranní OI nezná — u 0DTE je to většina gammy. FA levels linie (čárkované) vychází z téhož odhadu.',
        up: 'Nová zeď/flip z FA odhadu výš než měřené → dnešní tok staví positioning nad cenou; měřené vrstvy ho uvidí až zítra ráno.',
        down: 'FA put zeď/flip níž než měřené → dnes se staví ochrana pod trhem.',
        how: 'Je to model, ne měření: intradenní tok je klasifikovaný odhad a α hrubý kalibrační faktor (engine ho každé ráno ladí proti skutečnému ΔOI — badge „FA α" ve stavové liště). Vol, Δ Flow, Cum Δ i OI Δ složka pravého profilu zůstávají VŽDY měřené. Default je Měřené; volba se pamatuje per symbol.',
      },
      {
        name: 'Dyn GEX plocha',
        swatch: { kind: 'diverging' },
        where: 'Podkladová vrstva pod heatmapou; dropdown „Dyn plocha“ → Dyn GEX.',
        what: 'Modelovaný NetGEX přes cenové pásmo a čas (ADR-0009): zelená = dealeři pohyb tlumí, červená = zesilují. Není to směr, je to reakce trhu na pohyb. Jednotka dle přepínače „Jednotka“ (#569): $/1 % (výchozí) váží každou hladinu P²/100 — kolik dolarů podkladu dealeři přeobchodují při pohybu o 1 %, vyšší cenové hladiny mají přirozeně větší váhu; $/bod je surové pole bez váhy.',
        up: 'Silné zelené pásmo nad cenou → strop z tření; pohyb vzhůru se do něj bude zpomalovat.',
        down: 'Cena v červené zóně → pohyby se zesilují oběma směry, propady zrychlují.',
        how: 'Přepnutí jednotky mění tvar plochy a bílé čárkované Walls (Peak/Ridge) nad ní — zdi, levels, flip, Max Pain a setupy z enginu se nemění nikdy.',
      },
      {
        name: 'Dyn Charm plocha',
        swatch: { kind: 'diverging', pos: 'rgb(235,170,40)', neg: 'rgb(70,130,240)' },
        where: 'Podkladová vrstva pod heatmapou; dropdown „Dyn plocha“ → Dyn Charm.',
        what: 'Modelovaná změna dealer delta-hedge jen plynutím času (dDelta/dČas za den). Jantarová = kladný charm, modrá = záporný. Kvantifikuje EOD toky: OTM delty ke konci dne „vyhnívají“ a dealeři musí hedge dorovnávat i bez pohybu ceny.',
        up: 'Velká charm koncentrace POD spotem → do close předvídatelný tok nákupů (proslulé „charm flows“ poslední hodinu).',
        down: 'Koncentrace NAD spotem → tok prodejů do close. Nejsilnější v expirační dny.',
        how: 'Model z uložené IV a OI (stejný dealer model jako Dyn GEX) — čti jako mapu toků od času, ne jako signál. Jednotka sdílí přepínač s Dyn GEX (#569) — význam barvy se přepnutím plochy nemění.',
      },
      {
        name: 'Dyn Vanna plocha',
        swatch: { kind: 'diverging', pos: 'rgb(20,190,170)', neg: 'rgb(150,80,230)' },
        where: 'Podkladová vrstva pod heatmapou; dropdown „Dyn plocha“ → Dyn Vanna.',
        what: 'Modelovaná změna dealer delta-hedge se změnou implikované volatility (dDelta/dVol za 1 % IV). Teal = kladná vanna, fialová = záporná. Ukazuje, na kterých úrovních je trh nejcitlivější na pohyb IV.',
        up: 'Po události IV klesá → na úrovních s velkou vannou dealeři dorovnávají hedge nákupy (klasický „vanna rally“ pátek po opexu).',
        down: 'Skok IV nahoru (šok) obrací tytéž toky do prodejů.',
        how: 'Spolu s Dyn GEX a Charm tři síly expiračních dnů: GEX = brzdy/plyn od spotu, charm = toky od času, vanna = toky od volatility. Jednotka sdílí přepínač s Dyn GEX (#569).',
      },
      {
        name: 'Stará data',
        swatch: { kind: 'ramp', to: 'rgba(150,150,150,0.85)' },
        where: 'Hlavní graf, odbarvené a zprůhledněné buňky.',
        what: 'Kotace na tom striku je starší než 5 minut. Není to prázdno, ale neaktuálnost.',
        how: 'Na takovém striku nestav rozhodnutí — zeď tam možná už není, jen o tom zatím nevíme.',
      },
      {
        name: 'Projekce',
        swatch: { kind: 'ramp', to: 'rgba(20,200,170,0.45)' },
        where: 'Hlavní graf vpravo za svislým předělem „projekce →“.',
        what: 'Poslední naměřený sloupec protažený do konce seance, se sníženou sytostí.',
        how: 'Ukazuje, kde by zdi ležely, kdyby se pozicing už neměnil. Není to měření ani předpověď ceny — nečti z ní směr.',
      },
    ],
  },
  {
    title: 'Cena',
    items: [
      {
        name: 'Svíčky',
        swatch: { kind: 'candles' },
        where: 'Hlavní graf nad heatmapou; přepínačem Cena lze přepnout na linku.',
        what: 'Minutové OHLC; zelená roste, červená klesá. Poslední svíčka je rozdělaná a dokresluje se živě.',
        up: 'Dlouhé spodní knoty na úrovni = kupci ji brání, cena se od ní odráží nahoru.',
        down: 'Dlouhé horní knoty na úrovni = prodejci ji brání, cena se od ní odráží dolů.',
      },
    ],
  },
  {
    title: 'Značky v grafu',
    items: [
      {
        name: 'Zpráva',
        swatch: { kind: 'vline', color: 'rgba(20,184,166,0.95)', width: 2 },
        where: 'Hlavní graf, svislá čára s ikonou kategorie v minutě vydání.',
        what: 'Zelenomodrá je pozitivní zpráva, červená negativní, šedá neutrální nebo teprve plánovaná. Jas a tloušťka odpovídají důležitosti — okrajová zpráva je sotva vidět, FOMC křičí.',
        up: 'Pohyb nahoru hned po zelenomodré značce je reakce na zprávu, ne na pozicing — zdi ho nemusí zastavit.',
        down: 'Totéž dolů po červené značce. Šedá značka vpředu je plánovaný event: do jeho času čekej klidnější trh a pak skok.',
      },
      {
        name: 'Seance',
        swatch: { kind: 'vline', color: 'rgba(125,133,150,0.8)', dash: [4, 4] },
        where: 'Hlavní graf, svislé šedé čáry s popiskem.',
        what: 'Otevření a zavření hlavních trhů — Sydney, Šanghaj, Frankfurt, Londýn, US.',
        how: 'Na těchto hranicích se mění likvidita. US open bývá zlom dne: noční pohyb se často otočí a teprve tady vzniká skutečný směr.',
      },
      {
        name: 'Signál ze zpráv',
        swatch: { kind: 'signal' },
        where:
          'Hlavní graf, trojúhelník na cenové křivce; zapíná dropdown „Signály“ (Off / NEWS / COMBINED).',
        what: 'Long/Short nápověda ze zpráv: vznikne, jen když je potvrzený denní stav (RiskOn/RiskOff), přišla čerstvá zpráva ve směru stavu A její typ má v historii dost změřených reakcí se spolehlivou úspěšností (n ≥ 30, spodní mez hit-rate > 50 %). Sytost šipky = síla; vodorovná stopa vede do konce platnosti. Režim NEWS bere jen zprávy, COMBINED navíc vyžaduje souhlas GEX kontextu (cena vs. flip, sklon Cum Δ).',
        up: '▲ zelenomodrá pod cenou = long signál. Tooltip u kurzoru ukáže zdůvodnění, počet vzorků a hit-rate.',
        down: '▼ červená nad cenou = short signál. Badge ⚠ u šipky znamená nepotvrzenou změnu stavu — signál může předčasně vyhasnout.',
        how: 'Není to příkaz k obchodu — je to statistika minulých reakcí na podobné zprávy. Dokud model nemá dost dat, dropdown ukazuje „sbírám data“ a signály nechodí.',
      },
    ],
  },
  {
    title: 'Chipy v hlavičce',
    note: 'Souhrnný kontext dne vedle ceny — klik na chip otevře detail.',
    items: [
      {
        name: 'Tendence ceny',
        swatch: { kind: 'chip', color: '#7d8596', label: 'Neutral' },
        where: 'Hlavička, pětitečková škála Strong Short · Short · Neutral · Long · Strong Long.',
        what: 'Souhrn dvanácti ukazatelů z této legendy do jednoho čísla — každá složka (poloha vůči flipu, zdem, Max Painu, těžišti, sklon a rozchod Cum Δ, Δ Flow, SentIndex, gamma/charm/vanna v místě ceny) hlasuje −1 až +1 a skóre je vážený průměr s nejvyšší vahou Gamma Flipu. Klik rozbalí rozpad hlasů: vidíš, KTERÁ složka indikátor táhne — žádná černá skříňka.',
        up: 'Long / Strong Long: převažují růstové podmínky. Zelený chip a tečka vpravo.',
        down: 'Short / Strong Short: převažují klesající podmínky. Červený chip a tečka vlevo.',
        how: 'Badge „nekalibrováno“ je přiznání, že váhy zatím nejsou ověřené proti datům — indikátor popisuje positioning a tok, NENÍ to doporučení k obchodu. Neutral je schválně široký: dokud si složky odporují, raději mlčí.',
      },
      {
        name: 'Stav sentimentu',
        swatch: { kind: 'chip', color: DOWN_COLOR, label: 'RISK OFF' },
        where: 'Hlavička, zelený RISK ON / červený RISK OFF / šedý NEUTRAL.',
        what: 'Dlouhodobá nálada trhu z denních uzávěrů indexu zpráv (vlny nad MA5/MA10 s adaptivním prahem potvrzení). Na rozdíl od panelu Sentiment (minuty) se mění nejvýš jednou denně. Klik otevře sparkline dnešního indexu, klouzavé průměry a aktivní témata.',
        up: 'RISK ON: potvrzená pozitivní vlna — prostředí přeje růstu indexů.',
        down: 'RISK OFF: potvrzená negativní vlna — útěk od rizika, ES/NQ pod tlakem.',
        how: 'Pulsující tečka = nepotvrzená intradenní změna; potvrdí ji až denní close. Stav řídí i vznik signálů (long jen při RiskOn, short jen při RiskOff).',
      },
    ],
  },
  {
    title: 'Panely pod grafem',
    note: 'Sdílejí časovou osu s hlavním grafem i crosshair — hodnota pod kurzorem se ukazuje vpravo. Které panely jsou vidět, řídí přepínače nad grafem.',
    items: [
      {
        name: 'Vol',
        swatch: { kind: 'bars', color: 'rgba(125,133,150,0.8)' },
        where: 'První panel pod grafem.',
        what: 'Zobchodovaný objem futures za minutu.',
        up: 'Průraz zdi nahoru na vysokém sloupci → pohyb má za sebou skutečné obchody a spíš vydrží.',
        down: 'Průraz na nízkém objemu se často vrací zpátky — je to past, ne směr.',
      },
      {
        name: 'Opt Vol',
        swatch: { kind: 'bars', color: UP_COLOR, second: DOWN_COLOR },
        where: 'Druhý panel; zeleně call, červeně put.',
        what: 'Kolik opčních kontraktů se v dané minutě zobchodovalo, rozdělené na strany.',
        up: 'Skok zelených sloupců → čerstvý zájem o cally. Nová zeď se často objeví do pár minut po takovém skoku.',
        down: 'Skok červených sloupců → nakupuje se ochrana, staví se put pozice pod cenou.',
      },
      {
        name: 'Δ Flow C/P',
        swatch: { kind: 'bars', color: UP_COLOR, second: DOWN_COLOR },
        where: 'Třetí panel; zeleně call strana, červeně put.',
        what: 'Tentýž opční tok jako Opt Vol, ale přepočtený deltou na směrovou váhu. Sto kontraktů hluboko OTM s deltou 0,05 hne trhem jinak než sto kontraktů na penězích s deltou 0,5 — Δ Flow to rozliší, holé volume ne.',
        up: 'Zelená výrazně přebíjí červenou → směrová váha je na call straně. Dealeři se proti tomu zajišťují nákupem futures, což cenu podpírá.',
        down: 'Červená přebíjí zelenou → váha je na put straně a zajištění tlačí cenu dolů.',
        how: 'Neříká, kdo byl agresor — jen na které straně a v jaké směrové váze se obchodovalo. Ber ho jako váhu toku, ne jako důkaz nákupu či prodeje.',
      },
      {
        name: 'Cum Δ',
        swatch: { kind: 'area', positive: UP_COLOR, negative: DOWN_COLOR },
        where: 'Čtvrtý panel; plocha nad nulou zeleně, pod nulou červeně.',
        what: 'Kumulativní delta futures — průběžný součet toho, jestli obchody vznikaly agresivním nákupem, nebo prodejem. Na rozdíl od Δ Flow tady agresora známe.',
        up: 'Cena roste A Cum Δ roste s ní → za růstem stojí agresivní kupci, pohyb má podporu a spíš pokračuje.',
        down: 'Cena klesá A Cum Δ klesá → agresivní prodejci, propad má za sebou skutečný tok.',
        how: 'Nejcennější je rozchod: cena udělá nové maximum, ale Cum Δ ne → růst už netlačí kupci a často následuje obrat dolů. Obráceně stejně.',
      },
      {
        name: 'Sentiment',
        swatch: { kind: 'area', positive: UP_COLOR, negative: DOWN_COLOR },
        where: 'Poslední panel.',
        what: 'Index nálady ze zpracovaných zpráv — spojitá řada, ve které vliv zprávy postupně doznívá.',
        up: 'Kladné pásmo = risk-on: investoři jsou ochotní nést riziko, peníze tečou do akcií a ES/NQ mají sklon růst. Index roste = zprávy tuhle chuť posilují.',
        down: 'Záporné pásmo = risk-off: útěk do bezpečí, peníze odtékají z akcií do dluhopisů a dolaru, ES/NQ jsou pod tlakem. Prudký propad indexu je obvykle jedna silná negativní zpráva.',
        how: 'Je to kontext, ne vstupní signál. Trh na zprávu zareaguje v řádu minut, index doznívá mnohem déle. V Daily timeframe se panel kreslí jako OHLC svíčky — open ukazuje, co z nočních a víkendových zpráv do rána reálně zbylo, rozkmit svíčky velikost denních výkyvů nálady.',
      },
    ],
  },
  {
    title: 'Profil vpravo',
    items: [
      {
        name: 'Vol + OI',
        swatch: { kind: 'bars', color: UP_COLOR, second: DOWN_COLOR },
        where:
          'Pravý panel, vodorovné pruhy per strike. Sdílí osu Y s grafem, takže pruh je vždy v úrovni svého striku.',
        what:
          'Otevřené kontrakty a objem na každém striku — call zeleně, put červeně. Objemová část ' +
          'sloupce má dva tóny (v1.14, #1007): plná sytost = outright obchody, které vytiskly trade ' +
          'se stranou od burzy; tlumená = strukturovaný a dohodnutý objem (nohy spreadů, bloky), ' +
          'který CME jako obchod nevysílá. Není to šrafování — to patří jen chybějícímu OI.',
        up: 'Krátké pruhy nad cenou = řídký pozicing = málo odporu nad ní, prostor k růstu.',
        down: 'Nejdelší pruhy jsou přesně ty zdi, které v grafu vidíš jako vodorovné čáry. Dlouhý pruh pod cenou je podpora.',
      },
      {
        name: 'GEX křivka',
        swatch: { kind: 'gex' },
        where:
          'POZOR — není v hlavním grafu. Je to křivka v pravém profilu, zapíná se chipem „GEX“ v jeho hlavičce.',
        what: 'Modelovaný NetGEX přes cenové pásmo. Zelená vyčnívá doprava = kladná gamma, dealeři tlumí. Červená doleva = záporná gamma, dealeři zesilují. Žlutá značka je průchod nulou, tedy dynamický gamma flip. Jednotka dle přepínače u Dyn plochy (#569): $/1 % (výchozí, váha P²/100 per hladina) nebo $/bod.',
        up: 'Cena v zeleném pásmu → pohyby se tlumí, čekej menší rozsah a návraty k průměru; růst bude pozvolný.',
        down: 'Cena v červeném pásmu → pohyby se zesilují, propady zrychlují a nevykupují se.',
        how: 'Není to Max Pain a nemá s ním nic společného. Max Pain je jedno číslo a v hlavním grafu je to plná vodorovná čára; GEX křivka je průběh přes celé cenové pásmo v pravém profilu.',
      },
    ],
  },
]

const SWATCH_W = 62
const SWATCH_H = 18

/** Ukázka prvku — kreslí se stejnými barvami, jaké používá graf. */
export function LegendSwatch({ swatch }: { swatch: Swatch }) {
  const midY = SWATCH_H / 2
  const common = { width: SWATCH_W, height: SWATCH_H, className: 'legend-swatch' }
  if (swatch.kind === 'line') {
    return (
      <svg {...common} role="presentation">
        <line
          x1={2}
          y1={midY}
          x2={SWATCH_W - 2}
          y2={midY}
          stroke={swatch.color}
          strokeWidth={swatch.width ?? 1.5}
          strokeDasharray={swatch.dash?.join(' ')}
          opacity={swatch.opacity ?? 1}
        />
      </svg>
    )
  }
  if (swatch.kind === 'vline') {
    return (
      <svg {...common} role="presentation">
        <line
          x1={SWATCH_W / 2}
          y1={1}
          x2={SWATCH_W / 2}
          y2={SWATCH_H - 1}
          stroke={swatch.color}
          strokeWidth={swatch.width ?? 1.5}
          strokeDasharray={swatch.dash?.join(' ')}
        />
      </svg>
    )
  }
  if (swatch.kind === 'signal') {
    // Šipka long na cenové křivce + stopa platnosti (zrcadlí Heatmap #295)
    return (
      <svg {...common} role="presentation">
        <polyline
          points={`2,7 14,5 24,9 34,6 ${SWATCH_W - 2},8`}
          fill="none"
          stroke={UP_COLOR}
          strokeWidth={1.2}
          opacity={0.6}
        />
        <line
          x1={24}
          y1={9}
          x2={SWATCH_W - 4}
          y2={9}
          stroke="rgba(20,184,166,0.4)"
          strokeWidth={1}
        />
        <polygon points="24,11 19,16 29,16" fill="rgba(20,184,166,0.95)" />
      </svg>
    )
  }
  if (swatch.kind === 'chip') {
    return (
      <svg {...common} role="presentation">
        <rect
          x={1}
          y={2}
          width={SWATCH_W - 2}
          height={SWATCH_H - 4}
          rx={(SWATCH_H - 4) / 2}
          fill="none"
          stroke={swatch.color}
          opacity={0.7}
        />
        <text
          x={SWATCH_W / 2}
          y={midY + 3.5}
          textAnchor="middle"
          fontSize={9}
          fontWeight={600}
          fill={swatch.color}
        >
          {swatch.label}
        </text>
      </svg>
    )
  }
  if (swatch.kind === 'gex') {
    // Svislá osa nuly, kladná část doprava zeleně, záporná doleva červeně
    const axis = SWATCH_W / 2
    return (
      <svg {...common} role="presentation">
        <path
          d={`M${axis},1 C${axis + 16},4 ${axis + 14},7 ${axis},9`}
          fill={UP_COLOR}
          opacity={0.8}
        />
        <path
          d={`M${axis},9 C${axis - 18},11 ${axis - 12},15 ${axis},17`}
          fill={DOWN_COLOR}
          opacity={0.8}
        />
        <line x1={axis} y1={1} x2={axis} y2={SWATCH_H - 1} stroke="var(--border)" />
        <line x1={axis - 5} y1={9} x2={axis + 5} y2={9} stroke={GEX_FLIP} strokeWidth={1.5} />
      </svg>
    )
  }
  if (swatch.kind === 'ramp' || swatch.kind === 'diverging') {
    const id = `ramp-${swatch.kind === 'ramp' ? swatch.to : 'div'}`.replace(/[^a-z0-9]/gi, '')
    return (
      <svg {...common} role="presentation">
        <defs>
          <linearGradient id={id} x1="0" x2="1">
            {swatch.kind === 'ramp' ? (
              <>
                <stop offset="0%" stopColor={swatch.to} stopOpacity={0} />
                <stop offset="100%" stopColor={swatch.to} stopOpacity={1} />
              </>
            ) : (
              <>
                <stop offset="0%" stopColor={swatch.neg ?? rgba(putColor(1))} />
                <stop offset="50%" stopColor="rgba(0,0,0,0)" />
                <stop offset="100%" stopColor={swatch.pos ?? rgba(callColor(1))} />
              </>
            )}
          </linearGradient>
        </defs>
        <rect x={1} y={1} width={SWATCH_W - 2} height={SWATCH_H - 2} fill={`url(#${id})`} />
      </svg>
    )
  }
  if (swatch.kind === 'candles') {
    return (
      <svg {...common} role="presentation">
        {[
          { x: 14, color: UP_COLOR, top: 4, bottom: 13 },
          { x: 31, color: DOWN_COLOR, top: 6, bottom: 15 },
          { x: 48, color: UP_COLOR, top: 3, bottom: 11 },
        ].map((candle) => (
          <g key={candle.x} stroke={candle.color} fill={candle.color}>
            <line x1={candle.x} y1={1} x2={candle.x} y2={SWATCH_H - 1} strokeWidth={1} />
            <rect
              x={candle.x - 3}
              y={candle.top}
              width={6}
              height={candle.bottom - candle.top}
              stroke="none"
            />
          </g>
        ))}
      </svg>
    )
  }
  if (swatch.kind === 'area') {
    return (
      <svg {...common} role="presentation">
        <polygon points={`2,${midY} 20,4 34,7 46,${midY}`} fill={swatch.positive} opacity={0.8} />
        <polygon
          points={`46,${midY} 52,13 58,11 ${SWATCH_W - 2},${midY}`}
          fill={swatch.negative}
          opacity={0.8}
        />
        <line x1={2} y1={midY} x2={SWATCH_W - 2} y2={midY} stroke="var(--border)" />
      </svg>
    )
  }
  return (
    <svg {...common} role="presentation">
      {[3, 12, 21, 30, 39, 48].map((x, index) => {
        const isSecond = swatch.second !== undefined && index % 2 === 1
        const height = [10, 6, 14, 8, 12, 5][index]
        return (
          <rect
            key={x}
            x={x}
            y={SWATCH_H - 1 - height}
            width={6}
            height={height}
            fill={isSecond ? swatch.second : swatch.color}
          />
        )
      })}
    </svg>
  )
}

/** Modální legenda. Zavírá se křížkem, klávesou Esc i klikem mimo obsah. */
export function Legend({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="legend-backdrop" onClick={onClose} role="presentation">
      <div
        className="legend-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Legenda grafu"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="legend-header">
          <h2>Legenda grafu</h2>
          <button className="legend-close" onClick={onClose} aria-label="Zavřít legendu">
            ×
          </button>
        </header>
        <div className="legend-body">
          {LEGEND_SECTIONS.map((section) => (
            <section key={section.title} className="legend-section">
              <h3>{section.title}</h3>
              {section.note && <p className="muted legend-note">{section.note}</p>}
              <ul>
                {section.items.map((item) => (
                  <li key={item.name} className="legend-item">
                    <LegendSwatch swatch={item.swatch} />
                    <div className="legend-text">
                      <strong>{item.name}</strong>
                      {item.where && <p className="legend-where">{item.where}</p>}
                      <p>{item.what}</p>
                      {(item.up || item.down) && (
                        <div className="legend-moves">
                          {item.up && (
                            <p className="legend-up">
                              <span>▲ Roste</span>
                              {item.up}
                            </p>
                          )}
                          {item.down && (
                            <p className="legend-down">
                              <span>▼ Klesá</span>
                              {item.down}
                            </p>
                          )}
                        </div>
                      )}
                      {item.how && <p className="legend-how">{item.how}</p>}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}

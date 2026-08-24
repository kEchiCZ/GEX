/** Testy spodních panelů (issue #26): layout dle checkboxů, C/P barvy, Cum Δ plochy, sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { BottomPanels } from '../components/BottomPanels'
import { FakeWebSocket } from '../test/fakeWs'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { cumDeltaAreas, barHeights, cvdLinePoints, evoOiDisplay, evoOiStepPath, sentimentCandleGeometry } from './geometry' // prettier-ignore
import type { PanelSeries } from '../components/BottomPanels'

const DATA: PanelSeries = {
  vol: [100, 200, 400, 300],
  optVolCall: [10, 20, 40, 30],
  optVolPut: [15, 5, 25, 35],
  cumDelta: [50, -100, 200, -50],
  deltaFlowCall: [5, 10, 20, 15],
  deltaFlowPut: [7, 2, 12, 17],
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ expiries: [] }) }),
  )
})

// ── Geometrie ──────────────────────────────────────────────────────

test('barHeights normalizuje maximem', () => {
  expect(barHeights([100, 200, 400], 80)).toEqual([20, 40, 80])
})

test('cumDeltaAreas dělí plochu nad/pod nulou a drží rezervu od okrajů (#169)', () => {
  const areas = cumDeltaAreas([100, -100], 200, 80)
  expect(areas.zeroY).toBe(40)
  // Extrém končí CUM_DELTA_PAD (6 px) od okraje — trendující řada nesmí „jet po hraně"
  expect(areas.positive).toContain('50,6')
  expect(areas.positive).toContain('150,40')
  expect(areas.negative).toContain('150,74')
  // Explicitní pad = 0 dá původní chování až k okrajům
  const edge = cumDeltaAreas([100, -100], 200, 80, 0)
  expect(edge.positive).toContain('50,0')
  expect(edge.negative).toContain('150,80')
})

test('cvdLinePoints má vlastní měřítko a přeruší se na dírách (#829)', () => {
  // Vlastní normalizace: řada v úplně jiných jednotkách než plocha opčního
  // toku musí vyplnit pás stejně, jinak by jedna z řad byla plochá u nuly
  const points = cvdLinePoints([100, -100], 200, 80)
  expect(points).toBe('50,6 150,74')

  // Minuty bez dat (bez tasty větve) vypadnou — linka se přeruší místo aby
  // propadla k nule a předstírala vyrovnaný tok
  expect(cvdLinePoints([100, null, -100], 300, 80)).toBe('50,6 250,74')
  expect(cvdLinePoints([null, null], 200, 80)).toBe('')
})

// ── Panely: sdílená osa, C/P barvy, plochy ─────────────────────────

function renderPanels(
  visible = {
    vol: true,
    optVol: true,
    delta: true,
    deltaFlow: false,
    evoOi: false,
    sentiment: false,
  },
) {
  return render(
    <CrosshairProvider>
      <BottomPanels data={DATA} visible={visible} width={400} />
    </CrosshairProvider>,
  )
}

test('vykreslí tři panely; Opt Vol má C/P sloupce, Cum Δ plochy a nulu', () => {
  renderPanels()
  expect(screen.getByLabelText('Vol panel')).toBeDefined()
  const optVol = screen.getByLabelText('Opt Vol panel')
  expect(optVol.querySelectorAll('[data-part="optvol-call"]')).toHaveLength(4)
  expect(optVol.querySelectorAll('[data-part="optvol-put"]')).toHaveLength(4)
  const cumDelta = screen.getByLabelText('Cum Δ panel')
  expect(cumDelta.querySelector('[data-part="cumdelta-positive"]')).not.toBeNull()
  expect(cumDelta.querySelector('[data-part="cumdelta-negative"]')).not.toBeNull()
  expect(screen.getByTestId('cumdelta-zero')).toBeDefined()
})

test('CVD podkladu je druhá řada panelu, bez dat se nekreslí (#829)', () => {
  const { rerender } = render(
    <CrosshairProvider>
      <BottomPanels
        data={{ ...DATA, futuresCvd: [10, -20, 30, -5] }}
        visible={{
          vol: false,
          optVol: false,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.getByTestId('cumdelta-cvd')).toBeDefined()
  expect(screen.getByTestId('cvd-legend')).toBeDefined()

  // Bez řady (starší data, běh bez tasty) zůstane panel při opčním toku
  rerender(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: false,
          optVol: false,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.queryByTestId('cumdelta-cvd')).toBeNull()
  expect(screen.queryByTestId('cvd-legend')).toBeNull()
  expect(
    screen.getByLabelText('Cum Δ panel').querySelector('[data-part="cumdelta-positive"]'),
  ).not.toBeNull()
})

test('kotva seance: levý okraj osy není nula a panel to přizná (#829)', () => {
  const vis = {
    vol: false,
    optVol: false,
    delta: true,
    deltaFlow: false,
    evoOi: false,
    sentiment: false,
  }
  const { rerender } = render(
    <CrosshairProvider>
      <BottomPanels
        data={{ ...DATA, cumDelta: [-16109, -14000, -12000, -9000] }}
        visible={vis}
        width={400}
      />
    </CrosshairProvider>,
  )
  // Kumulativ běží od Globex open (22:00 UTC), osa začíná půlnocí — bez
  // přiznání by se výchylka četla proti nule, která na grafu není
  expect(screen.getByTestId('cumdelta-anchor').textContent).toContain('-16')

  // Den, který začíná od nuly, popisek nemá
  rerender(
    <CrosshairProvider>
      <BottomPanels data={{ ...DATA, cumDelta: [0, 50, -100, 200] }} visible={vis} width={400} />
    </CrosshairProvider>,
  )
  expect(screen.queryByTestId('cumdelta-anchor')).toBeNull()
})

test('vypnutí panelu přeskládá layout (AC)', () => {
  const { rerender } = renderPanels()
  expect(screen.getAllByRole('region')).toHaveLength(3)

  rerender(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: false,
          optVol: true,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Vol panel')).toBeNull()
  expect(screen.getAllByRole('region')).toHaveLength(2)

  rerender(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: false,
          optVol: false,
          delta: false,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Spodní panely')).toBeNull() // nic nezbylo
})

test('Δ Flow panel: C/P delta-vážené sloupce, zapíná se checkboxem', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: false,
          optVol: false,
          delta: false,
          deltaFlow: true,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  const panel = screen.getByLabelText('Δ Flow panel')
  expect(panel.querySelectorAll('[data-part="deltaflow-call"]')).toHaveLength(4)
  expect(panel.querySelectorAll('[data-part="deltaflow-put"]')).toHaveLength(4)
})

test('checkboxy v horní liště řídí panely (integrace přes App)', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)

  expect(screen.getByLabelText('Vol panel')).toBeDefined()
  fireEvent.click(screen.getByLabelText('Vol'))
  expect(screen.queryByLabelText('Vol panel')).toBeNull()
  expect(screen.getByLabelText('Opt Vol panel')).toBeDefined() // ostatní zůstávají
})

test('panely respektují výšku z props (#169)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: true,
          optVol: false,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
        height={160}
      />
    </CrosshairProvider>,
  )
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  expect(volSvg.getAttribute('height')).toBe('160')
  expect(volSvg.getAttribute('viewBox')).toBe('0 0 400 160')
  // Cum Δ nulová linka sedí ve středu nové výšky
  const zero = screen.getByTestId('cumdelta-zero')
  expect(zero.getAttribute('y1')).toBe('80')
})

test('vodorovný předěl mění výšku spodních panelů tažením (#169)', () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  const divider = screen.getByRole('separator', { name: 'Výška spodních panelů' })
  const volSvg = () => screen.getByLabelText('Vol panel').querySelector('svg')!
  expect(volSvg().getAttribute('height')).toBe('84')

  // Delta myši se dělí počtem viditelných panelů (#177): defaultně jsou
  // zapnuté 3 (Vol, Opt Vol, Cum Δ) → 60 px myši = +20 px na panel a hrana
  // celého bloku sleduje kurzor 1:1
  fireEvent.pointerDown(divider, { clientY: 600, pointerId: 1 })
  fireEvent.pointerMove(divider, { clientY: 540, pointerId: 1 }) // tažení nahoru → vyšší panely
  fireEvent.pointerUp(divider, { pointerId: 1 })
  expect(volSvg().getAttribute('height')).toBe('104')

  // Meze: nejde stáhnout pod 25 (#792 — polovina původních 50)
  fireEvent.pointerDown(divider, { clientY: 300, pointerId: 1 })
  fireEvent.pointerMove(divider, { clientY: 900, pointerId: 1 })
  fireEvent.pointerUp(divider, { pointerId: 1 })
  expect(volSvg().getAttribute('height')).toBe('25')
  // Výška se persistuje (ADR-0007)
  expect(window.localStorage.getItem('gexlens.panelHeight')).toBe('25')
})

test('úchyt panelu mění výšku jen jemu; globální předěl výšky sjednotí (#792)', () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  const volSvg = () => screen.getByLabelText('Vol panel').querySelector('svg')!
  const optSvg = () => screen.getByLabelText('Opt Vol panel').querySelector('svg')!
  expect(volSvg().getAttribute('height')).toBe('84')

  // Úchyt na spodní hraně Vol panelu: tažení dolů zvětšuje 1:1 JEN Vol
  const handle = screen.getByRole('separator', { name: 'Výška panelu Vol' })
  fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 })
  fireEvent.pointerMove(handle, { clientY: 140, pointerId: 1 })
  fireEvent.pointerUp(handle, { pointerId: 1 })
  expect(volSvg().getAttribute('height')).toBe('124')
  expect(optSvg().getAttribute('height')).toBe('84')

  // Klamp na nové minimum 25 (#792)
  fireEvent.pointerDown(handle, { clientY: 500, pointerId: 1 })
  fireEvent.pointerMove(handle, { clientY: 100, pointerId: 1 })
  fireEvent.pointerUp(handle, { pointerId: 1 })
  expect(volSvg().getAttribute('height')).toBe('25')
  // Individuální výšky se persistují (ADR-0007)
  expect(JSON.parse(window.localStorage.getItem('gexlens.panelHeights') ?? '{}').vol).toBe(25)

  // Globální předěl individuální výšky maže — blok je zase jednotný
  const divider = screen.getByRole('separator', { name: 'Výška spodních panelů' })
  fireEvent.pointerDown(divider, { clientY: 600, pointerId: 1 })
  fireEvent.pointerMove(divider, { clientY: 570, pointerId: 1 })
  fireEvent.pointerUp(divider, { pointerId: 1 })
  expect(volSvg().getAttribute('height')).toBe(optSvg().getAttribute('height'))
})

test('málo košů se neroztahuje na šířku — ukotvení k pravému okraji (issue #102)', () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  const volGroup = () => screen.getByLabelText('Vol panel').querySelector('g')!
  // 1m: demo den 390 minut vyplní šířku → fit-to-width beze změny (offset 0)
  expect(volGroup().getAttribute('transform')).toBe('translate(0 0) scale(1 1)')
  // 1h: 7 košů × 12 px (strop) → data u pravého okraje: 1200 − 60 − 7×12 = 1056
  fireEvent.click(screen.getByRole('button', { name: '1h' }))
  expect(volGroup().getAttribute('transform')).toBe('translate(1056 0) scale(1 1)')
})

test('crosshair ukazuje hodnoty ukazatelů vpravo (issue #104)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: true,
          optVol: true,
          delta: true,
          deltaFlow: true,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  // Bez crosshairu se hodnoty neukazují
  expect(screen.queryAllByTestId('panel-value')).toHaveLength(0)
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  fireEvent.pointerMove(volSvg, { clientX: 30, clientY: 40 }) // krok 12 px → minuta 2
  // vol[2]=400, cumDelta[2]=200(+), optVol C40/P25, deltaFlow C20/P12
  expect(screen.getByLabelText('Vol panel').querySelector('.panel-value')!.textContent).toBe('400')
  expect(screen.getByLabelText('Cum Δ panel').querySelector('.panel-value')!.textContent).toBe(
    '+200',
  )
  const opt = screen.getByLabelText('Opt Vol panel').querySelector('.panel-value')!
  expect(opt.textContent).toContain('C 40')
  expect(opt.textContent).toContain('P 25')
  const flow = screen.getByLabelText('Δ Flow panel').querySelector('.panel-value')!
  expect(flow.textContent).toContain('C 20')
  expect(flow.textContent).toContain('P 12')
})

test('crosshair drží i mimo data (posun grafu do budoucna) — issue #109', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: true,
          optVol: true,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
      <Reader />
    </CrosshairProvider>,
  )
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // 4 minuty, krok 12 px → data končí na 48 px; x=200 je daleko v budoucnu
  fireEvent.pointerMove(volSvg, { clientX: 200, clientY: 40 })
  expect(screen.getByTestId('reader').textContent).toBe('16') // crosshair drží, minuta mimo rozsah
  expect(screen.getAllByTestId('panel-crosshair').length).toBeGreaterThan(0) // linka i v panelech
  // Vpravo nahoře se mimo data hodnota neukazuje
  expect(screen.getByLabelText('Vol panel').querySelector('.panel-value')).toBeNull()
})

test('panel: hodnota na pravé ose Y podle výšky kurzoru (issue #107)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: true,
          optVol: false,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // volPeak=400, výška panelu 84, škála 0..80; y=44 → ((84−44)/80)×400 = 200
  fireEvent.pointerMove(volSvg, { clientX: 30, clientY: 44 })
  // Vpravo nahoře zůstává hodnota minuty (vol[2]=400), osa Y dává úroveň (200)
  expect(screen.getByLabelText('Vol panel').querySelector('.panel-value')!.textContent).toBe('400')
  expect(screen.getByLabelText('Vol panel').querySelector('.panel-axis-value')!.textContent).toBe(
    '200',
  )
  // Vodorovná crosshair linka na úrovni kurzoru v najetém panelu
  expect(
    screen.getByLabelText('Vol panel').querySelector('[data-testid="panel-crosshair-h"]'),
  ).not.toBeNull()
  // Osová hodnota i vodorovná linka jen v najetém panelu
  expect(screen.getByLabelText('Cum Δ panel').querySelector('.panel-axis-value')).toBeNull()
  expect(
    screen.getByLabelText('Cum Δ panel').querySelector('[data-testid="panel-crosshair-h"]'),
  ).toBeNull()

  // Cum Δ: symetrická škála kolem nuly s rezervou CUM_DELTA_PAD (#169);
  // cumPeak=200, škála (42−6)=36 px na peak; y=60 → ((42−60)/36)×200 = −100
  const cumSvg = screen.getByLabelText('Cum Δ panel').querySelector('svg')!
  fireEvent.pointerMove(cumSvg, { clientX: 30, clientY: 60 })
  expect(screen.getByLabelText('Cum Δ panel').querySelector('.panel-axis-value')!.textContent).toBe(
    '-100',
  )
  // Přechod panelu přesune osovou hodnotu (Vol už ji nemá)
  expect(screen.getByLabelText('Vol panel').querySelector('.panel-axis-value')).toBeNull()

  // Opuštění panelu osovou hodnotu skryje
  fireEvent.pointerLeave(cumSvg)
  expect(screen.getByLabelText('Cum Δ panel').querySelector('.panel-axis-value')).toBeNull()
})

// ── Crosshair sdílený s heatmapou ──────────────────────────────────

function Reader() {
  const { position } = useCrosshair()
  return <output data-testid="reader">{position ? position.minuteIdx : 'none'}</output>
}

test('pohyb v panelu nastaví minutu crosshairu; linka se kreslí ve všech panelech', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: true,
          optVol: true,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
      />
      <Reader />
    </CrosshairProvider>,
  )
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // 4 minuty → krok zastropovaný na 12 px (BUCKET_MAX_PX); x=30 → minuta 2
  fireEvent.pointerMove(volSvg, { clientX: 30, clientY: 40 })

  expect(screen.getByTestId('reader').textContent).toBe('2')
  const lines = screen.getAllByTestId('panel-crosshair')
  expect(lines).toHaveLength(3) // sdílená osa X — linka ve všech panelech
  for (const line of lines) {
    expect(Number(line.getAttribute('x1'))).toBe(30) // (2+0.5) × 12
  }
})

test('panely respektují pan/zoom časové osy hlavního grafu (prop time)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{
          vol: true,
          optVol: false,
          delta: false,
          deltaFlow: false,
          evoOi: false,
          sentiment: false,
        }}
        width={400}
        time={{ offsetX: 40, zoomX: 2 }}
      />
      <Reader />
    </CrosshairProvider>,
  )
  const svg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // Obsah je v transformované skupině — stejné mapování jako heatmapa
  expect(svg.querySelector('g')?.getAttribute('transform')).toBe('translate(40 0) scale(2 1)')
  // Inverze ukazatele: x=76 → base (76-40)/2 = 18 → minuta 1 (krok 12)
  fireEvent.pointerMove(svg, { clientX: 76, clientY: 40 })
  expect(screen.getByTestId('reader').textContent).toBe('1')
})

test('panel Sentiment kreslí plochu jako polygon, ne prázdný path (#288)', () => {
  const data: PanelSeries = { ...DATA, sentiment: [-1, 0, 2, 1] }
  render(
    <CrosshairProvider>
      <BottomPanels
        data={data}
        visible={{
          vol: false,
          optVol: false,
          delta: false,
          deltaFlow: false,
          evoOi: false,
          sentiment: true,
        }}
      />
    </CrosshairProvider>,
  )
  const pos = document.querySelector('[data-part="sentiment-pos"]')
  const neg = document.querySelector('[data-part="sentiment-neg"]')
  // Polygon, ne path — cumDeltaAreas vrací body, takže `d` by se nevykreslilo
  expect(pos?.tagName.toLowerCase()).toBe('polygon')
  expect(neg?.tagName.toLowerCase()).toBe('polygon')
  expect(pos?.getAttribute('points')?.length ?? 0).toBeGreaterThan(0)
})

// ── Sentiment svíčky v Daily pohledu (#296, SPEC 7.1) ──────────────

test('sentimentCandleGeometry: symetrická škála kolem nuly, dny bez dat se přeskočí', () => {
  const { geoms, zeroY } = sentimentCandleGeometry(
    [
      { open: 0, high: 1, low: -0.5, close: 0.5 },
      null,
      { open: 0.5, high: 0.5, low: -1, close: -1 },
    ],
    10,
    80,
    0,
  )
  expect(zeroY).toBe(40)
  expect(geoms).toHaveLength(2) // null den nekreslí nic
  expect(geoms.map((geom) => geom.index)).toEqual([0, 2])
  // Peak = max |high|,|low| = 1 → high 1 sedí na horní hraně, low −1 na spodní
  expect(geoms[0].wickY1).toBe(0)
  expect(geoms[1].wickY2).toBe(80)
  expect(geoms[0].up).toBe(true)
  expect(geoms[1].up).toBe(false)
  // Tělo první svíčky: open 0 → 40, close 0.5 → 20
  expect(geoms[0].bodyY).toBe(20)
  expect(geoms[0].bodyHeight).toBe(20)
})

test('panel Sentiment kreslí svíčky místo plochy, když dorazí Daily OHLC (#296)', () => {
  const candles = [
    { open: 0, high: 1, low: -0.5, close: 0.5 },
    { open: 0.5, high: 0.6, low: -0.2, close: -0.1 },
  ]
  const { container } = render(
    <CrosshairProvider>
      <BottomPanels
        data={{ ...DATA, sentimentCandles: candles }}
        visible={{
          vol: false,
          optVol: false,
          delta: false,
          deltaFlow: false,
          evoOi: false,
          sentiment: true,
        }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.getByLabelText('Sentiment panel')).toBeDefined()
  expect(container.querySelectorAll('[data-part="sentiment-candle"]')).toHaveLength(2)
  // Plocha (intraday zobrazení) se nekreslí
  expect(container.querySelector('[data-part="sentiment-pos"]')).toBeNull()
  expect(screen.getByTestId('sentiment-zero')).toBeDefined()
})

// ── Evo OI (#573) ──────────────────────────────────────────────────

test('evoOiStepPath: schodovitá cesta bez interpolace (#573)', () => {
  // Tři hodnoty, prostřední beze změny → H přes dva kroky, V jen při změně
  const path = evoOiStepPath([10, 10, 20], 30, (value) => 100 - value)
  expect(path).toBe('M0.0,90.0H10.0H20.0V80.0H30.0')
})

test('evoOiDisplay: Δ od začátku osy jako výchozí čtení (#573)', () => {
  expect(evoOiDisplay([100, 100, 130, 90], 'delta')).toEqual([0, 0, 30, -10])
  expect(evoOiDisplay([100, 130], 'abs')).toEqual([100, 130])
})

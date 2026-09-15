/** Ovládání playbacku (SPEC 7.3): slider přes den, ▶ 1×/5×/20×, live indikátor.

`inline` = vložené do lišty grafu jako jeden flex prvek (#1126 3f), bez
vlastního řádku s okrajem; bez `inline` je to samostatný pruh (`row`). */
import type { Playback, PlaybackSpeed } from '../replay/usePlayback'

export function PlaybackBar({
  playback,
  label,
  inline = false,
}: {
  playback: Playback
  label?: string
  inline?: boolean
}) {
  return (
    <div
      className={inline ? 'playback-bar playback-inline' : 'row playback-bar'}
      role="toolbar"
      aria-label="Playback"
    >
      <button
        className="chip"
        aria-label={playback.playing ? 'Pauza' : 'Přehrát'}
        onClick={playback.playing ? playback.pause : playback.play}
      >
        {playback.playing ? '⏸' : '▶'}
      </button>
      {([1, 5, 20] as PlaybackSpeed[]).map((speed) => (
        <button
          key={speed}
          className={playback.speed === speed ? 'chip active' : 'chip'}
          onClick={() => playback.setSpeed(speed)}
        >
          {speed}×
        </button>
      ))}
      <input
        type="range"
        aria-label="Pozice dne"
        min={0}
        max={playback.lastIndex}
        value={playback.position}
        onChange={(event) => playback.seek(Number(event.target.value))}
      />
      <button
        className={playback.isLive ? 'chip live-chip active' : 'chip live-chip'}
        onClick={playback.goLive}
        aria-label="Návrat na live"
      >
        {playback.isLive ? '● Live' : '⇥ Live'}
      </button>
      {label && <span className="muted">{label}</span>}
    </div>
  )
}

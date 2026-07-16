/** Ovládání playbacku (SPEC 7.3): slider přes den, ▶ 1×/5×/20×, live indikátor. */
import type { Playback, PlaybackSpeed } from '../replay/usePlayback'

export function PlaybackBar({ playback, label }: { playback: Playback; label?: string }) {
  return (
    <div className="row playback-bar" role="toolbar" aria-label="Playback">
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

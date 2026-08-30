import React, { useEffect, useMemo, useRef, useState } from "react";

const ALL_TRACKS = { persistent_id: "", name: "Songs" };

function Icon({ children, size = 18 }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

function MusicIcon({ size }) {
  return (
    <Icon size={size}>
      <path d="M9 18V5l10-2v13" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="16" cy="16" r="3" />
    </Icon>
  );
}

function PlayIcon({ paused = false, size = 18 }) {
  return paused ? (
    <Icon size={size}>
      <path d="M9 6v12M15 6v12" />
    </Icon>
  ) : (
    <svg aria-hidden="true" viewBox="0 0 24 24" width={size} height={size} fill="currentColor">
      <path d="M7.4 4.6a1 1 0 0 1 1.52-.85l11.1 7.4a1 1 0 0 1 0 1.7l-11.1 7.4a1 1 0 0 1-1.52-.85z" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <Icon size={15}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4 4" />
    </Icon>
  );
}

function SkipIcon({ next = false }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="currentColor"
      className={next ? "" : "flip"}
    >
      <path d="M5 5.5a1 1 0 0 1 1.55-.83L15 10.3V5.5a1 1 0 0 1 2 0v13a1 1 0 0 1-2 0v-4.8l-8.45 5.63A1 1 0 0 1 5 18.5z" />
    </svg>
  );
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function formatTrackTime(seconds) {
  return seconds > 0 ? formatTime(seconds) : "—";
}

function Artwork({ track, active = false }) {
  const [failed, setFailed] = useState(false);
  return (
    <span className={active ? "track-artwork active" : "track-artwork"}>
      {!failed ? (
        <img
          src={`/api/tracks/${encodeURIComponent(track.persistent_id)}/artwork`}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <MusicIcon size={19} />
      )}
      {active ? <span className="artwork-playing"><PlayIcon paused size={14} /></span> : null}
    </span>
  );
}

function StarRating({ rating }) {
  return (
    <span className="star-rating" aria-label={`${rating / 20} out of 5 stars`} title={`${rating}/100`}>
      {[20, 40, 60, 80, 100].map((threshold) => (
        <span key={threshold} className={rating >= threshold ? "filled" : ""}>★</span>
      ))}
    </span>
  );
}

export default function App() {
  const [library, setLibrary] = useState(null);
  const [playlist, setPlaylist] = useState(ALL_TRACKS);
  const [tracks, setTracks] = useState([]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ key: "name", direction: "asc" });
  const [selectedTrack, setSelectedTrack] = useState(null);
  const [autoplay, setAutoplay] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const audioRef = useRef(null);

  useEffect(() => {
    fetch("/api/library")
      .then((response) => {
        if (!response.ok) throw new Error("Could not load the library.");
        return response.json();
      })
      .then(setLibrary)
      .catch((requestError) => setError(requestError.message));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const parameter = playlist.persistent_id
      ? `?playlist_id=${encodeURIComponent(playlist.persistent_id)}`
      : "";
    setLoading(true);
    fetch(`/api/tracks${parameter}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Could not load tracks.");
        return response.json();
      })
      .then(({ tracks: nextTracks }) => {
        setTracks(nextTracks);
        setLoading(false);
      })
      .catch((requestError) => {
        if (requestError.name !== "AbortError") {
          setError(requestError.message);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [playlist]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !selectedTrack) return;
    audio.load();
    setElapsed(0);
    if (autoplay) {
      audio.play().catch(() => setPlaying(false));
      setAutoplay(false);
    }
  }, [selectedTrack?.persistent_id, autoplay]);

  const visibleTracks = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    const filtered = needle
      ? tracks.filter((track) =>
          [track.name, track.artist, track.album].some((value) =>
            value.toLocaleLowerCase().includes(needle),
          ),
        )
      : tracks;
    const direction = sort.direction === "asc" ? 1 : -1;
    return [...filtered].sort((left, right) => {
      if (sort.key === "rating" || sort.key === "duration") {
        return (left[sort.key] - right[sort.key]) * direction;
      }
      return left[sort.key].localeCompare(right[sort.key], undefined, {
        numeric: true,
        sensitivity: "base",
      }) * direction;
    });
  }, [tracks, query, sort]);

  function changePlaylist(nextPlaylist) {
    setPlaylist(nextPlaylist);
    setQuery("");
  }

  function changeSort(key) {
    setSort((current) => ({
      key,
      direction: current.key === key && current.direction === "asc" ? "desc" : "asc",
    }));
  }

  function startTrack(track) {
    if (!track?.playable) return;
    if (selectedTrack?.persistent_id === track.persistent_id) {
      audioRef.current?.play().catch(() => setPlaying(false));
      return;
    }
    setAutoplay(true);
    setSelectedTrack(track);
  }

  function togglePlayback() {
    const audio = audioRef.current;
    if (!selectedTrack) {
      startTrack(visibleTracks.find((track) => track.playable));
    } else if (audio?.paused) {
      audio.play().catch(() => setPlaying(false));
    } else {
      audio?.pause();
    }
  }

  function skip(offset) {
    if (!visibleTracks.length) return;
    const current = visibleTracks.findIndex(
      (track) => track.persistent_id === selectedTrack?.persistent_id,
    );
    const start = current < 0 ? 0 : current;
    for (let step = 1; step <= visibleTracks.length; step += 1) {
      const index = (start + offset * step + visibleTracks.length) % visibleTracks.length;
      if (visibleTracks[index].playable) {
        startTrack(visibleTracks[index]);
        return;
      }
    }
  }

  const sortMark = (key) => (sort.key === key ? (sort.direction === "asc" ? " ↑" : " ↓") : "");
  const trackCount = library?.metadata?.track_count ?? tracks.length;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="traffic-lights" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div className="transport compact-transport">
          <button type="button" aria-label="Previous track" onClick={() => skip(-1)}>
            <SkipIcon />
          </button>
          <button type="button" className="top-play" aria-label={playing ? "Pause" : "Play"} onClick={togglePlayback}>
            <PlayIcon paused={playing} size={17} />
          </button>
          <button type="button" aria-label="Next track" onClick={() => skip(1)}>
            <SkipIcon next />
          </button>
        </div>
        <div className="now-playing-top">
          <div className="mini-art"><MusicIcon size={18} /></div>
          <div>
            <strong>{selectedTrack?.name || "Not Playing"}</strong>
            <span>{selectedTrack ? `${selectedTrack.artist} — ${selectedTrack.album}` : "Choose a song from your library"}</span>
          </div>
        </div>
        <label className="search-box">
          <SearchIcon />
          <span className="sr-only">Search tracks</span>
          <input
            type="search"
            placeholder="Search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </header>

      <aside className="sidebar">
        <div className="brand"><span className="brand-icon"><MusicIcon size={21} /></span>Music</div>
        <nav aria-label="Library navigation">
          <p className="nav-heading">Library</p>
          <button
            type="button"
            className={!playlist.persistent_id ? "nav-item active" : "nav-item"}
            onClick={() => changePlaylist(ALL_TRACKS)}
          >
            <MusicIcon size={17} />
            Songs
          </button>
          <p className="nav-heading playlist-heading">Playlists</p>
          <div className="playlist-list">
            {library?.playlists.map((item) => (
              <button
                type="button"
                key={item.persistent_id}
                className={playlist.persistent_id === item.persistent_id ? "nav-item active" : "nav-item"}
                onClick={() => changePlaylist(item)}
                title={item.name}
              >
                <span className={item.smart ? "playlist-glyph smart" : "playlist-glyph"}>
                  {item.smart ? "✦" : "≡"}
                </span>
                <span>{item.name}</span>
              </button>
            ))}
          </div>
        </nav>
        <div className="snapshot-note">
          <span>Read-only snapshot</span>
          <strong>{library?.snapshot || "Loading…"}</strong>
        </div>
      </aside>

      <main className="content">
        <section className="library-header">
          <div>
            <p className="eyebrow">{playlist.persistent_id ? (playlist.smart ? "Smart Playlist" : "Playlist") : "Library"}</p>
            <h1>{playlist.name}</h1>
            <p>{loading ? "Loading tracks…" : `${visibleTracks.length.toLocaleString()} songs`}</p>
          </div>
          <button
            type="button"
            className="primary-action"
            onClick={() => startTrack(visibleTracks.find((track) => track.playable))}
            disabled={!visibleTracks.some((track) => track.playable)}
          >
            <PlayIcon size={14} /> Play
          </button>
        </section>

        {error ? <div className="error-banner" role="alert">{error}</div> : null}

        <div className="table-wrap" aria-busy={loading}>
          <table>
            <thead>
              <tr>
                <th className="favorite-column"><span className="sr-only">Favorite</span></th>
                <th><button type="button" onClick={() => changeSort("name")}>Song{sortMark("name")}</button></th>
                <th className="rating-column"><button type="button" onClick={() => changeSort("rating")}>Rating{sortMark("rating")}</button></th>
                <th><button type="button" onClick={() => changeSort("artist")}>Artist{sortMark("artist")}</button></th>
                <th className="time-column"><button type="button" onClick={() => changeSort("duration")}>Time{sortMark("duration")}</button></th>
                <th className="more-column"><span className="sr-only">More</span></th>
              </tr>
            </thead>
            <tbody>
              {visibleTracks.map((track) => {
                const active = selectedTrack?.persistent_id === track.persistent_id;
                return (
                  <tr
                    key={track.persistent_id}
                    data-track-id={track.persistent_id}
                    className={active ? "selected" : ""}
                    onClick={() => setSelectedTrack(track)}
                    onDoubleClick={() => startTrack(track)}
                  >
                    <td className="favorite-column">
                      {track.favorited ? <span className="favorite-star" title="Favorite">★</span> : null}
                    </td>
                    <td>
                      <div className="song-cell">
                        <Artwork track={track} active={active && playing} />
                        <div className="track-title">
                          <strong>{track.name}</strong>
                          <span>{track.album || "Unknown Album"}</span>
                        </div>
                      </div>
                    </td>
                    <td className="rating-column"><StarRating rating={track.rating} /></td>
                    <td>{track.artist || "Unknown Artist"}</td>
                    <td className="time-column">{formatTrackTime(track.duration)}</td>
                    <td className="more-column"><span aria-hidden="true">•••</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!loading && !visibleTracks.length ? (
            <div className="empty-state">
              <SearchIcon />
              <strong>No songs found</strong>
              <span>Try another search or playlist.</span>
            </div>
          ) : null}
        </div>
      </main>

      <footer className="player">
        <div className="player-track">
          <div className="artwork"><MusicIcon size={26} /></div>
          <div>
            <strong>{selectedTrack?.name || "Select a song"}</strong>
            <span>{selectedTrack?.artist || `${Number(trackCount).toLocaleString()} songs in your library`}</span>
          </div>
        </div>
        <div className="player-center">
          <div className="transport">
            <button type="button" aria-label="Previous track" onClick={() => skip(-1)}><SkipIcon /></button>
            <button type="button" className="play-button" aria-label={playing ? "Pause" : "Play"} onClick={togglePlayback}>
              <PlayIcon paused={playing} size={19} />
            </button>
            <button type="button" aria-label="Next track" onClick={() => skip(1)}><SkipIcon next /></button>
          </div>
          <div className="timeline">
            <span>{formatTime(elapsed)}</span>
            <input
              aria-label="Playback position"
              type="range"
              min="0"
              max={duration || 0}
              step="0.1"
              value={Math.min(elapsed, duration || 0)}
              disabled={!duration}
              onChange={(event) => {
                const time = Number(event.target.value);
                audioRef.current.currentTime = time;
                setElapsed(time);
              }}
            />
            <span>-{formatTime(Math.max(0, duration - elapsed))}</span>
          </div>
        </div>
        <div className="player-meta">
          <span>{selectedTrack?.album || playlist.name}</span>
        </div>
        <audio
          ref={audioRef}
          src={selectedTrack?.playable ? `/api/tracks/${encodeURIComponent(selectedTrack.persistent_id)}/audio` : undefined}
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => skip(1)}
          onTimeUpdate={(event) => setElapsed(event.currentTarget.currentTime)}
          onDurationChange={(event) => setDuration(event.currentTarget.duration || 0)}
        />
      </footer>
    </div>
  );
}

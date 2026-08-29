import type { TrackInfo } from "../api/types";
import styles from "./TrackCard.module.css";

export function TrackCard({ track, onSelect }: { track: TrackInfo; onSelect: () => void }) {
  return (
    <button className={styles.card} onClick={onSelect}>
      <div className={styles.head}>
        <span className={`${styles.dot} ${track.pro ? styles.dotPro : ""}`} />
        <span className={styles.name}>{track.name}</span>
        {track.pro && <span className="tag-pro">PRO · BETA</span>}
      </div>
      <p className={styles.body}>{track.blurb}</p>
      <div className={styles.meta}>
        <span className={styles.metaText}>{track.meta}</span>
        <span className={styles.arrow} aria-hidden="true">
          →
        </span>
      </div>
    </button>
  );
}

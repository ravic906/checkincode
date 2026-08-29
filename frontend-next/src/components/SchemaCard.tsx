import styles from "./SchemaCard.module.css";

export function SchemaCard({ name, cols }: { name: string; cols: string }) {
  return (
    <div className={styles.card}>
      <div className={styles.name}>{name}</div>
      <div className={styles.cols}>{cols}</div>
    </div>
  );
}

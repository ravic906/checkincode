import styles from "./StatCard.module.css";

export function StatCard({ value, label }: { value: string | number; label: string }) {
  return (
    <div className={styles.card}>
      <div className={styles.value}>{value}</div>
      <div className={styles.label}>{label}</div>
    </div>
  );
}
